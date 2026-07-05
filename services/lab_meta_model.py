"""Interpretable R2 meta-models for Strategy Lab."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

from schemas.market_data import Bar
from services.lab_evaluator import LabSimulationConfig, equity_curve, evaluate_signals, metrics_from_trades
from services.lab_features import feature_names
from services.lab_r1_scan import _compact_eval, break_even_bp
from services.lab_walkforward import HoldoutQuarantine, WalkForwardConfig, build_windows


@dataclass(frozen=True)
class MetaModelConfig:
    barriers: tuple[float, ...] = (0.55, 0.60, 0.65)
    seed: int = 7
    cost_grid: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 5.0)
    maker_bp: float = 2.0
    walkforward: WalkForwardConfig = field(default_factory=WalkForwardConfig)


def evaluate_meta_rule(
    rows: list[dict],
    bars: list[Bar],
    *,
    scheme: str,
    model_kind: str,
    barrier: float,
    config: MetaModelConfig | None = None,
) -> dict:
    cfg = config or MetaModelConfig()
    names = feature_names(rows)
    row_by_ts = {row["timestamp"]: row for row in rows}
    bar_by_ts = {bar.timestamp: bar for bar in bars}
    quarantine = HoldoutQuarantine(bars, cfg.walkforward)
    all_predictions: list[dict] = []
    cost_trades: dict[float, list[dict]] = {bp: [] for bp in cfg.cost_grid}
    invalid_reasons: list[str] = []
    importances: dict[str, float] = {name: 0.0 for name in names}
    importance_folds = 0
    for window in build_windows(bars, cfg.walkforward):
        train_rows = _rows_between(rows, window["train_start"], window["train_end"])
        val_bars = quarantine.public_bars_between(datetime.fromisoformat(window["validate_start"]), datetime.fromisoformat(window["validate_end"]))
        val_rows = [row_by_ts[bar.timestamp] for bar in val_bars if bar.timestamp in row_by_ts]
        if len(train_rows) < 100 or len(val_rows) < 10:
            invalid_reasons.append("insufficient_train_or_validate_rows")
            continue
        models = _fit_direction_models(train_rows, names, scheme, model_kind, cfg.seed)
        for key in ("long", "short"):
            for name, value in models[key]["importance"].items():
                importances[name] += value
        importance_folds += 1
        predictions = _predict_rows(models, val_rows, names, scheme)
        all_predictions.extend(predictions)
        signals = _signals_from_predictions(predictions, val_bars, barrier)
        for bp in cfg.cost_grid:
            result = evaluate_signals(val_bars, signals, LabSimulationConfig(cost_bp_per_side=bp))
            if result.get("status") == "valid":
                cost_trades[bp].extend(result["trades"])
            else:
                invalid_reasons.append(str(result.get("reason", "invalid")))
    cost_results = {f"{bp:g}bp": _compact_eval(_aggregate_trades(trades, bp)) for bp, trades in cost_trades.items()}
    calibration = _calibration(all_predictions, scheme)
    avg_importance = {name: round(value / importance_folds, 8) for name, value in importances.items()} if importance_folds else {}
    maker = cost_results.get(f"{cfg.maker_bp:g}bp", {})
    gross = cost_results.get("0bp", {})
    status = "valid" if gross.get("status") == "valid" else "invalid"
    row = {
        "scheme": scheme,
        "model": model_kind,
        "barrier": barrier,
        "oos_trades": gross.get("metrics", {}).get("trade_count"),
        "win_rate": gross.get("metrics", {}).get("win_rate"),
        "gross_bp_per_trip": gross.get("metrics", {}).get("expectancy_bp_on_notional"),
        "break_even_bp": break_even_bp(cost_results),
        "maker_2bp_expectancy_per_trade": maker.get("metrics", {}).get("expectancy_per_trade"),
        "max_drawdown_pct": maker.get("metrics", {}).get("max_drawdown_pct"),
        "sortino_at_2bp": maker.get("metrics", {}).get("sortino"),
        "status": status,
        "reason": "pass" if status == "valid" else ",".join(sorted(set(invalid_reasons))) or "invalid",
    }
    if row["status"] == "valid" and int(row.get("oos_trades") or 0) < 100:
        row["status"] = "insufficient_sample"
        row["reason"] = "insufficient_sample"
    return {
        "status": row["status"],
        "row": row,
        "cost_grid_results": cost_results,
        "calibration": calibration,
        "feature_importances": _top_importances(avg_importance),
        "prediction_count": len(all_predictions),
    }


def _fit_direction_models(rows: list[dict], names: list[str], scheme: str, model_kind: str, seed: int) -> dict:
    x = np.array([[float(row.get(name, 0.0)) for name in names] for row in rows], dtype=float)
    x = np.nan_to_num(x)
    out = {}
    for direction, label in (("long", "long_win"), ("short", "short_win")):
        y = np.array([1 if row.get(f"label_{scheme}") == label else 0 for row in rows], dtype=int)
        out[direction] = _fit_one(x, y, names, model_kind, seed)
    return out


def _fit_one(x: np.ndarray, y: np.ndarray, names: list[str], model_kind: str, seed: int) -> dict:
    positives = int(y.sum())
    negatives = int(len(y) - positives)
    if positives < 3 or negatives < 3:
        probability = positives / len(y) if len(y) else 0.0
        return {"model": _ConstantModel(probability), "importance": {name: 0.0 for name in names}}
    base = _base_model(model_kind, seed)
    calibrated = CalibratedClassifierCV(base, method="sigmoid", cv=3)
    calibrated.fit(x, y)
    return {"model": calibrated, "importance": _importance_from_calibrated(calibrated, names)}


def _base_model(model_kind: str, seed: int):
    if model_kind == "logistic_l2":
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=2000, random_state=seed, class_weight="balanced"),
        )
    if model_kind == "gbt_depth3":
        return GradientBoostingClassifier(
            max_depth=3,
            n_estimators=24,
            max_features="sqrt",
            subsample=0.8,
            random_state=seed,
        )
    raise ValueError(f"unknown model: {model_kind}")


def _predict_rows(models: dict, rows: list[dict], names: list[str], scheme: str) -> list[dict]:
    if not rows:
        return []
    x = np.array([[float(row.get(name, 0.0)) for name in names] for row in rows], dtype=float)
    x = np.nan_to_num(x)
    p_long = _predict_proba(models["long"]["model"], x)
    p_short = _predict_proba(models["short"]["model"], x)
    out = []
    for idx, row in enumerate(rows):
        direction = "long" if p_long[idx] >= p_short[idx] else "short"
        probability = float(max(p_long[idx], p_short[idx]))
        out.append({
            "timestamp": row["timestamp"],
            "direction": direction,
            "probability": probability,
            "actual_positive": 1 if row.get(f"label_{scheme}") == f"{direction}_win" else 0,
            "atr": float(row.get("atr", 0.0)),
            "target_atr": float(row.get("target_atr", 2.0)),
            "stop_atr": float(row.get("stop_atr", 1.0)),
            "horizon_bars": int(row.get("horizon_bars", 48)),
        })
    return out


def _signals_from_predictions(predictions: list[dict], val_bars: list[Bar], barrier: float) -> list[dict]:
    index_by_ts = {bar.timestamp: index for index, bar in enumerate(val_bars)}
    signals = []
    for item in predictions:
        if item["probability"] < barrier or item["timestamp"] not in index_by_ts:
            continue
        index = index_by_ts[item["timestamp"]]
        close = float(val_bars[index].close)
        atr_pct = item["atr"] / close if close else 0.0
        signals.append({
            "index": index,
            "direction": item["direction"],
            "timestamp": item["timestamp"],
            "close": close,
            "stop_pct": max(0.0001, atr_pct * item["stop_atr"]),
            "target_pct": max(0.0001, atr_pct * item["target_atr"]),
            "max_hold_bars": item["horizon_bars"],
        })
    return signals


def _aggregate_trades(trades: list[dict], bp: float) -> dict:
    if not trades:
        return {"status": "invalid", "reason": "zero_trades", "trades": [], "metrics": {}}
    equity = equity_curve(trades, 10_000)
    return {
        "status": "valid",
        "reason": "pass",
        "trades": trades,
        "equity": equity,
        "metrics": metrics_from_trades(trades, equity, LabSimulationConfig(cost_bp_per_side=bp)),
    }


def _calibration(predictions: list[dict], scheme: str) -> dict:
    if not predictions:
        return {"brier": None, "bins": [], "scheme": scheme}
    probs = [float(item["probability"]) for item in predictions]
    actual = [int(item["actual_positive"]) for item in predictions]
    bins = []
    for left in [i / 10 for i in range(10)]:
        right = left + 0.1
        idx = [i for i, prob in enumerate(probs) if left <= prob < right or (right == 1.0 and left <= prob <= right)]
        if not idx:
            continue
        bins.append({
            "bin": f"{left:.1f}-{right:.1f}",
            "count": len(idx),
            "mean_predicted": round(sum(probs[i] for i in idx) / len(idx), 6),
            "empirical_rate": round(sum(actual[i] for i in idx) / len(idx), 6),
        })
    return {"brier": round(float(brier_score_loss(actual, probs)), 8), "bins": bins, "scheme": scheme}


def _importance(model: Any, names: list[str]) -> dict[str, float]:
    if isinstance(model, Pipeline):
        if "logisticregression" in model.named_steps:
            model = model.named_steps["logisticregression"]
        elif model.steps:
            model = model.steps[-1][1]
    if hasattr(model, "coef_"):
        values = np.abs(model.coef_[0])
    elif hasattr(model, "feature_importances_"):
        values = model.feature_importances_
    else:
        values = np.zeros(len(names))
    return {name: round(float(value), 8) for name, value in zip(names, values)}


def _importance_from_calibrated(model: Any, names: list[str]) -> dict[str, float]:
    estimators = []
    for item in getattr(model, "calibrated_classifiers_", []) or []:
        estimator = getattr(item, "estimator", None) or getattr(item, "base_estimator", None)
        if estimator is not None:
            estimators.append(estimator)
    if not estimators:
        return {name: 0.0 for name in names}
    sums = {name: 0.0 for name in names}
    for estimator in estimators:
        for name, value in _importance(estimator, names).items():
            sums[name] += value
    return {name: round(value / len(estimators), 8) for name, value in sums.items()}


def _top_importances(values: dict[str, float], limit: int = 15) -> list[dict]:
    return [{"feature": key, "importance": value} for key, value in sorted(values.items(), key=lambda item: -item[1])[:limit]]


def _rows_between(rows: list[dict], start: str, end: str) -> list[dict]:
    left = datetime.fromisoformat(start)
    right = datetime.fromisoformat(end)
    return [row for row in rows if left <= datetime.fromisoformat(str(row["timestamp"])) <= right]


def _predict_proba(model: Any, x: np.ndarray) -> np.ndarray:
    values = model.predict_proba(x)
    return values[:, 1] if values.shape[1] > 1 else np.zeros(len(x))


class _ConstantModel:
    def __init__(self, probability: float) -> None:
        self.probability = float(probability)

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        p = np.full(len(x), self.probability)
        return np.vstack([1 - p, p]).T
