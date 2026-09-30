from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage27_empirical_fee_rounding() -> None:
    replay = load_script("stage27_replay_execution.py")
    assert replay.fee(5.0, 0.54) == 0.08694
    assert replay.fee(5.0, 0.32) == 0.07616


def test_target_conversion_hash_ignores_outcome_and_pnl() -> None:
    target = load_script("stage27_target_conversion.py")
    base = pd.Series(
        {
            "polymarket_slug": "btc-updown-5m-test",
            "date_utc": "2026-08-14",
            "latency_ms": 225,
            "price_bucket": "0.30-0.45",
            "tte_bucket": "100-140",
            "edge_bucket": "0.025-0.035",
            "outcome": 0,
            "net_strategy_pnl": -1.0,
        }
    )
    changed = base.copy()
    changed["outcome"] = 1
    changed["net_strategy_pnl"] = 100.0
    assert target.stable_score(17, base) == target.stable_score(17, changed)


def test_target_conversion_selection_is_deterministic_and_stratified() -> None:
    target = load_script("stage27_target_conversion.py")
    rows = []
    for group in ("low", "high"):
        for index in range(10):
            rows.append(
                {
                    "polymarket_slug": f"{group}-{index}",
                    "date_utc": "2026-08-14",
                    "latency_ms": 225,
                    "price_bucket": group,
                    "tte_bucket": "100-140",
                    "edge_bucket": "0.025-0.035",
                }
            )
    pool = pd.DataFrame(rows)
    first = target.stratified_select(pool, 6, 9)
    second = target.stratified_select(pool, 6, 9)
    assert first.polymarket_slug.tolist() == second.polymarket_slug.tolist()
    assert first.price_bucket.value_counts().to_dict() == {"low": 3, "high": 3}


def test_stage27_grid_is_frozen() -> None:
    replay = load_script("stage27_replay_execution.py")
    assert replay.EDGES == (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
    assert replay.LATENCIES == (175, 225, 325)
    assert replay.MAX_SUBMISSIONS == 3
    assert replay.RETRY_SECONDS == 2.0
