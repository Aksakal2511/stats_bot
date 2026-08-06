#!/usr/bin/env python3
"""Analyze edge-application families from fixed C/M edge OOS replay fills.

This is a diagnostic selector layer.  The expensive OOS run replays a fixed
grid such as C03/M03..C10/M10.  This script asks which segment definitions
would have selected more sensible required edges from that grid.

The selector rows are assembled from fixed-edge fill tapes, so they are useful
for comparing families and consistency, not as final deployable evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_SELECTION_LATENCIES = "350ms,500ms,700ms"
DEFAULT_DIAGNOSTIC_LATENCIES = "1000ms"
TIME_BINS = [0.0, 8.0, 14.5, 17.0, 22.0, 24.0]
TIME_LABELS = ["00_08", "08_1430", "1430_17", "17_22", "22_24"]


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def normalize_latency_label(value: str) -> str:
    text = str(value).strip()
    upper = text.upper()
    if upper == "EMPIRICAL":
        return "empirical"
    raw = upper.removesuffix("MS")
    return f"{int(raw)}ms"


def parse_latency_labels(value: str) -> tuple[str, ...]:
    labels: list[str] = []
    for item in parse_csv(value):
        label = normalize_latency_label(item)
        if label not in labels:
            labels.append(label)
    return tuple(labels)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, help="OOS output dir containing oos_fills.parquet.")
    parser.add_argument("--fills", default="", help="Optional explicit fills parquet.")
    parser.add_argument("--output-dir", default="", help="Defaults to <input-dir>/edge_selector_probe_analysis.")
    parser.add_argument(
        "--minute-cache",
        default="data/artifacts/policy_cube_z6_fixed_specs_oos_pm_clean_ffm_4week/cache/crossvenue_minute_vol_30_60_20260725.parquet",
        help="Minute volatility cache used by the replay. Joined as known-at next minute.",
    )
    parser.add_argument("--timezone", default="Europe/London")
    parser.add_argument("--selection-latencies", type=parse_latency_labels, default=parse_latency_labels(DEFAULT_SELECTION_LATENCIES))
    parser.add_argument("--diagnostic-latencies", type=parse_latency_labels, default=parse_latency_labels(DEFAULT_DIAGNOSTIC_LATENCIES))
    parser.add_argument(
        "--families",
        type=parse_csv,
        default=(),
        help="Optional comma-separated selector families to include, e.g. monwed_thufri_weekend,time,vol_q4,contradiction.",
    )
    parser.add_argument("--min-segment-trades", type=int, default=25)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def edge_pair_from_spec(spec_id: pd.Series) -> pd.DataFrame:
    parsed = spec_id.astype(str).str.extract(r"C(\d+)_M(\d+)")
    if parsed.isna().any().any():
        bad = spec_id.loc[parsed.isna().any(axis=1)].drop_duplicates().head(10).tolist()
        raise ValueError(f"Could not parse C/M edge from spec_id examples: {bad}")
    out = pd.DataFrame(index=spec_id.index)
    out["continuation_cents"] = parsed[0].astype(int)
    out["mean_reversion_cents"] = parsed[1].astype(int)
    out["edge_pair"] = "C" + parsed[0] + "_M" + parsed[1]
    return out


def period_days(input_dir: Path, fills: pd.DataFrame) -> dict[str, int]:
    manifest_path = input_dir / "run_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        days: dict[str, int] = {}
        for fold in manifest.get("folds", []):
            start = pd.Timestamp(fold["test_start"])
            end = pd.Timestamp(fold["test_end"])
            days[str(fold["id"])] = int((end - start).days) + 1
        if days:
            return days
    return fills.groupby("fold")["entry_date"].nunique().astype(int).to_dict()


def price_bucket_from_frame(frame: pd.DataFrame) -> pd.Series:
    if "policy_level" in frame.columns:
        extracted = frame["policy_level"].astype(str).str.extract(r"(p(?:lt|gt)?\d+_?\d*|p\d+_\d+)$")[0]
        if extracted.notna().any():
            return extracted.fillna("unknown")
    ask = pd.to_numeric(frame["ask"], errors="coerce")
    labels = np.select(
        [
            ask.lt(0.20),
            ask.lt(0.30),
            ask.lt(0.40),
            ask.lt(0.45),
            ask.lt(0.48),
            ask.lt(0.52),
            ask.lt(0.55),
            ask.lt(0.60),
            ask.lt(0.70),
            ask.le(0.80),
        ],
        ["p_lt20", "p20_30", "p30_40", "p40_45", "p45_48", "p48_52", "p52_55", "p55_60", "p60_70", "p70_80"],
        default="p_gt80",
    )
    return pd.Series(labels, index=frame.index)


def attach_local_vol(work: pd.DataFrame, minute_cache: Path) -> pd.DataFrame:
    if "local_vol_1h_bps" in work.columns and pd.to_numeric(work["local_vol_1h_bps"], errors="coerce").notna().any():
        return work
    work = work.drop(columns=["local_vol_1h_bps"], errors="ignore")
    if not minute_cache.exists():
        print(f"[WARN] minute cache not found; volatility buckets will be unknown: {minute_cache}", flush=True)
        work = work.copy()
        work["local_vol_1h_bps"] = np.nan
        return work
    minute = pd.read_parquet(minute_cache, columns=["minute_utc", "coinbase_rv_1h_bps"])
    minute["minute_utc"] = pd.to_datetime(minute["minute_utc"], utc=True)
    minute["vol_known_minute_utc"] = minute["minute_utc"] + pd.Timedelta(minutes=1)
    minute = minute.rename(columns={"coinbase_rv_1h_bps": "local_vol_1h_bps"})
    minute = minute[["vol_known_minute_utc", "local_vol_1h_bps"]].drop_duplicates("vol_known_minute_utc", keep="last")
    work = work.copy()
    work["vol_known_minute_utc"] = work["signal_time_utc"].dt.floor("min")
    work = work.merge(minute, on="vol_known_minute_utc", how="left")
    return work.drop(columns=["vol_known_minute_utc"])


def add_context(fills: pd.DataFrame, timezone: str, minute_cache: Path) -> pd.DataFrame:
    work = fills.copy()
    if "net_pnl" not in work.columns:
        work["net_pnl"] = pd.to_numeric(work["gross_pnl"], errors="coerce") - 0.01
    work["signal_time_utc"] = pd.to_datetime(work["signal_time_utc"], utc=True)
    work = attach_local_vol(work, minute_cache)
    work["entry_date"] = work["entry_date"].astype(str)
    local = work["signal_time_utc"].dt.tz_convert(timezone)
    dow = local.dt.dayofweek
    work["weekday_weekend"] = np.where(dow.ge(5), "weekend", "weekday")
    work["day_group"] = np.select(
        [dow.le(2), dow.between(3, 4), dow.ge(5)],
        ["mon_wed", "thu_fri", "weekend"],
        default="unknown",
    )
    work["dow_group"] = np.select(
        [dow.eq(0), dow.between(1, 3), dow.eq(4), dow.ge(5)],
        ["monday", "tue_thu", "friday", "weekend"],
        default="unknown",
    )
    hours = (
        local.dt.hour.astype(float)
        + local.dt.minute.astype(float) / 60.0
        + local.dt.second.astype(float) / 3600.0
        + local.dt.microsecond.astype(float) / 3_600_000_000.0
    )
    work["time_seg"] = pd.cut(hours, TIME_BINS, labels=TIME_LABELS, right=False, include_lowest=True).astype(str)
    work["price_bucket"] = price_bucket_from_frame(work)
    work["latency"] = work["execution_latency_label"].astype(str).str.lower()
    codes = edge_pair_from_spec(work["spec_id"])
    work = pd.concat([work.reset_index(drop=True), codes.reset_index(drop=True)], axis=1)
    work["style"] = work["style"].astype(str)
    work["selected_price_band"] = np.where(pd.to_numeric(work["ask"], errors="coerce").le(0.50), "underdog", "favorite")
    work["contradiction_state"] = np.where(
        work["style"].eq("continuation") & pd.to_numeric(work["ask"], errors="coerce").le(0.48),
        "cheap_continuation",
        "normal",
    )
    vol = pd.to_numeric(work.get("local_vol_1h_bps"), errors="coerce")
    move_abs = pd.to_numeric(work.get("move_z"), errors="coerce").abs()
    work["vol_bucket"] = "vol_unknown"
    work["move_abs_bucket"] = "move_unknown"
    for fold, idx in work.groupby("fold", observed=True).groups.items():
        vol_part = vol.loc[idx]
        if vol_part.dropna().nunique() >= 4:
            work.loc[idx, "vol_bucket"] = pd.qcut(vol_part.rank(method="first"), 4, labels=["v0_low", "v1_midlow", "v2_midhigh", "v3_high"]).astype(str)
        move_part = move_abs.loc[idx]
        if move_part.dropna().nunique() >= 4:
            work.loc[idx, "move_abs_bucket"] = pd.qcut(move_part.rank(method="first"), 4, labels=["z0_low", "z1_midlow", "z2_midhigh", "z3_high"]).astype(str)
    return work


def family_segments(work: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "global": pd.Series("all", index=work.index),
        "weekday_weekend": work["weekday_weekend"].astype(str),
        "day_group": work["day_group"].astype(str),
        "monwed_thufri_weekend": work["day_group"].astype(str),
        "mon_tuethu_fri_weekend": work["dow_group"].astype(str),
        "time_band": work["time_seg"].astype(str),
        "time": work["time_seg"].astype(str),
        "day_group_time": work["day_group"].astype(str) + "|" + work["time_seg"].astype(str),
        "vol_q4": work["vol_bucket"].astype(str),
        "day_group_vol_q4": work["day_group"].astype(str) + "|" + work["vol_bucket"].astype(str),
        "day_group_time_vol_q4": (
            work["day_group"].astype(str)
            + "|"
            + work["time_seg"].astype(str)
            + "|"
            + work["vol_bucket"].astype(str)
        ),
        "price_bucket": work["price_bucket"].astype(str),
        "style_price_bucket": work["style"].astype(str) + "|" + work["price_bucket"].astype(str),
        "move_abs_q4": work["move_abs_bucket"].astype(str),
        "style_move_abs_q4": work["style"].astype(str) + "|" + work["move_abs_bucket"].astype(str),
        "contradiction_state": work["contradiction_state"].astype(str),
        "contradiction": work["contradiction_state"].astype(str),
        "day_group_time_contradiction": (
            work["day_group"].astype(str)
            + "|"
            + work["time_seg"].astype(str)
            + "|"
            + work["contradiction_state"].astype(str)
        ),
        "day_group_time_contradiction_vol_q4": (
            work["day_group"].astype(str)
            + "|"
            + work["time_seg"].astype(str)
            + "|"
            + work["contradiction_state"].astype(str)
            + "|"
            + work["vol_bucket"].astype(str)
        ),
    }


def max_drawdown(values: pd.Series) -> float:
    if values.empty:
        return 0.0
    equity = pd.to_numeric(values, errors="coerce").fillna(0.0).cumsum()
    return float((equity.cummax().clip(lower=0.0) - equity).max())


def summarize_selected(part: pd.DataFrame, days: int) -> dict[str, Any]:
    if part.empty:
        return {
            "trades": 0,
            "pnl": 0.0,
            "pnl_day": 0.0,
            "pnl_trade": np.nan,
            "positive_day_rate": np.nan,
            "worst_day": 0.0,
            "max_drawdown": 0.0,
            "continuation_trades": 0,
            "continuation_pnl": 0.0,
            "mean_reversion_trades": 0,
            "mean_reversion_pnl": 0.0,
        }
    ordered = part.sort_values(["signal_time_utc", "fill_time_utc"], kind="stable").copy()
    pnl = pd.to_numeric(ordered["net_pnl"], errors="coerce").fillna(0.0)
    daily = ordered.assign(_date=ordered["signal_time_utc"].dt.date, _pnl=pnl).groupby("_date")["_pnl"].sum()
    cont = ordered.loc[ordered["style"].eq("continuation"), "net_pnl"]
    mr = ordered.loc[ordered["style"].eq("mean_reversion"), "net_pnl"]
    return {
        "trades": int(len(ordered)),
        "pnl": float(pnl.sum()),
        "pnl_day": float(pnl.sum()) / max(1, days),
        "pnl_trade": float(pnl.mean()) if len(pnl) else np.nan,
        "positive_day_rate": float((daily > 0).mean()) if len(daily) else np.nan,
        "worst_day": float(daily.min()) if len(daily) else 0.0,
        "max_drawdown": max_drawdown(pnl),
        "continuation_trades": int(len(cont)),
        "continuation_pnl": float(cont.sum()),
        "mean_reversion_trades": int(len(mr)),
        "mean_reversion_pnl": float(mr.sum()),
    }


def rank_segment_edges(
    work: pd.DataFrame,
    segments: pd.Series,
    family: str,
    days_by_fold: dict[str, int],
    selection_latencies: tuple[str, ...],
    min_segment_trades: int,
) -> pd.DataFrame:
    scoped = work.copy()
    scoped["selector_family"] = family
    scoped["selector_segment"] = segments.astype(str)
    group_cols = [
        "fold",
        "surface_arm",
        "selector_family",
        "selector_segment",
        "edge_pair",
        "continuation_cents",
        "mean_reversion_cents",
        "latency",
    ]
    grouped = (
        scoped.groupby(group_cols, observed=True)
        .agg(
            pnl=("net_pnl", "sum"),
            trades=("net_pnl", "size"),
            pnl_trade=("net_pnl", "mean"),
            continuation_trades=("style", lambda s: int(s.eq("continuation").sum())),
            mean_reversion_trades=("style", lambda s: int(s.eq("mean_reversion").sum())),
        )
        .reset_index()
    )
    grouped["days"] = grouped["fold"].map(days_by_fold).astype(float)
    grouped["pnl_day"] = grouped["pnl"] / grouped["days"].clip(lower=1.0)
    id_cols = [
        "fold",
        "surface_arm",
        "selector_family",
        "selector_segment",
        "edge_pair",
        "continuation_cents",
        "mean_reversion_cents",
    ]
    rows: list[dict[str, Any]] = []
    for key, part in grouped.groupby(id_cols, observed=True):
        by_latency = part.set_index("latency")
        if not all(label in by_latency.index for label in selection_latencies):
            continue
        selected = by_latency.loc[list(selection_latencies)]
        if int(selected["trades"].sum()) < int(min_segment_trades):
            continue
        row = dict(zip(id_cols, key))
        row["min_pnl_day_select"] = float(selected["pnl_day"].min())
        row["mean_pnl_day_select"] = float(selected["pnl_day"].mean())
        row["std_pnl_day_select"] = float(selected["pnl_day"].std(ddof=0))
        row["trades_select"] = int(selected["trades"].sum())
        row["pnl_trade_select"] = float(selected["pnl"].sum() / max(1, selected["trades"].sum()))
        for latency, latency_part in by_latency.iterrows():
            row[f"pnl_day_{latency}"] = float(latency_part["pnl_day"])
            row[f"trades_{latency}"] = int(latency_part["trades"])
        rows.append(row)
    ranked = pd.DataFrame(rows)
    if ranked.empty:
        return ranked
    ranked = ranked.sort_values(
        [
            "fold",
            "surface_arm",
            "selector_family",
            "selector_segment",
            "min_pnl_day_select",
            "mean_pnl_day_select",
            "pnl_trade_select",
            "trades_select",
        ],
        ascending=[True, True, True, True, False, False, False, False],
    )
    ranked["rank_in_segment"] = (
        ranked.groupby(["fold", "surface_arm", "selector_family", "selector_segment"], observed=True).cumcount() + 1
    )
    return ranked


def assemble_family_summary(
    work: pd.DataFrame,
    segments: pd.Series,
    choices: pd.DataFrame,
    family: str,
    days_by_fold: dict[str, int],
    selection_latencies: tuple[str, ...],
    diagnostic_latencies: tuple[str, ...],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    scoped = work.copy()
    scoped["selector_segment"] = segments.astype(str)
    choice = choices.loc[choices["rank_in_segment"].eq(1), ["fold", "surface_arm", "selector_segment", "edge_pair"]].copy()
    choice = choice.rename(columns={"edge_pair": "chosen_edge_pair"})
    scoped = scoped.merge(choice, on=["fold", "surface_arm", "selector_segment"], how="inner", validate="many_to_one")
    selected = scoped.loc[scoped["edge_pair"].eq(scoped["chosen_edge_pair"])].copy()
    rows: list[dict[str, Any]] = []
    for (fold, surface_arm, latency), part in selected.groupby(["fold", "surface_arm", "latency"], observed=True):
        if latency not in (*selection_latencies, *diagnostic_latencies):
            continue
        rows.append(
            {
                "selector_family": family,
                "fold": fold,
                "surface_arm": surface_arm,
                "latency": latency,
                "days": int(days_by_fold.get(str(fold), part["entry_date"].nunique())),
                **summarize_selected(part, int(days_by_fold.get(str(fold), part["entry_date"].nunique()))),
            }
        )
    summary = pd.DataFrame(rows)
    if summary.empty:
        return summary, selected
    primary_rows: list[dict[str, Any]] = []
    for key, part in summary.groupby(["selector_family", "fold", "surface_arm"], observed=True):
        by_latency = part.set_index("latency")
        if not all(label in by_latency.index for label in selection_latencies):
            continue
        selected_lat = by_latency.loc[list(selection_latencies)]
        row = dict(zip(["selector_family", "fold", "surface_arm"], key))
        row["min_pnl_day_select"] = float(selected_lat["pnl_day"].min())
        row["mean_pnl_day_select"] = float(selected_lat["pnl_day"].mean())
        row["std_pnl_day_select"] = float(selected_lat["pnl_day"].std(ddof=0))
        for metric in ("trades", "pnl", "pnl_trade", "positive_day_rate", "worst_day", "max_drawdown", "continuation_trades", "continuation_pnl", "mean_reversion_trades", "mean_reversion_pnl"):
            row[f"mean_{metric}_select"] = float(pd.to_numeric(selected_lat[metric], errors="coerce").mean())
        if diagnostic_latencies:
            for label in diagnostic_latencies:
                if label in by_latency.index:
                    row[f"pnl_day_{label}"] = float(by_latency.loc[label, "pnl_day"])
                    row[f"trades_{label}"] = int(by_latency.loc[label, "trades"])
        primary_rows.append(row)
    return pd.DataFrame(primary_rows), selected


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    fills_path = Path(args.fills).expanduser().resolve() if args.fills else input_dir / "oos_fills.parquet"
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_dir / "edge_selector_probe_analysis"
    if args.overwrite and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fills = pd.read_parquet(fills_path)
    if fills.empty:
        raise SystemExit(f"No fills found at {fills_path}")
    minute_cache = Path(args.minute_cache).expanduser().resolve()
    work = add_context(fills, args.timezone, minute_cache)
    days_by_fold = period_days(input_dir, work)
    segment_map = family_segments(work)
    if args.families:
        wanted = set(args.families)
        invalid = sorted(wanted - set(segment_map))
        if invalid:
            raise SystemExit(f"Unknown selector families requested: {invalid}; valid={sorted(segment_map)}")
        segment_map = {name: value for name, value in segment_map.items() if name in wanted}
    rankings: list[pd.DataFrame] = []
    family_summaries: list[pd.DataFrame] = []
    selected_samples: list[pd.DataFrame] = []
    for family, segments in segment_map.items():
        ranked = rank_segment_edges(
            work,
            segments,
            family,
            days_by_fold,
            args.selection_latencies,
            args.min_segment_trades,
        )
        if ranked.empty:
            continue
        rankings.append(ranked)
        summary, selected = assemble_family_summary(
            work,
            segments,
            ranked,
            family,
            days_by_fold,
            args.selection_latencies,
            args.diagnostic_latencies,
        )
        if not summary.empty:
            family_summaries.append(summary)
        if not selected.empty:
            keep_cols = [
                "fold",
                "surface_arm",
                "latency",
                "selector_segment",
                "chosen_edge_pair",
                "edge_pair",
                "signal_time_utc",
                "canonical_window_id",
                "style",
                "side",
                "ask",
                "value",
                "edge",
                "fill_price",
                "settlement_value",
                "net_pnl",
            ]
            selected_samples.append(selected[[c for c in keep_cols if c in selected.columns]].assign(selector_family=family))
    all_rankings = pd.concat(rankings, ignore_index=True, sort=False) if rankings else pd.DataFrame()
    all_summaries = pd.concat(family_summaries, ignore_index=True, sort=False) if family_summaries else pd.DataFrame()
    all_rankings.to_csv(output_dir / "segment_edge_rankings.csv", index=False)
    all_summaries.to_csv(output_dir / "selector_family_primary_summary.csv", index=False)
    choices = all_rankings.loc[all_rankings.get("rank_in_segment", pd.Series(dtype=int)).eq(1)].copy() if not all_rankings.empty else pd.DataFrame()
    choices.to_csv(output_dir / "selector_edge_choices.csv", index=False)
    if not choices.empty:
        consistency = (
            choices.groupby(["selector_family", "surface_arm", "selector_segment", "edge_pair"], observed=True)
            .agg(folds=("fold", "nunique"), mean_rank_score=("mean_pnl_day_select", "mean"))
            .reset_index()
            .sort_values(["selector_family", "surface_arm", "selector_segment", "folds", "mean_rank_score"], ascending=[True, True, True, False, False])
        )
        consistency.to_csv(output_dir / "edge_choice_consistency.csv", index=False)
    if selected_samples:
        pd.concat(selected_samples, ignore_index=True, sort=False).to_parquet(output_dir / "selector_selected_fills_sample.parquet", index=False)
    manifest = {
        "input_dir": str(input_dir),
        "fills": str(fills_path),
        "output_dir": str(output_dir),
        "rows": int(len(work)),
        "fold_days": days_by_fold,
        "selection_latencies": list(args.selection_latencies),
        "diagnostic_latencies": list(args.diagnostic_latencies),
        "minute_cache": str(minute_cache),
        "families": list(segment_map),
        "note": "Diagnostic selector assembly from fixed-edge fill tapes; segment choices are not nested-calibrated.",
    }
    (output_dir / "edge_selector_probe_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not all_summaries.empty:
        display = all_summaries.sort_values(
            ["fold", "min_pnl_day_select", "mean_pnl_day_select", "mean_pnl_trade_select"],
            ascending=[True, False, False, False],
        ).groupby("fold", observed=True).head(10)
        print(display.to_string(index=False), flush=True)
    print(f"[edge_selector_probe] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
