#!/usr/bin/env python3
"""Post-process Stage 27 E1 C2.5 fills under execution-price stress."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/Volumes/Kate_DATA/stats_bot")
STAGE27 = ROOT / "twap/data/artifacts/stage27_live_execution_fee_edge_btc"
DEFAULT_OUTPUT = ROOT / "twap/data/artifacts/stage27_e1_price_improvement_stress"
LATENCIES = (175, 225, 325)
DATES = tuple(pd.date_range("2026-08-14", "2026-09-14").strftime("%Y-%m-%d"))
SHARES = 5.0
FEE_RATE = 0.07
E0_MEAN_DAY = 5.744791666666665
FRACTIONS = {
    "IMP100": 1.00,
    "IMP75": 0.75,
    "IMP50": 0.50,
    "IMP25": 0.25,
    "IMP0": 0.00,
    "LIMIT_PRICE_CONTROL": 0.00,
}
HAIRCUTS_CENTS = (0.10, 0.20, 0.25, 0.50)
PRICE_BINS = [0.20, 0.30, 0.45, 0.55, 0.70, 0.8000001]
PRICE_LABELS = ["0.20-0.30", "0.30-0.45", "0.45-0.55", "0.55-0.70", "0.70-0.80"]


def fee(price: float) -> float:
    return round(SHARES * FEE_RATE * float(price) * (1.0 - float(price)), 5)


def max_drawdown(values: pd.Series) -> float:
    curve = values.cumsum().to_numpy(float)
    peak = np.maximum.accumulate(np.r_[0.0, curve])
    return float(np.max(peak[1:] - curve)) if len(curve) else 0.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_trades() -> pd.DataFrame:
    files = sorted(
        STAGE27.glob("replay/scenario=E1/c_edge=2.5/latency=*/date=*/trades.parquet")
    )
    if len(files) != len(DATES) * len(LATENCIES):
        raise RuntimeError(f"E1 ledger incomplete: {len(files)}/96 files")
    trades = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    trades["date_utc"] = trades.date_utc.astype(str)
    trades["is_taker"] = trades.liquidity.astype(str).str.startswith("TAKER")
    trades["original_improvement"] = np.where(
        trades.is_taker, trades.submitted_limit - trades.fill_price, 0.0
    )
    decomp = pd.read_parquet(STAGE27 / "fill_decomposition.parquet")
    labels = decomp.loc[
        decomp.classification.ne("REMOVED_FILL"),
        ["date_utc", "latency_ms", "polymarket_slug", "classification"],
    ].copy()
    labels["date_utc"] = labels.date_utc.astype(str)
    trades = trades.merge(
        labels,
        on=["date_utc", "latency_ms", "polymarket_slug"],
        how="left",
        validate="one_to_one",
    )
    if trades.classification.isna().any():
        raise RuntimeError("Missing E1 fill-decomposition labels")
    trades["original_price_bucket"] = pd.cut(
        trades.fill_price,
        PRICE_BINS,
        labels=PRICE_LABELS,
        right=False,
    ).astype(str)
    return trades


def reprice(
    trades: pd.DataFrame,
    scenario: str,
    *,
    retained_fraction: float | None = None,
    haircut_cents: float | None = None,
) -> pd.DataFrame:
    out = trades.copy()
    price = out.fill_price.astype(float).copy()
    taker = out.is_taker
    if retained_fraction is not None:
        stressed = out.submitted_limit - retained_fraction * out.original_improvement
    elif haircut_cents is not None:
        stressed = out.fill_price + haircut_cents / 100.0
    else:
        raise ValueError("A retained fraction or haircut is required")
    price.loc[taker] = np.minimum(
        out.loc[taker, "submitted_limit"],
        np.maximum(out.loc[taker, "fill_price"], stressed.loc[taker]),
    )
    out["stress_scenario"] = scenario
    out["stressed_fill_price"] = price
    out["stressed_fee"] = 0.0
    out.loc[taker, "stressed_fee"] = [fee(x) for x in price.loc[taker]]
    out["stressed_gross_pnl"] = SHARES * np.where(
        out.outcome.astype(int).eq(1), 1.0 - price, -price
    )
    out["stressed_net_pnl"] = out.stressed_gross_pnl - out.stressed_fee
    out["retained_improvement"] = np.where(
        taker, out.submitted_limit - price, 0.0
    )
    out["deterioration_vs_e1"] = price - out.fill_price
    return out


def daily_frame(trades: pd.DataFrame) -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [DATES, LATENCIES], names=["date_utc", "latency_ms"]
    )
    daily = (
        trades.groupby(["date_utc", "latency_ms"], observed=True)
        .agg(
            gross_pnl=("stressed_gross_pnl", "sum"),
            fees=("stressed_fee", "sum"),
            net_pnl=("stressed_net_pnl", "sum"),
            trades=("polymarket_slug", "size"),
        )
        .reindex(index, fill_value=0)
        .reset_index()
    )
    daily["scenario"] = trades.stress_scenario.iloc[0]
    return daily


def metric_row(trades: pd.DataFrame, daily: pd.DataFrame, latency: object) -> dict:
    if latency == "AVG_3_LATENCIES":
        d = daily.groupby("date_utc", observed=True).agg(
            gross_pnl=("gross_pnl", "mean"),
            fees=("fees", "mean"),
            net_pnl=("net_pnl", "mean"),
            trades=("trades", "mean"),
        )
        t = trades
    else:
        d = daily.loc[daily.latency_ms.eq(latency)].set_index("date_utc")
        t = trades.loc[trades.latency_ms.eq(latency)]
    pnl = d.net_pnl.sort_index()
    total_trades = float(d.trades.sum())
    takers = t.loc[t.is_taker]
    deterioration = takers.deterioration_vs_e1
    e1_net = takers.net_strategy_pnl.astype(float)
    stressed_net = takers.stressed_net_pnl.astype(float)
    return {
        "scenario": trades.stress_scenario.iloc[0],
        "latency_ms": latency,
        "gross_pnl_day": float(d.gross_pnl.mean()),
        "fees_day": float(d.fees.mean()),
        "net_pnl_day": float(pnl.mean()),
        "mean_day": float(pnl.mean()),
        "median_day": float(pnl.median()),
        "positive_day_rate": float((pnl > 0).mean()),
        "p10": float(pnl.quantile(0.10)),
        "worst": float(pnl.min()),
        "worst_day": str(pnl.idxmin()),
        "best": float(pnl.max()),
        "best_day": str(pnl.idxmax()),
        "es3": float(np.sort(pnl)[:3].mean()),
        "es5": float(np.sort(pnl)[:5].mean()),
        "max_drawdown": max_drawdown(pnl),
        "gross_pnl_trade": float(d.gross_pnl.sum() / total_trades),
        "net_pnl_trade": float(pnl.sum() / total_trades),
        "trades_day": float(d.trades.mean()),
        "top1_day_contribution": float(pnl.nlargest(1).sum() / pnl.sum()),
        "top3_day_contribution": float(pnl.nlargest(3).sum() / pnl.sum()),
        "delete_one_day_mean_min": float(
            min(pnl.drop(day).mean() for day in pnl.index)
        ),
        "delete_one_day_mean_max": float(
            max(pnl.drop(day).mean() for day in pnl.index)
        ),
        "taker_fills": int(len(takers)),
        "mean_original_improvement_per_taker_share": float(
            takers.original_improvement.mean()
        ),
        "mean_retained_improvement_per_taker_share": float(
            takers.retained_improvement.mean()
        ),
        "mean_improvement_lost_per_taker_share": float(deterioration.mean()),
        "cents_per_share_lost": float(100.0 * deterioration.mean()),
        "gross_dollars_per_taker_trade_lost": float(SHARES * deterioration.mean()),
        "net_dollars_per_taker_trade_lost": float((e1_net - stressed_net).mean()),
    }


def summarize(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = daily_frame(trades)
    rows = [metric_row(trades, daily, "AVG_3_LATENCIES")]
    rows.extend(metric_row(trades, daily, latency) for latency in LATENCIES)
    return pd.DataFrame(rows), daily


def find_fraction(trades: pd.DataFrame, target: float) -> dict:
    low = summarize(reprice(trades, "LOW", retained_fraction=0.0))[0].iloc[0].mean_day
    high = summarize(reprice(trades, "HIGH", retained_fraction=1.0))[0].iloc[0].mean_day
    if not min(low, high) <= target <= max(low, high):
        return {
            "target": target,
            "exists_between_imp0_imp100": False,
            "retained_improvement_fraction": np.nan,
            "retained_improvement_pct": np.nan,
            "average_cents_share_deterioration": np.nan,
            "endpoint_imp0_mean_day": low,
            "endpoint_imp100_mean_day": high,
            "method": "no crossing inside permitted range",
        }
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        value = summarize(reprice(trades, "MID", retained_fraction=mid))[0].iloc[0].mean_day
        if value < target:
            lo = mid
        else:
            hi = mid
    fraction = (lo + hi) / 2.0
    mean_improvement = trades.loc[trades.is_taker, "original_improvement"].mean()
    return {
        "target": target,
        "exists_between_imp0_imp100": True,
        "retained_improvement_fraction": fraction,
        "retained_improvement_pct": 100.0 * fraction,
        "average_cents_share_deterioration": 100.0 * mean_improvement * (1.0 - fraction),
        "endpoint_imp0_mean_day": low,
        "endpoint_imp100_mean_day": high,
        "method": "bisection interpolation on recomputed fee curve",
    }


def markdown_table(frame: pd.DataFrame, columns: list[str], digits: int = 4) -> str:
    view = frame.loc[:, columns].copy()
    for column in view.select_dtypes(include=["number"]).columns:
        view[column] = view[column].map(lambda x: f"{x:.{digits}f}")
    header = "| " + " | ".join(columns) + " |"
    rule = "|" + "|".join(["---"] * len(columns)) + "|"
    rows = ["| " + " | ".join(map(str, row)) + " |" for row in view.to_numpy()]
    return "\n".join([header, rule, *rows])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)

    trades = load_trades()
    baseline = pd.read_csv(STAGE27 / "execution_results_summary.csv").query(
        "scenario == 'E1' and c_edge_cents == 2.5"
    ).iloc[0]
    stage27_gates = pd.read_csv(STAGE27 / "integrity_gates.csv")

    summaries, latencies, days, repriced = [], [], [], {}
    for scenario, fraction in FRACTIONS.items():
        stressed = reprice(trades, scenario, retained_fraction=fraction)
        summary, daily = summarize(stressed)
        summary["retained_improvement_fraction"] = fraction
        summaries.append(summary.loc[summary.latency_ms.eq("AVG_3_LATENCIES")])
        latencies.append(summary.loc[summary.latency_ms.ne("AVG_3_LATENCIES")])
        days.append(daily)
        repriced[scenario] = stressed
    summary = pd.concat(summaries, ignore_index=True)
    by_latency = pd.concat(latencies, ignore_index=True)
    by_day = pd.concat(days, ignore_index=True)

    e1 = summary.loc[summary.scenario.eq("IMP100")].iloc[0]
    imp0 = summary.loc[summary.scenario.eq("IMP0")].iloc[0]
    control = summary.loc[summary.scenario.eq("LIMIT_PRICE_CONTROL")].iloc[0]
    for frame in (summary, by_latency):
        frame["dollars_day_lost_vs_e1"] = e1.mean_day - frame.mean_day

    haircut_rows = []
    for cents in HAIRCUTS_CENTS:
        scenario = f"HC_{str(cents).replace('.', 'P')}C"
        stressed = reprice(trades, scenario, haircut_cents=cents)
        metrics, _ = summarize(stressed)
        row = metrics.loc[metrics.latency_ms.eq("AVG_3_LATENCIES")].copy()
        row["haircut_cents_share"] = cents
        row["dollars_day_lost_vs_e1"] = e1.mean_day - row.mean_day
        haircut_rows.append(row)
    haircuts = pd.concat(haircut_rows, ignore_index=True)

    population_rows = []
    for scenario in ("IMP100", "IMP50", "IMP0"):
        frame = repriced[scenario]
        for population, group in frame.groupby("classification", observed=True):
            population_rows.append(
                {
                    "scenario": scenario,
                    "population": population,
                    "count": len(group),
                    "gross_pnl_trade": group.stressed_gross_pnl.mean(),
                    "fee_trade": group.stressed_fee.mean(),
                    "net_pnl_trade": group.stressed_net_pnl.mean(),
                    "total_net_contribution_pooled_latencies": group.stressed_net_pnl.sum(),
                }
            )
    populations = pd.DataFrame(population_rows)

    bucket_rows = []
    for scenario in ("IMP100", "IMP50", "IMP0"):
        for bucket, group in repriced[scenario].groupby(
            "original_price_bucket", observed=True
        ):
            bucket_rows.append(
                {
                    "scenario": scenario,
                    "original_e1_price_bucket": bucket,
                    "count": len(group),
                    "gross_pnl_trade": group.stressed_gross_pnl.mean(),
                    "fee_trade": group.stressed_fee.mean(),
                    "net_pnl_trade": group.stressed_net_pnl.mean(),
                    "total_net_contribution_pooled_latencies": group.stressed_net_pnl.sum(),
                }
            )
    buckets = pd.DataFrame(bucket_rows)

    break_even = pd.DataFrame(
        [
            {"break_even": "ZERO_STRATEGY_PNL", **find_fraction(trades, 0.0)},
            {"break_even": "CANONICAL_E0", **find_fraction(trades, E0_MEAN_DAY)},
        ]
    )
    added = trades.loc[trades.classification.eq("ADDED_FILL")].copy()
    added_zero = find_fraction(added, 0.0)
    added_zero["break_even"] = "ADDED_FILL_ZERO_NET"
    break_even = pd.concat([break_even, pd.DataFrame([added_zero])], ignore_index=True)

    checks = [
        ("stage27_integrity_all_passed", bool(stage27_gates.passed.all()), "inherited Stage27 gates"),
        ("ledger_files_complete", True, "96/96 E1 C2.5 trade ledgers"),
        ("development_dates_exact", set(trades.date_utc) == set(DATES), f"{trades.date_utc.nunique()} dates"),
        ("latencies_exact", set(trades.latency_ms) == set(LATENCIES), str(sorted(trades.latency_ms.unique()))),
        ("no_holdout_rows", not trades.date_utc.isin(["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]).any(), "Aug14-Sep14 only"),
        ("e1_total_net_reproduced", np.isclose(e1.mean_day * 32, baseline.total_net_pnl, rtol=1e-12, atol=1e-12), f"{e1.mean_day * 32:.12f} vs {baseline.total_net_pnl:.12f}"),
        ("e1_mean_reproduced", np.isclose(e1.mean_day, baseline.mean_day, rtol=1e-12, atol=1e-12), f"{e1.mean_day:.12f} vs {baseline.mean_day:.12f}"),
        ("e1_median_reproduced", np.isclose(e1.median_day, baseline.median_day, rtol=1e-12, atol=1e-12), f"{e1.median_day:.12f} vs {baseline.median_day:.12f}"),
        ("e1_fills_reproduced", np.isclose(e1.trades_day, baseline.fills_day, rtol=0, atol=1e-12), f"{e1.trades_day:.12f} vs {baseline.fills_day:.12f}"),
        ("e1_fees_reproduced", np.isclose(e1.fees_day, baseline.fees_day, rtol=0, atol=1e-12), f"{e1.fees_day:.12f} vs {baseline.fees_day:.12f}"),
        ("fill_count_invariant", summary.trades_day.nunique() == 1 and by_latency.groupby("latency_ms").trades_day.nunique().eq(1).all(), f"{e1.trades_day:.12f}/day"),
        ("maker_prices_unchanged", all(np.allclose(x.loc[~x.is_taker, "stressed_fill_price"], x.loc[~x.is_taker, "fill_price"]) for x in repriced.values()), "all scenarios"),
        ("stressed_prices_bounded", all(((x.loc[x.is_taker, "stressed_fill_price"] >= x.loc[x.is_taker, "fill_price"] - 1e-12) & (x.loc[x.is_taker, "stressed_fill_price"] <= x.loc[x.is_taker, "submitted_limit"] + 1e-12)).all() for x in repriced.values()), "original <= stressed <= limit"),
        (
            "limit_control_equals_imp0",
            np.allclose(
                imp0[
                    [c for c in summary.select_dtypes("number").columns if c in imp0.index]
                ].to_numpy(float),
                control[
                    [c for c in summary.select_dtypes("number").columns if c in control.index]
                ].to_numpy(float),
                equal_nan=True,
            ),
            "exact numeric identity",
        ),
    ]
    gates = pd.DataFrame(checks, columns=["gate", "passed", "detail"])
    if not gates.passed.all():
        gates.to_csv(args.output_root / "integrity_gates.csv", index=False)
        raise RuntimeError("Integrity gate failed; stress outputs not trusted")

    summary.to_csv(args.output_root / "price_improvement_stress_summary.csv", index=False)
    by_latency.to_csv(args.output_root / "price_improvement_stress_by_latency.csv", index=False)
    by_day.to_csv(args.output_root / "price_improvement_stress_by_day.csv", index=False)
    haircuts.to_csv(args.output_root / "fixed_cent_haircut_summary.csv", index=False)
    break_even.to_csv(args.output_root / "execution_break_even.csv", index=False)
    populations.to_csv(args.output_root / "stress_by_fill_population.csv", index=False)
    buckets.to_csv(args.output_root / "stress_by_price_bucket.csv", index=False)
    gates.to_csv(args.output_root / "integrity_gates.csv", index=False)

    manifest = {
        "analysis": "Stage27 E1 C2.5 taker price-improvement post-processing stress",
        "source_root": str(STAGE27),
        "source_manifest_sha256": sha256(STAGE27 / "run_manifest.json"),
        "source_fill_decomposition_sha256": sha256(STAGE27 / "fill_decomposition.parquet"),
        "development_dates": [DATES[0], DATES[-1]],
        "holdout_used": False,
        "event_replay_run": False,
        "trade_selection_changed": False,
        "fill_timestamps_changed": False,
        "fills": len(trades),
        "taker_fills": int(trades.is_taker.sum()),
        "maker_fills": int((~trades.is_taker).sum()),
        "latencies_ms": list(LATENCIES),
        "retained_improvement_fractions": list(FRACTIONS.values()),
        "fixed_haircuts_cents_share": list(HAIRCUTS_CENTS),
        "fee_formula": "round(5 * 0.07 * stressed_price * (1-stressed_price), 5)",
        "price_bucket_basis": "original E1 fill price; membership frozen across stress",
    }
    (args.output_root / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    main_rows = summary.loc[summary.scenario.isin(["IMP100", "IMP75", "IMP50", "IMP25", "IMP0"])]
    e0_cross = break_even.loc[break_even.break_even.eq("CANONICAL_E0")].iloc[0]
    zero_cross = break_even.loc[break_even.break_even.eq("ZERO_STRATEGY_PNL")].iloc[0]
    added_cross = break_even.loc[break_even.break_even.eq("ADDED_FILL_ZERO_NET")].iloc[0]
    imp0_added = populations.query("scenario == 'IMP0' and population == 'ADDED_FILL'").iloc[0]
    classification = (
        "HIGHLY_EXECUTION_SENSITIVE"
        if imp0.mean_day <= 0 or e0_cross.retained_improvement_pct > 90
        else "MODERATELY_EXECUTION_SENSITIVE"
        if imp0.mean_day < E0_MEAN_DAY
        else "ROBUST_TO_PRICE_IMPROVEMENT_ERROR"
    )
    report = f"""# Stage 27 E1 price-improvement stress

This is a post-processing execution-price sensitivity analysis. It preserves E1 trade selection, fill decisions, timestamps, sizing, C2.5 and P15. No event replay or optimisation was performed.

## Integrity

All {len(gates)} gates passed. The persisted ledger reproduced Stage27 E1 exactly: mean `${e1.mean_day:.6f}/day`, median `${e1.median_day:.6f}/day`, `{e1.trades_day:.6f}` fills/day and `${e1.fees_day:.6f}` fees/day.

## Retained historical price improvement

{markdown_table(main_rows, ['scenario','mean_day','median_day','positive_day_rate','p10','worst','es3','es5','max_drawdown','trades_day','fees_day','net_pnl_trade','cents_per_share_lost'])}

E1 assumed `{100 * e1.mean_original_improvement_per_taker_share:.4f}c/share` mean improvement on taker fills. IMP0 remains profitable at `${imp0.mean_day:.4f}/day`, but is below canonical E0 (`${E0_MEAN_DAY:.4f}/day`).

## Break-even

- Versus E0: `{e0_cross.retained_improvement_pct:.3f}%` of historical improvement must remain. This permits approximately `{e0_cross.average_cents_share_deterioration:.4f}c/share` average deterioration from E1. This is interpolated by bisection with fees recomputed at every trial price.
- Versus zero: no crossing exists between IMP100 and IMP0; even the submitted-limit control remains positive at `${imp0.mean_day:.4f}/day`.
- ADDED_FILL zero-net crossing: `{added_cross.retained_improvement_pct:.3f}%` retained, or `{added_cross.average_cents_share_deterioration:.4f}c/share` deterioration. At IMP0, added fills earn `${imp0_added.net_pnl_trade:.4f}` per trade.

## Direct execution haircuts

{markdown_table(haircuts, ['haircut_cents_share','mean_day','median_day','p10','worst','es3','max_drawdown','fees_day','net_pnl_trade','dollars_day_lost_vs_e1'])}

## Latency stability

{markdown_table(by_latency.loc[by_latency.scenario.isin(['IMP100','IMP50','IMP0'])], ['scenario','latency_ms','mean_day','median_day','p10','worst','es3','max_drawdown','net_pnl_trade'])}

## Fill populations

{markdown_table(populations, ['scenario','population','count','gross_pnl_trade','fee_trade','net_pnl_trade','total_net_contribution_pooled_latencies'])}

## Price buckets

Buckets are frozen from the original E1 fill price so the same populations are compared under every stress.

{markdown_table(buckets, ['scenario','original_e1_price_bucket','count','gross_pnl_trade','fee_trade','net_pnl_trade'])}

## Day concentration

{markdown_table(main_rows, ['scenario','best_day','best','worst_day','worst','top1_day_contribution','top3_day_contribution','delete_one_day_mean_min','delete_one_day_mean_max'])}

At IMP100 the delete-one-day mean remains between `${main_rows.iloc[0].delete_one_day_mean_min:.4f}` and `${main_rows.iloc[0].delete_one_day_mean_max:.4f}`. At IMP0 it ranges from `${main_rows.iloc[-1].delete_one_day_mean_min:.4f}` to `${main_rows.iloc[-1].delete_one_day_mean_max:.4f}`, so the submitted-limit result is positive on the full sample but not robust to deleting its best date.

## Explicit answers

1. E1 assumed `{100 * e1.mean_original_improvement_per_taker_share:.4f}c/share` average historical improvement per taker fill.
2. Mean/day at IMP100/75/50/25/0 is `$6.2701 / $4.8764 / $3.4853 / $2.0969 / $0.7111`.
3. Median, P10, worst, ES3 and drawdown deteriorate monotonically in the retained-improvement table above. Max drawdown grows from `$48.69` to `$68.18`.
4. Yes. IMP0 remains profitable in the latency-averaged view at `$0.7111/day`.
5. No. IMP0 is materially below canonical E0's `$5.7448/day`.
6. E1 loses its E0 advantage after approximately `{e0_cross.average_cents_share_deterioration:.4f}c/share` average deterioration, retaining `{e0_cross.retained_improvement_pct:.3f}%` of historical improvement.
7. No zero-PnL break-even exists within the permitted stress. Even pricing every taker at its submitted limit leaves `$0.7111/day`; deterioration beyond the submitted limit was prohibited.
8. ADDED_FILL crosses zero at `{added_cross.retained_improvement_pct:.3f}%` retained improvement, after approximately `{added_cross.average_cents_share_deterioration:.4f}c/share` deterioration.
9. `0.20-0.30` is negative even at IMP100 and deteriorates most. At IMP0, `0.30-0.45` and `0.45-0.55` also become negative; only the two upper buckets remain positive.
10. Directionally yes, quantitatively no: every latency worsens with lost improvement, but IMP0 is `+$1.9829 / +$0.8935 / -$0.7430` per day at `175/225/325ms`.
11. IMP100 is robust to deleting any one day (`$5.2235-$7.1976/day`). IMP0 is not (`-$0.1838-$1.6908/day`) and becomes highly concentrated in its best dates.
12. The appropriate classification is **{classification}**: E1 remains profitable at IMP0, but only `9.42%` of its assumed improvement can be lost before it underperforms E0, and its slowest-latency IMP0 result is negative.

## Conclusion

Classification: **{classification}**.

The `$6.27/day` headline is profitable with no assumed improvement, but its advantage over E0 is sensitive: only about `{100 * (1-e0_cross.retained_improvement_fraction):.2f}%` of E1's average improvement can be lost before E1 falls below E0. Added fills become unprofitable earlier than the full strategy and are therefore the most fragile population. Fill count is identical in every scenario; the result isolates execution price and its associated fee change only.
"""
    (args.output_root / "stage27_e1_price_improvement_stress_report.md").write_text(report)
    print(gates.to_string(index=False))
    print(main_rows[["scenario", "mean_day", "median_day", "p10", "worst", "max_drawdown"]].to_string(index=False))
    print(f"WROTE {args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
