"""Persist the exact upstream instrument contract required by Nautilus shadow mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_config import dualtrack_config
from services.dualtrack_instrument_source import DEFAULT_INSTRUMENT_ENDPOINT, fetch_execution_instrument_definition
from services.journal_store import write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and validate a DualTrack Nautilus shadow instrument definition.")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    shadow = ((dualtrack_config().get("execution_shadow") or {}).get("nautilus") or {})
    endpoint = str(args.endpoint or shadow.get("instrument_endpoint") or DEFAULT_INSTRUMENT_ENDPOINT)
    source = str(args.source or shadow.get("instrument_source") or "binance_usdm_futures")
    definition = fetch_execution_instrument_definition(endpoint=endpoint, source=source)
    fee_model = shadow.get("fee_model") if isinstance(shadow.get("fee_model"), dict) else {}
    fee_model = dict(fee_model)
    if definition.get("maker_fee_rate") not in (None, "") and definition.get("taker_fee_rate") not in (None, ""):
        fee_model = {
            "mode": "upstream_instrument",
            "maker_fee_rate": definition["maker_fee_rate"],
            "taker_fee_rate": definition["taker_fee_rate"],
            "source": "instrument-definition-v1",
            "real_money_eligible": False,
        }
    blockers = []
    if fee_model.get("maker_fee_rate") in (None, "") or fee_model.get("taker_fee_rate") in (None, ""):
        blockers.append("shadow fee model is missing maker_fee_rate or taker_fee_rate")
    if fee_model.get("real_money_eligible") is not False:
        blockers.append("shadow fee model must explicitly be paper-only")
    artifact = {
        "schema_version": "dualtrack-nautilus-instrument-preflight-v1",
        "status": "blocked" if blockers else "ready_for_paper_shadow",
        "instrument": definition,
        "fee_model": fee_model,
        "blockers": blockers,
    }
    write_json(output_root / "dualtrack" / "nautilus" / "instrument_preflight.json", [artifact])
    if args.json:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
    else:
        print(
            "dualtrack_nautilus_shadow_prepare: "
            f"status={artifact['status']} instrument={definition['instrument_id']} blockers={len(artifact['blockers'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
