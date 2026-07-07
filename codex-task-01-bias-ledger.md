# Codex 任务 01：人轨方向分强制记账（bias ledger）

> 不变量编号 DT-INV-2。设计出自 2026-07-07 镜子诊断（park-io/000_park-os/镜子诊断书-v1.md 第 4 节）。
> 你（Codex）的任务是**严格按本规格实现**，不要扩展范围、不要重新设计。规格里没写的决策，选最保守的做法并在 PR 描述里列出。

## 背景（为什么做）

双轨系统里机器轨每笔交易都被逐笔记账审计，但人轨的口述方向（market view，含 0-100 的方向分）零记账：观点过期后没有对错裁决，新观点无限滚动覆盖旧观点。本任务建立一本与机器轨同规格的**裁决账本**。

两条核心规则：
1. **新观点的入场券是旧观点的死亡证明**——存在已过期但未裁决的旧观点时，拒绝录入新观点。
2. **fail-closed**——裁决所需数据缺失时，标记 pending、告警、阻断新录入；绝不猜测、绝不编造裁决。

## 动手前必读（理解现有契约，复用而不是重造）

- `services/market_view_intake.py` — 入口 `MarketViewIntake.record()`，本任务的闸门点
- `services/market_view.py` — `MarketViewStore`（`DEFAULT_VALID_FOR_HOURS`、`DEFAULT_PRICE_MOVE_EXPIRY_PCT`）和 `infer_market_view_reference_price()`（价格查询复用它或它底层的 helper，**不要写新 SQL**）
- `services/direction_bias_gate.py` — `_expiry_status()` 的过期语义（expires_at 缺失时用 generated_at + valid_for_hours 推导）；时间戳解析语义照抄它（UTC-aware）
- `services/journal_store.py` — `write_json` 惯例
- `tests/test_market_view_intake.py`、`tests/test_direction_bias_gate.py` — 测试风格与 fixture 惯例
- `pipelines/` 下任一 runner — CLI 入口的惯例

⚠️ 本仓库踩过 local-vs-UTC 混用导致护栏 fail-open 的坑：**所有时间戳一律 UTC-aware，裁决以 expires_at 为键，不以 run_date 为键。**

## 交付物

1. `services/bias_ledger.py`（新建，<400 行）
2. `pipelines/bias_ledger_settle.py`（新建，薄 CLI runner，风格照抄现有 pipelines）
3. `services/market_view_intake.py`（修改：`record()` 入口加闸门，见下）
4. `tests/test_bias_ledger.py`（新建）
5. `services/completion_audit.py` 加一个 `_human_bias_ledger` 检查段（照抄现有 section 模式：账本存在、无 overdue-open、summary 新鲜）

## 数据 schema

账本：`{output_root}/bias_ledger/ledger.jsonl`，**append-only JSONL**，每行一条：

```json
{
  "view_id": "<run_date>-<generated_at的紧凑时间戳>",
  "run_date": "2026-07-06",
  "issued_at": "<generated_at, UTC ISO>",
  "bias_score": 65,
  "price_at_issue": 4202.5,
  "expires_at": "<UTC ISO>",
  "status": "open | adjudicated | pending_data",
  "verdict": null,
  "settle_price": null,
  "realized_move_pct": null,
  "brier": null,
  "adjudicated_at": null,
  "pending_reason": null
}
```

- `price_at_issue`：录入时用 `infer_market_view_reference_price()` 取；取不到 → 条目照常创建但 `pending_reason="missing_reference_price"`，结算时保持 `pending_data`（fail-closed）。
- 已裁决（adjudicated）条目**永不修改**。状态推进 = 追加一行同 view_id 的新记录，读取时以该 view_id 的最后一行为准（event-sourcing 风格，与 append-only 相容）。

汇总：`{output_root}/bias_ledger/summary.json` —— `total / adjudicated / correct / wrong / undecidable / no_claim / pending_data / hit_rate / mean_brier / last_10`。每次结算后**从全量账本重算**（纯派生数据，允许覆盖写，绝不手改）。

## 裁决规则（MECE，逐字实现，阈值做成模块常量并允许构造函数覆盖）

设 `NEUTRAL_MOVE_THRESHOLD_PCT = 0.30`，`SETTLE_BAR_TOLERANCE_MIN = 15`。

1. **结算时点** = `expires_at`（推导规则与 `_expiry_status` 一致：优先 expiry.expires_at，缺失则 generated_at + valid_for_hours）。价格提前触发过期的观点 v1 也统一按 expires_at 结算（简化，v2 再议）。
2. **settle_price** = market_db 中 timestamp ≤ expires_at 的最近一根 1m bar 的 close，且距 expires_at 不超过 `SETTLE_BAR_TOLERANCE_MIN` 分钟；否则 → `status="pending_data"`，`pending_reason="no_bar_within_tolerance"`。
3. `realized_move_pct = (settle_price − price_at_issue) / price_at_issue × 100`
4. `|realized_move_pct| < NEUTRAL_MOVE_THRESHOLD_PCT` → `verdict="undecidable"`，**不计入** brier 与 hit_rate。
5. 否则 `y = 1 if realized_move_pct > 0 else 0`；`brier = (bias_score/100 − y)²`——所有可裁决条目都计 brier，无论是否中性。
6. 方向裁决：`bias_score ≥ 60`（看多主张）→ y==1 为 correct，否则 wrong；`bias_score ≤ 40`（看空主张）→ y==0 为 correct，否则 wrong；`40 < score < 60` → `verdict="no_claim"`（不计 hit_rate，计 brier）。

## 闸门规则（改 `MarketViewIntake.record()`）

录入新观点前：

1. 先对账本中所有 `status="open"` 且已过 expires_at 的条目**自动尝试结算**（调用与 settle pipeline 相同的函数）。
2. 结算后若仍存在 open 或 pending_data 且已过期的条目 → raise `BiasLedgerBlockedError`，错误信息列出被阻塞的 view_id 和 pending_reason，**不录入新观点**。
3. 无阻塞 → 正常录入，并向账本追加新的 open 条目。

正常情况下结算全自动，这个闸门永远不拦人；它只在数据管道坏掉时发作——那正是该停下来的时刻。

## 硬约束

- **不改** `DirectionBiasGate` 的过滤行为；**不碰** broker / 订单 / 执行路径的任何代码。
- 结算幂等：重跑不产生重复裁决、不改变已有裁决（用测试证明）。
- 显式错误处理，禁止裸 `except` 吞错；全部函数带类型注解；dataclass 用 `frozen=True`；遵守 PEP 8。
- 新文件不超过 400 行；超了就拆。

## TDD（先写测试、亲眼看它们红、再实现）

`tests/test_bias_ledger.py` 至少覆盖：

1. 录入观点 → 账本出现 open 条目，含 price_at_issue / expires_at
2. 结算：看多且涨 → correct；看空且涨 → wrong；|涨跌| < 0.30% → undecidable（三例分开）
3. `40 < score < 60` → no_claim 且 brier 有值
4. brier 与 hit_rate 汇总计算正确（手算一组固定样例）
5. 存在过期未裁决条目时 `record()` raise `BiasLedgerBlockedError`，错误信息含 view_id
6. 闸门先自动结算、结算成功后新观点正常录入（自愈路径）
7. bar 缺失（超出容差）→ pending_data + 阻断 + **不产生任何 verdict**
8. 结算重跑幂等：账本行数与裁决内容不变
9. 已裁决条目不可变：后续任何操作不修改该 view_id 已有裁决行

## 完成定义（DOD）

- [ ] 上述 9 类测试全绿，且整个仓库 `pytest` 全绿
- [ ] `python -m pipelines.bias_ledger_settle --run-date <date>` 在 fixture 数据上可裁决、重跑幂等
- [ ] 人为构造过期未裁决条目后，录入新观点确实被拒且报错信息可执行
- [ ] completion_audit 新增段落在正常/异常两种 fixture 下分别 pass/fail
- [ ] PR 描述列出：所有规格未覆盖、由你自行决定的点
