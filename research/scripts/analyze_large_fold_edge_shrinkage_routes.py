#!/usr/bin/env python3
"""Analyze edge shrinkage/fallback routes on a large fixed-edge replay tape.

This is an edge-application diagnostic over fixed C/MR replay fills.  It learns
the preferred edge per segment from the same large replay tape, optionally
shrinking sparse child segments toward parents.  Because the replay can overlap
the model training period, treat results as edge-structure evidence rather than
clean model OOS PnL.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research.analyze_edge_selector_probe_oos import add_context, family_segments, parse_csv, parse_latency_labels


DEFAULT_FAMILIES = (
    "global",
    "day_group_time",
    "day_group_time_contradiction",
    "style_price_bucket",
    "day_group_time_vol_q4",
    "day_group_time_contradiction_vol_q4",
    "day_group_vol_q4",
    "price_bucket",
    "style_move_abs_q4",
)
DEFAULT_ROUTES = (
    "direct_min500_global",
    "direct_min1000_global",
    "fallback_parent_min500",
    "fallback_parent_min1000",
    "shrink_global_k1000",
    "shrink_parent_k1000",
    "shrink_parent_k3000",
    "shrink_time_first_k1000",
    "shrink_vol_first_k1000",
    "shrink_contradiction_first_k1000",
)
SELECTION_LATENCIES = ("350ms", "500ms", "700ms")
DIAGNOSTIC_LATENCIES = ("1000ms",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--fills", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--minute-cache",
        default="data/artifacts/policy_cube_z6_fixed_specs_oos_pm_clean_ffm_4week/cache/crossvenue_minute_vol_30_60_20260725.parquet",
    )
    parser.add_argument("--timezone", default="Europe/London")
    parser.add_argument("--families", type=parse_csv, default=DEFAULT_FAMILIES)
    parser.add_argument("--routes", type=parse_csv, default=DEFAULT_ROUTES)
    parser.add_argument("--selection-latencies", type=parse_latency_labels, default=SELECTION_LATENCIES)
    parser.add_argument("--diagnostic-latencies", type=parse_latency_labels, default=DIAGNOSTIC_LATENCIES)
    parser.add_argument("--c-floors", default="3,4")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def edge_fields(work: pd.DataFrame) -> pd.DataFrame:
    parsed = work["spec_id"].astype(str).str.extract(r"C(\d+)_M(\d+)")
    if parsed.isna().any().any():
        bad = work.loc[parsed.isna().any(axis=1), "spec_id"].drop_duplicates().head(10).tolist()
        raise ValueError(f"Could not parse spec_id edge examples: {bad}")
    out = work
    out["continuation_cents"] = parsed[0].astype(int)
    out["mean_reversion_cents"] = parsed[1].astype(int)
    out["edge_pair"] = "C" + parsed[0] + "_M" + parsed[1]
    out["latency"] = out["execution_latency_label"].astype(str).str.lower()
    out["entry_date"] = out["entry_date"].astype(str)
    return out


def max_drawdown(values: pd.Series) -> float:
    pnl = pd.to_numeric(values, errors="coerce").fillna(0.0)
    if pnl.empty:
        return 0.0
    equity = pnl.cumsum()
    return float((equity.cummax().clip(lower=0.0) - equity).max())


def summarize(part: pd.DataFrame, days: int) -> dict[str, Any]:
    pnl = pd.to_numeric(part["net_pnl"], errors="coerce").fillna(0.0)
    daily = part.assign(_pnl=pnl).groupby("entry_date")["_pnl"].sum()
    cont = part.loc[part["style"].eq("continuation"), "net_pnl"]
    mr = part.loc[part["style"].eq("mean_reversion"), "net_pnl"]
    return {
        "trades": int(len(part)),
        "pnl": float(pnl.sum()),
        "pnl_day": float(pnl.sum()) / max(1, int(days)),
        "pnl_trade": float(pnl.mean()) if len(part) else np.nan,
        "positive_day_rate": float((daily > 0).mean()) if len(daily) else np.nan,
        "worst_day": float(daily.min()) if len(daily) else 0.0,
        "max_drawdown": max_drawdown(pnl),
        "continuation_trades": int(len(cont)),
        "continuation_pnl": float(cont.sum()),
        "mean_reversion_trades": int(len(mr)),
        "mean_reversion_pnl": float(mr.sum()),
    }


def build_segment_stats(
    work: pd.DataFrame,
    family_map: dict[str, pd.Series],
    *,
    selection_latencies: tuple[str, ...],
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for family, segments in family_map.items():
        scoped = work[
            [
                "edge_pair",
                "continuation_cents",
                "mean_reversion_cents",
                "latency",
                "net_pnl",
            ]
        ].copy()
        scoped["selector_family"] = family
        scoped["selector_segment"] = segments.astype(str).to_numpy()
        grouped = (
            scoped.groupby(
                [
                    "selector_family",
                    "selector_segment",
                    "edge_pair",
                    "continuation_cents",
                    "mean_reversion_cents",
                    "latency",
                ],
                observed=True,
            )
            .agg(pnl=("net_pnl", "sum"), trades=("net_pnl", "size"))
            .reset_index()
        )
        rows.append(grouped)
    stats = pd.concat(rows, ignore_index=True, sort=False)
    days = work["entry_date"].nunique()
    stats["pnl_day"] = stats["pnl"] / max(1, int(days))
    id_cols = ["selector_family", "selector_segment", "edge_pair", "continuation_cents", "mean_reversion_cents"]
    out_rows: list[dict[str, Any]] = []
    for key, part in stats.groupby(id_cols, observed=True):
        by_latency = part.set_index("latency")
        if not all(label in by_latency.index for label in selection_latencies):
            continue
        selected = by_latency.loc[list(selection_latencies)]
        row = dict(zip(id_cols, key))
        row["score_min_pnl_day"] = float(selected["pnl_day"].min())
        row["score_mean_pnl_day"] = float(selected["pnl_day"].mean())
        row["score_std_pnl_day"] = float(selected["pnl_day"].std(ddof=0))
        row["score_trades"] = int(selected["trades"].sum())
        row["score_pnl_trade"] = float(selected["pnl"].sum() / max(1, selected["trades"].sum()))
        for latency in tuple(selection_latencies) + DIAGNOSTIC_LATENCIES:
            if latency in by_latency.index:
                row[f"pnl_day_{latency}"] = float(by_latency.loc[latency, "pnl_day"])
                row[f"trades_{latency}"] = int(by_latency.loc[latency, "trades"])
        out_rows.append(row)
    return pd.DataFrame(out_rows)


def parse_route(route: str) -> tuple[str, int | None, float | None]:
    text = str(route)
    if "_min" in text:
        return text.split("_min", 1)[0], int(text.rsplit("_min", 1)[1].split("_", 1)[0]), None
    if "_k" in text:
        return text.split("_k", 1)[0], None, float(text.rsplit("_k", 1)[1].split("_", 1)[0])
    return text, None, None


def parent_candidates(family: str, segment: str, route_kind: str) -> list[tuple[str, str]]:
    parts = str(segment).split("|")
    chain: list[tuple[str, str]] = []
    if family == "global":
        return []
    if route_kind in {"direct", "shrink_global"}:
        return [("global", "all")]
    if family == "style_price_bucket" and len(parts) >= 2:
        chain.append(("price_bucket", parts[1]))
    elif family == "day_group_time_contradiction_vol_q4" and len(parts) >= 4:
        day, time, contradiction, vol = parts[0], parts[1], parts[2], parts[3]
        if route_kind in {"shrink_vol_first", "fallback_vol_first"}:
            chain += [("day_group_vol_q4", f"{day}|{vol}"), ("vol_q4", vol)]
        elif route_kind in {"shrink_contradiction_first", "fallback_contradiction_first"}:
            chain += [("contradiction", contradiction)]
        elif route_kind in {"shrink_time_first", "fallback_time_first"}:
            chain += [("day_group_time_contradiction", f"{day}|{time}|{contradiction}"), ("day_group_time", f"{day}|{time}"), ("time", time)]
        else:
            chain += [("day_group_time_contradiction", f"{day}|{time}|{contradiction}"), ("day_group_time", f"{day}|{time}"), ("day_group", day)]
    elif family == "day_group_time_vol_q4" and len(parts) >= 3:
        day, time, vol = parts[0], parts[1], parts[2]
        if route_kind in {"shrink_vol_first", "fallback_vol_first"}:
            chain += [("day_group_vol_q4", f"{day}|{vol}"), ("vol_q4", vol)]
        else:
            chain += [("day_group_time", f"{day}|{time}"), ("day_group", day)]
    elif family == "day_group_time_contradiction" and len(parts) >= 3:
        day, time, contradiction = parts[0], parts[1], parts[2]
        if route_kind in {"shrink_contradiction_first", "fallback_contradiction_first"}:
            chain += [("contradiction", contradiction)]
        else:
            chain += [("day_group_time", f"{day}|{time}"), ("day_group", day)]
    elif family == "day_group_time" and len(parts) >= 2:
        chain += [("day_group", parts[0]), ("time", parts[1])]
    elif family == "day_group_vol_q4" and len(parts) >= 2:
        chain += [("day_group", parts[0]), ("vol_q4", parts[1])]
    elif family in {"day_group", "time", "vol_q4", "price_bucket", "contradiction", "move_abs_q4"}:
        chain = []
    elif family == "style_move_abs_q4" and len(parts) >= 2:
        chain.append(("move_abs_q4", parts[1]))
    chain.append(("global", "all"))
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in chain:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def score_lookup(stats: pd.DataFrame) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (str(row.selector_family), str(row.selector_segment), str(row.edge_pair)): row._asdict()
        for row in stats.itertuples(index=False)
    }


def choose_edges(
    stats: pd.DataFrame,
    *,
    family: str,
    route: str,
    c_floor: int,
) -> pd.DataFrame:
    route_kind, min_trades, shrink_k = parse_route(route)
    scoped = stats.loc[stats["selector_family"].eq(family) & stats["continuation_cents"].ge(c_floor)].copy()
    if scoped.empty:
        return scoped
    lookup = score_lookup(stats.loc[stats["continuation_cents"].ge(c_floor)].copy())
    rows: list[dict[str, Any]] = []
    for segment, seg_stats in scoped.groupby("selector_segment", observed=True):
        adjusted: list[dict[str, Any]] = []
        for row in seg_stats.itertuples(index=False):
            out = row._asdict()
            out["raw_score_min_pnl_day"] = float(row.score_min_pnl_day)
            out["route"] = route
            out["c_floor"] = c_floor
            out["chosen_from_family"] = family
            out["chosen_from_segment"] = segment
            if route_kind == "direct":
                if int(row.score_trades) < int(min_trades or 0):
                    parent_edge = first_parent_choice(lookup, family, str(segment), str(row.edge_pair), route_kind, min_trades or 0)
                    if parent_edge is not None:
                        out.update(parent_edge)
                out["adjusted_score"] = float(out.get("score_min_pnl_day", row.score_min_pnl_day))
            elif route_kind.startswith("fallback"):
                if int(row.score_trades) < int(min_trades or 0):
                    parent_edge = first_parent_choice(lookup, family, str(segment), str(row.edge_pair), route_kind, min_trades or 0)
                    if parent_edge is not None:
                        out.update(parent_edge)
                out["adjusted_score"] = float(out.get("score_min_pnl_day", row.score_min_pnl_day))
            elif route_kind.startswith("shrink"):
                parent = first_parent_score(lookup, family, str(segment), str(row.edge_pair), route_kind)
                parent_score = float(parent["score_min_pnl_day"]) if parent is not None else 0.0
                alpha = float(row.score_trades) / (float(row.score_trades) + float(shrink_k or 1000.0))
                out["parent_score_min_pnl_day"] = parent_score
                out["shrink_alpha"] = alpha
                out["adjusted_score"] = alpha * float(row.score_min_pnl_day) + (1.0 - alpha) * parent_score
            else:
                out["adjusted_score"] = float(row.score_min_pnl_day)
            adjusted.append(out)
        choices = pd.DataFrame(adjusted).sort_values(
            ["adjusted_score", "score_mean_pnl_day", "score_pnl_trade", "score_trades"],
            ascending=[False, False, False, False],
        )
        rows.append(choices.head(1).iloc[0].to_dict())
    return pd.DataFrame(rows)


def first_parent_score(
    lookup: dict[tuple[str, str, str], dict[str, Any]],
    family: str,
    segment: str,
    edge_pair: str,
    route_kind: str,
) -> dict[str, Any] | None:
    for parent_family, parent_segment in parent_candidates(family, segment, route_kind):
        row = lookup.get((parent_family, parent_segment, edge_pair))
        if row is not None:
            return row
    return None


def first_parent_choice(
    lookup: dict[tuple[str, str, str], dict[str, Any]],
    family: str,
    segment: str,
    edge_pair: str,
    route_kind: str,
    min_trades: int,
) -> dict[str, Any] | None:
    del edge_pair
    candidates: list[dict[str, Any]] = []
    for parent_family, parent_segment in parent_candidates(family, segment, route_kind):
        for (lookup_family, lookup_segment, _), row in lookup.items():
            if lookup_family != parent_family or lookup_segment != parent_segment:
                continue
            if int(row.get("score_trades", 0)) >= min(1, min_trades):
                candidates.append(row)
        if candidates:
            best = max(candidates, key=lambda row: (row["score_min_pnl_day"], row["score_mean_pnl_day"], row["score_pnl_trade"]))
            return {
                "edge_pair": best["edge_pair"],
                "continuation_cents": best["continuation_cents"],
                "mean_reversion_cents": best["mean_reversion_cents"],
                "score_min_pnl_day": best["score_min_pnl_day"],
                "score_mean_pnl_day": best["score_mean_pnl_day"],
                "score_trades": best["score_trades"],
                "score_pnl_trade": best["score_pnl_trade"],
                **{
                    key: value
                    for key, value in best.items()
                    if str(key).startswith(("pnl_day_", "trades_"))
                },
                "chosen_from_family": parent_family,
                "chosen_from_segment": parent_segment,
            }
    return None


def evaluate_choices(
    work: pd.DataFrame,
    family_segments_map: dict[str, pd.Series],
    choices: pd.DataFrame,
    *,
    family: str,
    route: str,
    c_floor: int,
    selection_latencies: tuple[str, ...],
    diagnostic_latencies: tuple[str, ...],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    segment_series = family_segments_map[family].astype(str)
    choice_map = dict(zip(choices["selector_segment"].astype(str), choices["edge_pair"].astype(str)))
    selected_edge = segment_series.map(choice_map)
    selected = work.loc[work["edge_pair"].astype(str).eq(selected_edge)].copy()
    days = work["entry_date"].nunique()
    latency_rows: list[dict[str, Any]] = []
    for latency, part in selected.groupby("latency", observed=True):
        row = {
            "selector_family": family,
            "route": route,
            "c_floor": c_floor,
            "latency": latency,
            "days": int(days),
            **summarize(part, int(days)),
        }
        latency_rows.append(row)
    latency = pd.DataFrame(latency_rows)
    if latency.empty:
        return latency, pd.DataFrame()
    by_latency = latency.set_index("latency")
    if not all(label in by_latency.index for label in selection_latencies):
        return latency, pd.DataFrame()
    selected_lat = by_latency.loc[list(selection_latencies)]
    primary = {
        "selector_family": family,
        "route": route,
        "c_floor": c_floor,
        "segments": int(choices["selector_segment"].nunique()),
        "min_pnl_day_select": float(selected_lat["pnl_day"].min()),
        "mean_pnl_day_select": float(selected_lat["pnl_day"].mean()),
        "std_pnl_day_select": float(selected_lat["pnl_day"].std(ddof=0)),
        "mean_trades_day_select": float((selected_lat["trades"] / selected_lat["days"]).mean()),
        "mean_pnl_trade_select": float(selected_lat["pnl_trade"].mean()),
        "mean_positive_day_rate_select": float(selected_lat["positive_day_rate"].mean()),
        "worst_day_select": float(selected_lat["worst_day"].min()),
        "max_drawdown_select": float(selected_lat["max_drawdown"].max()),
        "mean_continuation_trades_select": float(selected_lat["continuation_trades"].mean()),
        "mean_continuation_pnl_select": float(selected_lat["continuation_pnl"].mean()),
        "mean_mean_reversion_trades_select": float(selected_lat["mean_reversion_trades"].mean()),
        "mean_mean_reversion_pnl_select": float(selected_lat["mean_reversion_pnl"].mean()),
    }
    for label in diagnostic_latencies:
        if label in by_latency.index:
            primary[f"pnl_day_{label}"] = float(by_latency.loc[label, "pnl_day"])
            primary[f"trades_{label}"] = int(by_latency.loc[label, "trades"])
    return latency, pd.DataFrame([primary])


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    fills_path = Path(args.fills).expanduser().resolve() if args.fills else input_dir / "oos_fills.parquet"
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_dir / "large_edge_shrinkage_analysis"
    if args.overwrite and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    needed_columns = [
        "signal_time_utc",
        "entry_date",
        "fold",
        "spec_id",
        "execution_latency_label",
        "net_pnl",
        "style",
        "ask",
        "move_z",
        "policy_level",
        "local_vol_1h_bps",
    ]
    try:
        work = pd.read_parquet(fills_path, columns=needed_columns)
    except Exception:
        work = pd.read_parquet(fills_path)
    print(f"[large_edge_shrinkage] loaded fills rows={len(work):,}", flush=True)
    if work.empty:
        raise SystemExit(f"No fills found: {fills_path}")
    work = edge_fields(add_context(work, args.timezone, Path(args.minute_cache).expanduser().resolve()))
    print(f"[large_edge_shrinkage] context attached days={work['entry_date'].nunique():,}", flush=True)
    families_all = family_segments(work)
    requested_families = tuple(f for f in args.families if f in families_all)
    if not requested_families:
        raise SystemExit(f"No requested families are available: {args.families}")
    required = set(requested_families) | {
        "global",
        "day_group",
        "time",
        "vol_q4",
        "day_group_time",
        "day_group_vol_q4",
        "price_bucket",
        "contradiction",
        "day_group_time_contradiction",
        "move_abs_q4",
    }
    family_map = {family: families_all[family] for family in required if family in families_all}
    print(f"[large_edge_shrinkage] building segment stats families={len(family_map)}", flush=True)
    stats = build_segment_stats(work, family_map, selection_latencies=tuple(args.selection_latencies))
    stats.to_csv(output_dir / "segment_edge_stats.csv", index=False)
    print(f"[large_edge_shrinkage] segment stats rows={len(stats):,}", flush=True)

    c_floors = tuple(int(item.strip()) for item in str(args.c_floors).split(",") if item.strip())
    choice_frames: list[pd.DataFrame] = []
    latency_frames: list[pd.DataFrame] = []
    primary_frames: list[pd.DataFrame] = []
    for c_floor in c_floors:
        for family in requested_families:
            for route in args.routes:
                print(f"[large_edge_shrinkage] route c_floor={c_floor} family={family} route={route}", flush=True)
                choices = choose_edges(stats, family=family, route=str(route), c_floor=int(c_floor))
                if choices.empty:
                    continue
                choices["selector_family"] = family
                choices["route"] = route
                choices["c_floor"] = c_floor
                choice_frames.append(choices)
                latency, primary = evaluate_choices(
                    work,
                    families_all,
                    choices,
                    family=family,
                    route=str(route),
                    c_floor=int(c_floor),
                    selection_latencies=tuple(args.selection_latencies),
                    diagnostic_latencies=tuple(args.diagnostic_latencies),
                )
                if not latency.empty:
                    latency_frames.append(latency)
                if not primary.empty:
                    primary_frames.append(primary)

    choices_out = pd.concat(choice_frames, ignore_index=True, sort=False) if choice_frames else pd.DataFrame()
    latency_out = pd.concat(latency_frames, ignore_index=True, sort=False) if latency_frames else pd.DataFrame()
    primary_out = pd.concat(primary_frames, ignore_index=True, sort=False) if primary_frames else pd.DataFrame()
    if primary_out.empty:
        raise SystemExit("No primary route summaries produced")
    primary_out = primary_out.sort_values(
        ["c_floor", "min_pnl_day_select", "mean_pnl_day_select", "mean_pnl_trade_select"],
        ascending=[True, False, False, False],
    )
    choices_out.to_csv(output_dir / "chosen_edge_routes.csv", index=False)
    latency_out.to_csv(output_dir / "route_latency_summary.csv", index=False)
    primary_out.to_csv(output_dir / "route_primary_summary.csv", index=False)

    manifest = {
        "input_dir": str(input_dir),
        "fills": str(fills_path),
        "rows": int(len(work)),
        "days": int(work["entry_date"].nunique()),
        "families": list(requested_families),
        "routes": list(args.routes),
        "c_floors": list(c_floors),
        "selection_latencies": list(args.selection_latencies),
        "diagnostic_latencies": list(args.diagnostic_latencies),
        "note": "Large overlapping edge-selection diagnostic. Not clean model OOS.",
    }
    (output_dir / "large_edge_shrinkage_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[large_edge_shrinkage] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
