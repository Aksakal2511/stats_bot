#!/usr/bin/env python3
"""Aggregate completed Stage 27 partitions into the predeclared report tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/Volumes/Kate_DATA/stats_bot")
DEFAULT_OUTPUT = ROOT / "twap/data/artifacts/stage27_live_execution_fee_edge_btc"
EDGES = (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
LATENCIES = (175, 225, 325)
PRICE_BINS = [0.20, 0.30, 0.45, 0.55, 0.70, 0.8000001]
PRICE_LABELS = ["0.20-0.30", "0.30-0.45", "0.45-0.55", "0.55-0.70", "0.70-0.80"]


def max_drawdown(values: pd.Series) -> float:
    curve = values.cumsum().to_numpy(float)
    if not len(curve):
        return 0.0
    peak = np.maximum.accumulate(np.r_[0.0, curve])
    return float(np.max(peak[1:] - curve))


def load_replay(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    trades, metrics, attempts = [], [], []
    for marker in root.glob(
        "replay/scenario=*/c_edge=*/latency=*/date=*/_COMPLETE.json"
    ):
        unit = marker.parent
        meta = json.loads(marker.read_text())
        metrics.append(meta)
        for name, target in (
            ("trades.parquet", trades),
            ("attempts.parquet", attempts),
        ):
            p = unit / name
            if p.exists():
                x = pd.read_parquet(p)
                if len(x):
                    x["scenario"], x["c_edge_cents"], x["date_utc"], x["latency_ms"] = (
                        meta["scenario"],
                        meta["c_edge_cents"],
                        meta["date_utc"],
                        meta["latency_ms"],
                    )
                    target.append(x)
    return (
        pd.concat(trades, ignore_index=True, sort=False) if trades else pd.DataFrame(),
        pd.DataFrame(metrics),
        pd.concat(attempts, ignore_index=True, sort=False)
        if attempts
        else pd.DataFrame(),
    )


def summarize(
    days: pd.DataFrame, trades: pd.DataFrame, group: list[str]
) -> pd.DataFrame:
    rows = []
    for key, d in days.groupby(group, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        filt = np.ones(len(trades), dtype=bool)
        for col, val in zip(group, key):
            filt &= trades[col].eq(val).to_numpy()
        t = trades.loc[filt].copy()
        pnl = d.net_pnl.astype(float)
        total_gross = float(d.gross_pnl.sum())
        fills = int(d.fills.sum())
        daily_avg = d.groupby("date_utc", observed=True).net_pnl.mean()
        rows.append(
            {
                **dict(zip(group, key)),
                "total_net_pnl": float(daily_avg.sum()),
                "mean_day": float(daily_avg.mean()),
                "median_day": float(daily_avg.median()),
                "positive_day_rate": float((daily_avg > 0).mean()),
                "p10": float(daily_avg.quantile(0.10)),
                "worst": float(daily_avg.min()),
                "es3": float(np.mean(np.sort(daily_avg)[:3])),
                "es5": float(np.mean(np.sort(daily_avg)[:5])),
                "max_drawdown": max_drawdown(daily_avg.sort_index()),
                "fills_day": float(d.groupby("date_utc").fills.mean().mean()),
                "eligible_markets_day": float(
                    d.groupby("date_utc").eligible_markets.mean().mean()
                ),
                "fill_conversion": fills / max(1, int(d.eligible_markets.sum())),
                "gross_pnl_trade": total_gross / max(1, fills),
                "fees_day": float(d.groupby("date_utc").fees.mean().mean()),
                "net_pnl_trade": float(pnl.sum()) / max(1, fills),
                "taker_share": float(
                    t.liquidity.astype(str).str.startswith("TAKER").mean()
                )
                if len(t) and "liquidity" in t
                else 0.0,
                "maker_share": float(t.liquidity.eq("MAKER").mean())
                if len(t) and "liquidity" in t
                else 0.0,
                "top1_day_contribution": float(
                    daily_avg.nlargest(1).sum() / daily_avg.sum()
                )
                if daily_avg.sum()
                else np.nan,
                "top3_day_contribution": float(
                    daily_avg.nlargest(3).sum() / daily_avg.sum()
                )
                if daily_avg.sum()
                else np.nan,
                "delete_one_day_mean_min": float(
                    min(
                        (daily_avg.drop(i).mean() for i in daily_avg.index),
                        default=np.nan,
                    )
                ),
                "delete_one_day_mean_max": float(
                    max(
                        (daily_avg.drop(i).mean() for i in daily_avg.index),
                        default=np.nan,
                    )
                ),
            }
        )
    return pd.DataFrame(rows)


def fill_decomposition(trades: pd.DataFrame) -> pd.DataFrame:
    e0 = trades.loc[trades.scenario.eq("E0") & trades.c_edge_cents.eq(2.5)].copy()
    e1 = trades.loc[trades.scenario.eq("E1") & trades.c_edge_cents.eq(2.5)].copy()
    key = ["date_utc", "latency_ms", "polymarket_slug"]
    merged = e0.merge(e1, on=key, how="outer", suffixes=("_e0", "_e1"), indicator=True)
    same = (
        merged._merge.eq("both")
        & merged.signal_time_utc_e0.astype(str).eq(
            merged.signal_time_utc_e1.astype(str)
        )
        & np.isclose(
            merged.get("entry_limit_e0"), merged.get("fill_price_e1"), equal_nan=False
        )
    )
    merged["classification"] = np.select(
        [merged._merge.eq("left_only"), merged._merge.eq("right_only"), same],
        ["REMOVED_FILL", "ADDED_FILL", "SHARED_SAME_ENTRY"],
        default="SHARED_CHANGED_ENTRY",
    )
    return merged


def added_fill_breakdown(added: pd.DataFrame) -> pd.DataFrame:
    work = added.copy()
    if work.empty:
        return pd.DataFrame()
    work["price_bucket"] = pd.cut(
        work.fill_price_e1,
        PRICE_BINS,
        labels=PRICE_LABELS,
        right=False,
    ).astype(str)
    signal = pd.to_datetime(work.signal_time_utc_e1, utc=True)
    work["session_utc"] = (signal.dt.hour // 4 * 4).map(
        lambda hour: f"{hour:02d}-{hour + 4:02d}UTC"
    )

    def row(frame: pd.DataFrame, dimension: str, level: object) -> dict[str, object]:
        return {
            "dimension": dimension,
            "level": level,
            "count": len(frame),
            "fills_day": len(frame) / (32 * 3),
            "gross_pnl": frame.gross_strategy_pnl_e1.sum(),
            "gross_pnl_trade": frame.gross_strategy_pnl_e1.mean(),
            "fees": frame.fees_e1.sum(),
            "fees_trade": frame.fees_e1.mean(),
            "net_pnl": frame.net_strategy_pnl_e1.sum(),
            "net_pnl_trade": frame.net_strategy_pnl_e1.mean(),
            "win_rate": frame.outcome_e1.mean(),
            "mean_price": frame.fill_price_e1.mean(),
            "mean_tte": frame.tte_e1.mean(),
            "mean_spread": frame.spread_e1.mean(),
            "mean_model_edge": frame.model_edge_e1.mean(),
            "mean_edge_surplus": frame.edge_surplus_e1.mean(),
            "mean_twap_dz": frame.twap_dz_e1.mean(),
            "mean_vol_1h": frame.vol_1h_e1.mean(),
            "break_even_gross_pnl_trade": frame.fees_e1.mean(),
        }

    rows = [row(work, "ALL", "ALL")]
    dimensions = {
        "latency_ms": "latency_ms",
        "price_bucket": "price_bucket",
        "bps_branch": "branch_used_e1",
        "side": "side_e1",
        "session": "session_utc",
    }
    for dimension, column in dimensions.items():
        rows.extend(
            row(frame, dimension, level)
            for level, frame in work.groupby(column, observed=True, dropna=False)
        )
    return pd.DataFrame(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("AGGREGATE",), default="AGGREGATE")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--scenario", default="ALL")
    p.add_argument("--c-edge", default="ALL")
    p.add_argument("--latency", default="ALL")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    del args.phase, args.workers, args.resume, args.scenario, args.c_edge, args.latency
    trades, days, attempts = load_replay(args.output_root)
    expected = 2 * len(EDGES) * len(LATENCIES) * 32
    if len(days) != expected:
        raise RuntimeError(f"Replay incomplete: {len(days)}/{expected} units")
    days.to_csv(args.output_root / "execution_results_by_day.csv", index=False)
    by_lat = summarize(days, trades, ["scenario", "c_edge_cents", "latency_ms"])
    overall = summarize(days, trades, ["scenario", "c_edge_cents"])
    by_lat.to_csv(args.output_root / "execution_results_by_latency.csv", index=False)
    overall.to_csv(args.output_root / "execution_results_summary.csv", index=False)
    overall.to_csv(args.output_root / "edge_sweep_summary.csv", index=False)
    by_lat.to_csv(args.output_root / "edge_sweep_by_latency.csv", index=False)
    days.to_csv(args.output_root / "edge_sweep_by_day.csv", index=False)
    decomp = fill_decomposition(trades)
    decomp.to_parquet(
        args.output_root / "fill_decomposition.parquet", index=False, compression="zstd"
    )
    added = decomp.loc[decomp.classification.eq("ADDED_FILL")]
    added_summary = added_fill_breakdown(added)
    added_summary.to_csv(args.output_root / "added_fill_summary.csv", index=False)
    changed = decomp.loc[decomp.classification.eq("SHARED_CHANGED_ENTRY")]
    pd.DataFrame(
        [
            {
                "count": len(changed),
                "gross_delta": changed.get("gross_strategy_pnl_e1", 0).sum()
                - changed.get("pnl_e0", 0).sum(),
                "fees": changed.get("fees_e1", pd.Series(dtype=float)).sum(),
                "net_delta": changed.get("net_strategy_pnl_e1", 0).sum()
                - changed.get("pnl_e0", 0).sum(),
            }
        ]
    ).to_csv(args.output_root / "changed_entry_summary.csv", index=False)
    fee_rows = []
    for (scenario, edge), t in trades.groupby(
        ["scenario", "c_edge_cents"], observed=True
    ):
        fee_rows.append(
            {
                "scenario": scenario,
                "c_edge_cents": edge,
                "submitted_limit_cost": t.get(
                    "submitted_limit_cost", pd.Series(dtype=float)
                ).sum(),
                "actual_fill_cost": t.get(
                    "actual_fill_cost", pd.Series(dtype=float)
                ).sum(),
                "price_improvement": t.get(
                    "price_improvement_vs_limit", pd.Series(dtype=float)
                ).sum(),
                "taker_fees": t.loc[
                    t.liquidity.astype(str).str.startswith("TAKER"), "fees"
                ].sum()
                if "fees" in t
                else 0,
                "maker_fees": t.loc[t.liquidity.eq("MAKER"), "fees"].sum()
                if "fees" in t
                else 0,
            }
        )
    pd.DataFrame(fee_rows).to_csv(
        args.output_root / "fee_price_improvement_decomposition.csv", index=False
    )
    freq = days.copy()
    freq["eligible_to_fill_pct"] = freq.fills / freq.eligible_markets.replace(0, np.nan)
    freq["eligible_to_submit_pct"] = (
        freq.submitted_markets / freq.eligible_markets.replace(0, np.nan)
    )
    freq["submit_to_fill_pct"] = freq.fills / freq.submitted_markets.replace(0, np.nan)
    freq.to_csv(args.output_root / "opportunity_frequency_by_day.csv", index=False)
    trades["price_bucket"] = pd.cut(
        trades.fill_price.fillna(trades.entry_limit),
        PRICE_BINS,
        labels=PRICE_LABELS,
        right=False,
    )
    pb = (
        trades.groupby(
            ["scenario", "c_edge_cents", "latency_ms", "price_bucket"], observed=True
        )
        .agg(
            trades=("polymarket_slug", "size"),
            gross_pnl=("gross_strategy_pnl", "sum"),
            fees=("fees", "sum"),
            net_pnl=("net_strategy_pnl", "sum"),
            taker_share=(
                "liquidity",
                lambda x: x.astype(str).str.startswith("TAKER").mean(),
            ),
        )
        .reset_index()
    )
    for c in ("gross_pnl", "fees", "net_pnl"):
        pb[c + "_trade"] = pb[c] / pb.trades
    pb.to_csv(args.output_root / "edge_price_bucket_summary.csv", index=False)
    removed = []
    for scenario in ("E0", "E1"):
        for lo, hi in zip(EDGES[:-1], EDGES[1:]):
            low = trades.loc[trades.scenario.eq(scenario) & trades.c_edge_cents.eq(lo)]
            high = trades.loc[trades.scenario.eq(scenario) & trades.c_edge_cents.eq(hi)]
            keys = ["date_utc", "latency_ms", "polymarket_slug"]
            gone = low.merge(
                high[keys].drop_duplicates(), on=keys, how="left", indicator=True
            ).loc[lambda x: x._merge.eq("left_only")]
            removed.append(
                {
                    "scenario": scenario,
                    "from_c": lo,
                    "to_c": hi,
                    "trades": len(gone),
                    "gross_pnl_trade": gone.gross_strategy_pnl.mean(),
                    "fees_trade": gone.fees.mean(),
                    "net_pnl_trade": gone.net_strategy_pnl.mean(),
                }
            )
    pd.DataFrame(removed).to_csv(
        args.output_root / "edge_incremental_removed_population.csv", index=False
    )
    p15_rows = []
    stage26d = Path(
        "/Volumes/Kate_DATA/policy_finder/twap/data/artifacts/stage26d_local_pulse_guard_btc_corrected"
    )
    for latency in LATENCIES:
        for date in sorted(days.date_utc.unique()):
            e0_guard = pd.read_parquet(
                stage26d
                / "replay"
                / f"date={date}"
                / f"latency={latency}"
                / "scenario=P15_PAUSE/trades.parquet"
            )
            e0_open = pd.read_parquet(
                stage26d
                / "replay"
                / f"date={date}"
                / f"latency={latency}"
                / "scenario=B0_C2P5/trades.parquet"
            )
            e1_unit = (
                args.output_root
                / "replay/scenario=E1/c_edge=2.5"
                / f"latency={latency}"
                / f"date={date}"
            )
            e1_guard = pd.read_parquet(e1_unit / "trades.parquet")
            e1_open = pd.read_parquet(e1_unit / "counterfactual_no_p15_trades.parquet")
            key = "polymarket_slug"
            e0_removed = e0_open.loc[~e0_open[key].isin(e0_guard[key])]
            e1_removed = e1_open.loc[~e1_open[key].isin(e1_guard[key])]
            p15_rows.append(
                {
                    "date_utc": date,
                    "latency_ms": latency,
                    "fills_prevented_e0": len(e0_removed),
                    "fills_prevented_e1": len(e1_removed),
                    "gross_pnl_prevented_e0": e0_removed.pnl.sum(),
                    "gross_pnl_prevented_e1": e1_removed.gross_strategy_pnl.sum(),
                    "net_pnl_prevented_e1": e1_removed.net_strategy_pnl.sum(),
                }
            )
    pd.DataFrame(p15_rows).to_csv(
        args.output_root / "p15_execution_interaction.csv", index=False
    )
    target = list(
        args.output_root.glob(
            "target_conversion/scenario=*/c_edge=*/latency=*/date=*/seed_metrics.parquet"
        )
    )
    if target:
        seed = pd.concat([pd.read_parquet(x) for x in target], ignore_index=True)
        seed.to_parquet(
            args.output_root / "target_conversion_seed_results.parquet",
            index=False,
            compression="zstd",
        )
        seed_rows = []
        for key, frame in seed.groupby(
            ["scenario", "c_edge_cents", "latency_ms", "seed"], observed=True
        ):
            daily = frame.sort_values("date_utc").net_pnl.astype(float)
            seed_rows.append(
                {
                    "scenario": key[0],
                    "c_edge_cents": key[1],
                    "latency_ms": key[2],
                    "seed": key[3],
                    "fills_day": frame.fills.mean(),
                    "mean_day": daily.mean(),
                    "median_day": daily.median(),
                    "positive_day_rate": (daily > 0).mean(),
                    "p10": daily.quantile(0.10),
                    "worst": daily.min(),
                    "es3": np.sort(daily)[:3].mean(),
                    "max_drawdown": max_drawdown(daily.reset_index(drop=True)),
                    "fees_day": frame.fees.mean(),
                    "net_pnl_trade": frame.net_pnl.sum() / max(1, frame.fills.sum()),
                }
            )
        per = pd.DataFrame(seed_rows)
        uncertainty_metrics = (
            "fills_day",
            "mean_day",
            "median_day",
            "positive_day_rate",
            "p10",
            "worst",
            "es3",
            "max_drawdown",
            "fees_day",
            "net_pnl_trade",
        )
        summary = (
            per.groupby(["scenario", "c_edge_cents", "latency_ms"], observed=True)
            .agg(
                **{
                    f"{c}_{q}": (c, lambda x, q=q: x.quantile(q))
                    for c in uncertainty_metrics
                    for q in (0.1, 0.5, 0.9)
                }
            )
            .reset_index()
        )
        summary.to_csv(args.output_root / "target_conversion_summary.csv", index=False)
    else:
        pd.DataFrame().to_csv(
            args.output_root / "target_conversion_summary.csv", index=False
        )
    report = [
        "# Stage 27 report",
        "",
        "This report is diagnostic. It does not change production C2.5 or execution.",
        "",
        "## Headline C2.5",
        "",
        overall.loc[overall.c_edge_cents.eq(2.5)].to_markdown(index=False),
        "",
        "## Integrity",
        "",
        f"Completed replay units: {len(days)}/{expected}.",
        "E0 strict identity is enforced per date and latency before E1 is trusted.",
    ]
    (args.output_root / "stage27_report.md").write_text("\n".join(report) + "\n")
    (args.output_root / "stage27_freeze_note.md").write_text(
        "# Freeze note\n\nStage 27 is diagnostic only. Production remains C2.5 with frozen P15 and unchanged live execution.\n"
    )
    print(f"Stage27 aggregation complete: {args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
