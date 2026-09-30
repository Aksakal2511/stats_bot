#!/usr/bin/env python3
"""Outcome-independent T80/T85/T90 approximation over completed Stage27 E1 units."""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/Volumes/Kate_DATA/stats_bot")
DEFAULT_OUTPUT = ROOT / "twap/data/artifacts/stage27_live_execution_fee_edge_btc"
TARGETS = {"T80": 0.80, "T85": 0.85, "T90": 0.90}
SEEDS = tuple(range(100))


def stable_score(seed: int, row: pd.Series) -> int:
    # Outcome and PnL are intentionally absent.
    key = "|".join(
        map(
            str,
            (
                seed,
                row.polymarket_slug,
                row.date_utc,
                row.latency_ms,
                row.get("price_bucket"),
                row.get("tte_bucket"),
                row.get("edge_bucket"),
            ),
        )
    )
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)


def buckets(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["price_bucket"] = pd.cut(
        out.executable_ask, [0.20, 0.30, 0.45, 0.55, 0.70, 0.8000001], right=False
    ).astype(str)
    out["tte_bucket"] = pd.cut(out.tte, [60, 100, 140, 180, 221], right=False).astype(
        str
    )
    out["edge_bucket"] = pd.cut(
        out.model_edge, [-np.inf, 0.025, 0.035, 0.05, np.inf], right=False
    ).astype(str)
    return out


def make_added(row: pd.Series, scenario: str, seed: int) -> dict:
    price = float(row.executable_ask)
    shares = 5.0
    gross = shares * ((1.0 - price) if int(row.outcome) else -price)
    fee = round(shares * 0.07 * price * (1 - price), 5)
    return {
        **row.to_dict(),
        "scenario": scenario,
        "seed": seed,
        "liquidity": "TAKER_APPROX",
        "fill_price": price,
        "submitted_limit": row.submitted_limit,
        "gross_strategy_pnl": gross,
        "fees": fee,
        "net_strategy_pnl": gross - fee,
        "price_improvement_vs_limit": shares * (float(row.submitted_limit) - price),
        "approximation": True,
    }


def stratified_select(pool: pd.DataFrame, count: int, seed: int) -> pd.DataFrame:
    if count <= 0 or pool.empty:
        return pool.iloc[0:0].copy()
    count = min(count, len(pool))
    strata = ["price_bucket", "tte_bucket", "edge_bucket"]
    groups = [(key, frame.copy()) for key, frame in pool.groupby(strata, observed=True)]
    quotas = []
    for key, frame in groups:
        exact = count * len(frame) / len(pool)
        quotas.append([key, frame, int(math.floor(exact)), exact % 1.0])
    remaining = count - sum(item[2] for item in quotas)
    quotas.sort(
        key=lambda item: (
            -item[3],
            hashlib.sha256(f"{seed}|{item[0]}".encode()).hexdigest(),
        )
    )
    for item in quotas[:remaining]:
        item[2] += 1
    selected = []
    for _, frame, quota, _ in quotas:
        ranked = frame.assign(
            _score=frame.apply(lambda row: stable_score(seed, row), axis=1)
        )
        selected.append(ranked.nsmallest(quota, "_score").drop(columns="_score"))
    return pd.concat(selected, ignore_index=True) if selected else pool.iloc[0:0].copy()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("TARGET",), default="TARGET")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--scenario", choices=("ALL", "T80", "T85", "T90"), default="ALL")
    p.add_argument("--c-edge", default="2.0,2.5,3.0,3.5,4.0,4.5,5.0")
    p.add_argument("--latency", default="175,225,325")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    del args.phase, args.workers
    scenarios = list(TARGETS) if args.scenario == "ALL" else [args.scenario]
    edges = [float(x) for x in args.c_edge.split(",")]
    latencies = [int(x) for x in args.latency.split(",")]
    for edge in edges:
        for latency in latencies:
            units = sorted(
                (
                    args.output_root
                    / "replay/scenario=E1"
                    / f"c_edge={edge:.1f}"
                    / f"latency={latency}"
                ).glob("date=*")
            )
            if len(units) != 32:
                raise RuntimeError(
                    f"E1 incomplete for C{edge}/{latency}: {len(units)}/32"
                )
            for unit in units:
                date = unit.name.removeprefix("date=")
                opp = buckets(pd.read_parquet(unit / "opportunities.parquet"))
                fills = pd.read_parquet(unit / "trades.parquet")
                filled = set(fills.polymarket_slug.astype(str)) if len(fills) else set()
                pool = opp.loc[~opp.polymarket_slug.astype(str).isin(filled)].copy()
                for scenario in scenarios:
                    dest = (
                        args.output_root
                        / "target_conversion"
                        / f"scenario={scenario}"
                        / f"c_edge={edge:.1f}"
                        / f"latency={latency}"
                        / f"date={date}"
                    )
                    marker = dest / "_COMPLETE"
                    if args.resume and marker.exists():
                        continue
                    dest.mkdir(parents=True, exist_ok=True)
                    rows = []
                    target_n = min(
                        len(opp), int(math.ceil(TARGETS[scenario] * len(opp)))
                    )
                    add_n = max(0, target_n - len(fills))
                    for seed in SEEDS:
                        selected = stratified_select(pool, add_n, seed)
                        added = pd.DataFrame(
                            [
                                make_added(r, scenario, seed)
                                for _, r in selected.iterrows()
                            ]
                        )
                        base = fills.copy()
                        if len(base):
                            base["scenario"], base["seed"], base["approximation"] = (
                                scenario,
                                seed,
                                False,
                            )
                        combined = pd.concat(
                            [base, added], ignore_index=True, sort=False
                        )
                        rows.append(
                            {
                                "scenario": scenario,
                                "seed": seed,
                                "date_utc": date,
                                "latency_ms": latency,
                                "c_edge_cents": edge,
                                "eligible_markets": len(opp),
                                "fills": len(combined),
                                "added_fills": len(added),
                                "gross_pnl": combined.gross_strategy_pnl.sum(),
                                "fees": combined.fees.sum(),
                                "net_pnl": combined.net_strategy_pnl.sum(),
                            }
                        )
                    pd.DataFrame(rows).to_parquet(
                        dest / "seed_metrics.parquet", index=False, compression="zstd"
                    )
                    marker.write_text("complete\n")
                    print(
                        f"DONE {scenario} C{edge:.1f} {latency} {date} target={target_n}",
                        flush=True,
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
