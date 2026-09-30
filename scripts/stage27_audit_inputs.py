#!/usr/bin/env python3
"""Cheap Stage 27 input/provenance audit; never invokes a replay simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path("/Volumes/Kate_DATA/stats_bot")
POLICY = Path("/Volumes/Kate_DATA/policy_finder")
STATE = (
    POLICY / "twap/data/artifacts/stage23_5_mr_fixed_edge_btc/cache/development_state"
)
STAGE26B = (
    POLICY
    / "twap/data/artifacts/gcp_returned/stage26b_ranked_detector_economic_screen_btc/twap/data/artifacts/stage26b_ranked_detector_economic_screen_btc"
)
STAGE26D = POLICY / "twap/data/artifacts/stage26d_local_pulse_guard_btc_corrected"
MODEL = ROOT / "model_artifacts/btc5m_v1_aug14_sep14/final_strategy.json"
LIVE_AUDIT = ROOT / "live_run_audit_20260929_48da1a63_data"
DEFAULT_OUTPUT = ROOT / "twap/data/artifacts/stage27_live_execution_fee_edge_btc"
DATES = tuple(str(x.date()) for x in pd.date_range("2026-08-14", "2026-09-14"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def size(path: Path) -> int:
    return (
        sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        if path.is_dir()
        else path.stat().st_size
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("AUDIT",), default="AUDIT")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--scenario", default="ALL")
    p.add_argument("--c-edge", default="2.5")
    p.add_argument("--latency", default="175,225,325")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    del args.workers, args.resume, args.scenario, args.c_edge, args.latency
    required = [
        STATE,
        STATE / "_COMPLETE.json",
        STAGE26B / "detector_timeline.parquet",
        STAGE26D / "run_manifest.json",
        MODEL,
        LIVE_AUDIT / "live_entry_fills_from_journal.parquet",
    ]
    missing = [str(x) for x in required if not x.exists()]
    if missing:
        raise FileNotFoundError("Missing Stage27 inputs:\n" + "\n".join(missing))
    state_files = sorted(STATE.glob("date_utc=*.parquet"))
    dates = tuple(x.stem.removeprefix("date_utc=") for x in state_files)
    model = json.loads(MODEL.read_text())
    stage26d = json.loads((STAGE26D / "run_manifest.json").read_text())
    fee_audit = pd.read_csv(LIVE_AUDIT / "trade_wallet_reconciliation.csv")
    wallet_activity = pd.read_csv(LIVE_AUDIT / "polymarket_wallet_activity.csv")
    wallet_activity = wallet_activity.loc[
        wallet_activity["type"].eq("TRADE") & wallet_activity["side"].eq("BUY")
    ].copy()
    wallet_activity["implied_fee"] = wallet_activity.usdcSize.astype(
        float
    ) - wallet_activity["size"].astype(float) * wallet_activity.price.astype(float)
    expected_fee = wallet_activity.groupby("slug", observed=True).implied_fee.sum()
    reconciled_fee = fee_audit.set_index("market_slug").fee.astype(float)
    common_fee_markets = expected_fee.index.intersection(reconciled_fee.index)
    fee_error = (
        expected_fee.loc[common_fee_markets] - reconciled_fee.loc[common_fee_markets]
    ).abs()
    gates = [
        (
            "development_dates_exact",
            dates == DATES,
            f"{dates[0] if dates else None}..{dates[-1] if dates else None}; n={len(dates)}",
        ),
        (
            "holdout_absent",
            not any(
                d in dates
                for d in ("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")
            ),
            str(dates),
        ),
        (
            "model_version",
            model.get("strategy_version") == "btc5m_v1_aug14_sep14",
            model.get("strategy_version"),
        ),
        (
            "training_period",
            model.get("training_start") == DATES[0]
            and model.get("training_end") == DATES[-1],
            f"{model.get('training_start')}..{model.get('training_end')}",
        ),
        (
            "model_family",
            model.get("base_model", {}).get("family") == "smoothed_lam10000",
            model.get("base_model", {}).get("family"),
        ),
        (
            "holdout_not_fit",
            model.get("parity_validation", {}).get("holdout_used_for_fit") is False,
            model.get("parity_validation", {}).get("holdout_used_for_fit"),
        ),
        (
            "p15_detector",
            stage26d.get("detector_id")
            == "MODEL_EDGE_MEDIAN_W120_M4_EN20_EX30_R15_NONE_CE1",
            stage26d.get("detector_id"),
        ),
        (
            "latencies",
            stage26d.get("latencies_ms") == [175, 225, 325],
            stage26d.get("latencies_ms"),
        ),
        (
            "execution_config",
            stage26d.get("canonical_replay_base", {}).get("shares") == 5.0,
            stage26d.get("canonical_replay_base"),
        ),
        (
            "empirical_fee_formula",
            len(common_fee_markets) == len(fee_audit)
            and bool(np.allclose(fee_error, 0.0, rtol=0.0, atol=1e-10)),
            f"markets={len(common_fee_markets)} max_abs_error={float(fee_error.max()):.12g}",
        ),
    ]
    args.output_root.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame(gates, columns=["gate", "passed", "detail"])
    out.to_csv(args.output_root / "integrity_gates.csv", index=False)
    manifest = {
        "development_dates": [DATES[0], DATES[-1]],
        "holdout_used": False,
        "state_cache": str(STATE),
        "stage26b": str(STAGE26B),
        "stage26d": str(STAGE26D),
        "live_audit": str(LIVE_AUDIT),
        "production_model": str(MODEL),
        "production_model_sha256": sha256(MODEL),
        "input_sizes_bytes": {
            str(x): size(x) for x in (STATE, STAGE26B, STAGE26D, LIVE_AUDIT)
        },
        "latencies_ms": [175, 225, 325],
        "c_edges_cents": [2, 2.5, 3, 3.5, 4, 4.5, 5],
        "fee": "round(shares * 0.07 * price * (1-price), 5)",
    }
    (args.output_root / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    (args.output_root / "stage27_live_execution_semantics.md").write_text(
        (ROOT / "stage27_live_execution_semantics.md").read_text()
    )
    print(out.to_string(index=False))
    print(json.dumps(manifest["input_sizes_bytes"], indent=2))
    if not out.passed.all():
        raise RuntimeError("Stage27 input audit failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
