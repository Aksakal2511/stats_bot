#!/usr/bin/env python3
"""OOS replay for continuous move_dz value surfaces.

This is a research-only companion to run_z6_move_dz_fixed_specs_oos.py.  It
keeps the same folds, Telonex/Polymarket quotes, PM settlement outcomes and current
execution replay, but replaces hard move_dz/price bucket policy values with a
regularized continuous probability surface.  PM-conditioned surfaces use
Polymarket ask as a feature; BTC-only surfaces use PM ask only for the final
edge/execution check.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import SplineTransformer
from sklearn.preprocessing import StandardScaler


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research.run_crossvenue_corrected_signal_research import daily_part_path, load_frame, parquet_columns  # noqa: E402
from scripts.research.run_current_execution_local_vol_walk_forward import (  # noqa: E402
    Arm,
    LatencyProfile,
    date_range,
    fmt,
    latency_profile_from_config,
    load_outcomes,
    minute_for_mode,
    replay_current_execution,
    stamp,
)
from scripts.research.run_execution_aware_crossvenue_walk_forward import quote_lookup  # noqa: E402
from scripts.research.run_z6_edge_tte_nested_walk_forward import (  # noqa: E402
    DEFAULT_DAILY,
    DEFAULT_LATENCY_CONFIG,
    DEFAULT_MINUTE,
    DEFAULT_OUTCOMES,
    FOLD_SETS,
    Spec,
)
from scripts.research.run_z6_move_dz_fixed_specs_oos import (  # noqa: E402
    DEFAULT_DZ_ROOT,
    DEFAULT_SPECS,
    attach_distance_z,
    half_life_label,
    parse_half_lives,
    parse_specs,
    parse_tte_ranges,
)


DEFAULT_OUTPUT = REPO_ROOT / "data/artifacts/policy_cube_move_dz_continuous_surface_oos"
DEFAULT_PM_EVENT_CB_TICK_ROOT = (
    REPO_ROOT
    / "data/combined_new/session_splits/statsbot_12_bucket_2h_london/"
    "pm_event_cb_tick_lag100"
)
EPS = 1e-5
DEFAULT_SUBSECOND_LATENCIES = "300,500,700,1000,EMPIRICAL"
DEFAULT_EXECUTION_ENTRY_MODES = (
    "passive_maker,touch_limit,guarded_fast_1c,guarded_fast_2c,unguarded_fast_1c,unguarded_fast_2c"
)
DEFAULT_EXECUTION_LATCHES = "no_latch,1s,2s,3s"
EXECUTION_ENTRY_MODES = {
    "passive_maker",
    "touch_limit",
    "guarded_fast_1c",
    "guarded_fast_2c",
    "unguarded_fast_1c",
    "unguarded_fast_2c",
    "adaptive_guarded_max1c",
    "adaptive_guarded_max2c",
    "adaptive_guarded_max3c",
    "adaptive_guarded_max2c_min_surplus_1c",
    "adaptive_guarded_max3c_min_surplus_2c",
    "statsbot_current",
    "statsbot_no_surplus",
    "statsbot_stale_grace_0p8",
}
LATCH_MIN_EDGE = 0.01
PM_PRICE_MODEL_KINDS = {"surface_linear", "surface_rbf", "surface_spline"}
MOMENTUM_PRICE_BUCKET_MODEL_KINDS = {"btc_linear_z_momentum_price_bucket"}
PRICE_BUCKET_MODEL_KINDS = {"btc_linear_z_price_bucket", "btc_spline_z_price_bucket", *MOMENTUM_PRICE_BUCKET_MODEL_KINDS}
BTC_ONLY_MODEL_KINDS = {
    "btc_linear_z",
    "btc_spline_z",
    "btc_linear_state",
    "btc_spline_state",
    *PRICE_BUCKET_MODEL_KINDS,
}
SPLINE_MODEL_KINDS = {"surface_spline", "btc_spline_z", "btc_spline_state", "btc_spline_z_price_bucket"}
MOMENTUM_FEATURES = (
    "side_dz_delta_2s",
    "side_ret_bps_2s",
    "side_dz_delta_5s",
    "side_ret_bps_5s",
)
ASYMMETRIC_PRICE_BUCKET_EDGES = (
    (0.20, 0.30, "p20_30"),
    (0.30, 0.40, "p30_40"),
    (0.40, 0.45, "p40_45"),
    (0.45, 0.48, "p45_48"),
    (0.48, 0.52, "p48_52"),
    (0.52, 0.55, "p52_55"),
    (0.55, 0.60, "p55_60"),
    (0.60, 0.70, "p60_70"),
    (0.70, 0.80, "p70_80"),
)
HYBRID_FINE_MID_PRICE_BUCKET_EDGES = (
    (0.20, 0.30, "p20_30"),
    (0.30, 0.40, "p30_40"),
    (0.40, 0.45, "p40_45"),
    (0.45, 0.475, "p45_475"),
    (0.475, 0.50, "p475_50"),
    (0.50, 0.525, "p50_525"),
    (0.525, 0.55, "p525_55"),
    (0.55, 0.60, "p55_60"),
    (0.60, 0.70, "p60_70"),
    (0.70, 0.80, "p70_80"),
)
HYBRID_ULTRA_MID_PRICE_BUCKET_EDGES = (
    (0.20, 0.30, "p20_30"),
    (0.30, 0.40, "p30_40"),
    (0.40, 0.45, "p40_45"),
    (0.45, 0.47, "p45_47"),
    (0.47, 0.49, "p47_49"),
    (0.49, 0.51, "p49_51"),
    (0.51, 0.53, "p51_53"),
    (0.53, 0.55, "p53_55"),
    (0.55, 0.60, "p55_60"),
    (0.60, 0.70, "p60_70"),
    (0.70, 0.80, "p70_80"),
)


def _fixed_price_bucket_edges(step: float) -> tuple[tuple[float, float, str], ...]:
    edges: list[tuple[float, float, str]] = []
    low = 0.20
    while low < 0.80 - 1e-9:
        high = round(low + step, 10)
        label = f"p{int(round(low * 100)):02d}_{int(round(high * 100)):02d}"
        edges.append((round(low, 10), high, label))
        low = high
    return tuple(edges)


PRICE_BUCKET_SCHEMES: dict[str, tuple[tuple[float, float, str], ...]] = {
    "asymmetric": ASYMMETRIC_PRICE_BUCKET_EDGES,
    "current": ASYMMETRIC_PRICE_BUCKET_EDGES,
    "fixed2.5": _fixed_price_bucket_edges(0.025),
    "fixed5": _fixed_price_bucket_edges(0.05),
    "fixed10": _fixed_price_bucket_edges(0.10),
    "hybrid_fine_mid": HYBRID_FINE_MID_PRICE_BUCKET_EDGES,
    "hybrid_ultra_mid": HYBRID_ULTRA_MID_PRICE_BUCKET_EDGES,
}
LOOKBACK_FOLD_SETS: dict[str, tuple[dict[str, str], ...]] = {
    "continuous_recent_lookback_20260713": (
        {
            "id": "recent1m_train1m_cutoff_20260612_test_20260613_20260713",
            "training_start": "2026-05-13",
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-06-13",
            "test_end": "2026-07-13",
        },
        {
            "id": "recent1m_train2m_cutoff_20260612_test_20260613_20260713",
            "training_start": "2026-04-13",
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-06-13",
            "test_end": "2026-07-13",
        },
        {
            "id": "recent1m_train3m_cutoff_20260612_test_20260613_20260713",
            "training_start": "2026-03-13",
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-06-13",
            "test_end": "2026-07-13",
        },
        {
            "id": "recent1m_trainexpanding_cutoff_20260612_test_20260613_20260713",
            "training_start": "2026-02-14",
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-06-13",
            "test_end": "2026-07-13",
        },
        {
            "id": "recent2m_train1m_cutoff_20260512_test_20260513_20260713",
            "training_start": "2026-04-13",
            "outer_cutoff": "2026-05-12",
            "test_start": "2026-05-13",
            "test_end": "2026-07-13",
        },
        {
            "id": "recent2m_train2m_cutoff_20260512_test_20260513_20260713",
            "training_start": "2026-03-13",
            "outer_cutoff": "2026-05-12",
            "test_start": "2026-05-13",
            "test_end": "2026-07-13",
        },
        {
            "id": "recent2m_train3m_expanding_cutoff_20260512_test_20260513_20260713",
            "training_start": "2026-02-14",
            "outer_cutoff": "2026-05-12",
            "test_start": "2026-05-13",
            "test_end": "2026-07-13",
        },
    )
}
REGIME_START = "2026-05-09"
REGIME_END = "2026-06-06"
REGIME_WEEKLY_CUTOFFS = (
    "2026-04-19",
    "2026-04-26",
    "2026-05-03",
    "2026-05-10",
    "2026-05-17",
    "2026-05-24",
    "2026-05-31",
    "2026-06-07",
    "2026-06-14",
    "2026-06-21",
    "2026-06-28",
    "2026-06-30",
)
REGIME_EXPLORATION_FOLD_SET: tuple[dict[str, Any], ...] = tuple(
    {
        "id": f"weekly_cumulative_cutoff_{cutoff.replace('-', '')}_test_20260701_20260713",
        "training_start": "2026-02-14",
        "outer_cutoff": cutoff,
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "weekly_cumulative",
    }
    for cutoff in REGIME_WEEKLY_CUTOFFS
) + (
    {
        "id": "segment_pre_regime_train_20260214_20260508_test_20260701_20260713",
        "training_segments": (("2026-02-14", "2026-05-08"),),
        "outer_cutoff": "2026-05-08",
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
    {
        "id": "segment_regime_only_train_20260509_20260606_test_20260701_20260713",
        "training_segments": ((REGIME_START, REGIME_END),),
        "outer_cutoff": REGIME_END,
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
    {
        "id": "segment_post_regime_train_20260607_20260630_test_20260701_20260713",
        "training_segments": (("2026-06-07", "2026-06-30"),),
        "outer_cutoff": "2026-06-30",
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
    {
        "id": "segment_exclude_regime_train_20260214_20260508_20260607_20260630_test_20260701_20260713",
        "training_segments": (("2026-02-14", "2026-05-08"), ("2026-06-07", "2026-06-30")),
        "outer_cutoff": "2026-06-30",
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
    {
        "id": "segment_pre_plus_regime_train_20260214_20260606_test_20260701_20260713",
        "training_segments": (("2026-02-14", "2026-06-06"),),
        "outer_cutoff": "2026-06-06",
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
    {
        "id": "segment_full_pre_july_train_20260214_20260630_test_20260701_20260713",
        "training_segments": (("2026-02-14", "2026-06-30"),),
        "outer_cutoff": "2026-06-30",
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
    {
        "id": "segment_full_pre_july_downweight_regime_025_test_20260701_20260713",
        "training_segments": (("2026-02-14", "2026-06-30"),),
        "downweight_segments": ((REGIME_START, REGIME_END, 0.25),),
        "outer_cutoff": "2026-06-30",
        "test_start": "2026-07-01",
        "test_end": "2026-07-13",
        "diagnostic_group": "regime_segment",
    },
)
REGIME_FOLD_SETS: dict[str, tuple[dict[str, Any], ...]] = {
    "continuous_regime_inclusion_20260713": REGIME_EXPLORATION_FOLD_SET,
}


def _date_text(value: date) -> str:
    return value.isoformat()


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value))


def normalize_price_bucket_scheme(scheme: str) -> str:
    text = str(scheme).strip().lower()
    if text == "current":
        return "asymmetric"
    if text not in PRICE_BUCKET_SCHEMES:
        raise ValueError(f"Unknown price bucket scheme: {scheme}")
    return text


def price_bucket_edges(scheme: str) -> tuple[tuple[float, float, str], ...]:
    return PRICE_BUCKET_SCHEMES[normalize_price_bucket_scheme(scheme)]


def price_bucket_labels(values: pd.Series | np.ndarray, scheme: str = "asymmetric") -> pd.Series:
    series = pd.to_numeric(values, errors="coerce")
    labels = pd.Series(pd.NA, index=series.index, dtype="string")
    for index, (low, high, label) in enumerate(price_bucket_edges(scheme)):
        if index == 0:
            mask = series.ge(low) & series.le(high)
        else:
            mask = series.gt(low) & series.le(high)
        labels.loc[mask] = label
    return labels


def _segments_minus_blackout(train_start: str, train_end: str, blackout_start: str, blackout_end: str) -> tuple[tuple[str, str], ...]:
    start = _parse_date(train_start)
    end = _parse_date(train_end)
    block_start = _parse_date(blackout_start)
    block_end = _parse_date(blackout_end)
    if block_start > block_end:
        raise ValueError(f"Invalid blackout range: {blackout_start}>{blackout_end}")
    segments: list[tuple[str, str]] = []
    left_end = min(end, block_start - timedelta(days=1))
    if start <= left_end:
        segments.append((_date_text(start), _date_text(left_end)))
    right_start = max(start, block_end + timedelta(days=1))
    if right_start <= end:
        segments.append((_date_text(right_start), _date_text(end)))
    if not segments:
        raise ValueError(f"Blackout removes all training data: {blackout_start}..{blackout_end}")
    return tuple(segments)


def _rolling_blackout_folds(
    *,
    fold_set_label: str,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    blackout_scan_start: str,
    blackout_scan_end: str,
    blackout_lengths: tuple[int, ...],
    step_days: int,
) -> tuple[dict[str, Any], ...]:
    folds: list[dict[str, Any]] = [
        {
            "id": f"{fold_set_label}_baseline_full_train_{train_start.replace('-', '')}_{train_end.replace('-', '')}_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": ((train_start, train_end),),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{fold_set_label}_baseline_apr19_train_20260214_20260419_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": (("2026-02-14", "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{fold_set_label}_baseline_apr26_train_20260214_20260426_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": (("2026-02-14", "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{fold_set_label}_baseline_exclude_may09_jun06_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": _segments_minus_blackout(train_start, train_end, REGIME_START, REGIME_END),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
            "blackout_start": REGIME_START,
            "blackout_end": REGIME_END,
            "blackout_length_days": 29,
        },
    ]
    scan_start = _parse_date(blackout_scan_start)
    scan_end = _parse_date(blackout_scan_end)
    for length in blackout_lengths:
        start = scan_start
        while start + timedelta(days=int(length) - 1) <= scan_end:
            end = start + timedelta(days=int(length) - 1)
            start_text = _date_text(start)
            end_text = _date_text(end)
            folds.append(
                {
                    "id": (
                        f"{fold_set_label}_blackout_{int(length):02d}d_"
                        f"{start_text.replace('-', '')}_{end_text.replace('-', '')}_"
                        f"test_{test_start.replace('-', '')}_{test_end.replace('-', '')}"
                    ),
                    "training_segments": _segments_minus_blackout(train_start, train_end, start_text, end_text),
                    "outer_cutoff": train_end,
                    "test_start": test_start,
                    "test_end": test_end,
                    "diagnostic_group": "rolling_blackout",
                    "blackout_start": start_text,
                    "blackout_end": end_text,
                    "blackout_length_days": int(length),
                }
            )
            start += timedelta(days=int(step_days))
    return tuple(folds)


def _rolling_blackout_folds_by_start(
    *,
    fold_set_label: str,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    blackout_start_min: str,
    blackout_start_max: str,
    blackout_lengths: tuple[int, ...],
) -> tuple[dict[str, Any], ...]:
    folds: list[dict[str, Any]] = [
        {
            "id": f"{fold_set_label}_baseline_full_train_{train_start.replace('-', '')}_{train_end.replace('-', '')}_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": ((train_start, train_end),),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{fold_set_label}_baseline_apr19_train_20260214_20260419_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": (("2026-02-14", "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{fold_set_label}_baseline_apr26_train_20260214_20260426_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": (("2026-02-14", "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{fold_set_label}_baseline_exclude_may09_jun06_test_{test_start.replace('-', '')}_{test_end.replace('-', '')}",
            "training_segments": _segments_minus_blackout(train_start, train_end, REGIME_START, REGIME_END),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
            "blackout_start": REGIME_START,
            "blackout_end": REGIME_END,
            "blackout_length_days": 29,
        },
    ]
    start = _parse_date(blackout_start_min)
    start_max = _parse_date(blackout_start_max)
    training_end = _parse_date(train_end)
    for length in blackout_lengths:
        current = start
        while current <= start_max:
            end = current + timedelta(days=int(length) - 1)
            if end <= training_end:
                start_text = _date_text(current)
                end_text = _date_text(end)
                folds.append(
                    {
                        "id": (
                            f"{fold_set_label}_blackout_{int(length):02d}d_"
                            f"{start_text.replace('-', '')}_{end_text.replace('-', '')}_"
                            f"test_{test_start.replace('-', '')}_{test_end.replace('-', '')}"
                        ),
                        "training_segments": _segments_minus_blackout(train_start, train_end, start_text, end_text),
                        "outer_cutoff": train_end,
                        "test_start": test_start,
                        "test_end": test_end,
                        "diagnostic_group": "rolling_blackout",
                        "blackout_start": start_text,
                        "blackout_end": end_text,
                        "blackout_length_days": int(length),
                    }
                )
            current += timedelta(days=1)
    return tuple(folds)


def _pinpoint_stage1_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    train_end = "2026-06-12"
    test_start = "2026-06-13"
    test_end = "2026-07-13"
    label = "blackout_pinpoint_stage1_1m"
    folds: list[dict[str, Any]] = [
        {
            "id": f"{label}_baseline_full_train_20260214_20260612_test_20260613_20260713",
            "training_segments": ((train_start, train_end),),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{label}_baseline_apr19_train_20260214_20260419_test_20260613_20260713",
            "training_segments": (("2026-02-14", "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{label}_baseline_apr26_train_20260214_20260426_test_20260613_20260713",
            "training_segments": (("2026-02-14", "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
        },
        {
            "id": f"{label}_baseline_exclude_apr29_may18_test_20260613_20260713",
            "training_segments": _segments_minus_blackout(train_start, train_end, "2026-04-29", "2026-05-18"),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": f"{label}_baseline_exclude_apr26_may23_test_20260613_20260713",
            "training_segments": _segments_minus_blackout(train_start, train_end, "2026-04-26", "2026-05-23"),
            "outer_cutoff": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "blackout_baseline",
            "blackout_start": "2026-04-26",
            "blackout_end": "2026-05-23",
            "blackout_length_days": 28,
        },
    ]
    rolling = _rolling_blackout_folds_by_start(
        fold_set_label=label,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        blackout_start_min="2026-04-26",
        blackout_start_max="2026-05-08",
        blackout_lengths=(14, 21, 28),
    )
    folds.extend(fold for fold in rolling if fold.get("diagnostic_group") == "rolling_blackout")
    return tuple(folds)


def _targeted_training_bakeoff_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    common_train_end = "2026-06-12"
    common_test_start = "2026-06-13"
    common_test_end = "2026-07-13"
    return (
        {
            "id": "bakeoff_A_long_train_20260214_20260419_test_20260420_20260713",
            "training_recipe": "A_Feb14_Apr19",
            "training_segments": ((train_start, "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": "2026-04-20",
            "test_end": "2026-07-13",
            "diagnostic_group": "targeted_bakeoff_long",
        },
        {
            "id": "bakeoff_B_long_train_20260214_20260426_test_20260427_20260713",
            "training_recipe": "B_Feb14_Apr26",
            "training_segments": ((train_start, "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": "2026-04-27",
            "test_end": "2026-07-13",
            "diagnostic_group": "targeted_bakeoff_long",
        },
        {
            "id": "bakeoff_A_common_train_20260214_20260419_test_20260613_20260713",
            "training_recipe": "A_Feb14_Apr19",
            "training_segments": ((train_start, "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": common_test_start,
            "test_end": common_test_end,
            "diagnostic_group": "targeted_bakeoff_common",
        },
        {
            "id": "bakeoff_B_common_train_20260214_20260426_test_20260613_20260713",
            "training_recipe": "B_Feb14_Apr26",
            "training_segments": ((train_start, "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": common_test_start,
            "test_end": common_test_end,
            "diagnostic_group": "targeted_bakeoff_common",
        },
        {
            "id": "bakeoff_C_common_train_20260214_20260612_excl_20260429_20260518_test_20260613_20260713",
            "training_recipe": "C_Feb14_Jun12_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, common_train_end, "2026-04-29", "2026-05-18"),
            "outer_cutoff": common_train_end,
            "test_start": common_test_start,
            "test_end": common_test_end,
            "diagnostic_group": "targeted_bakeoff_common",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "bakeoff_D_common_train_20260214_20260612_excl_20260501_20260521_test_20260613_20260713",
            "training_recipe": "D_Feb14_Jun12_excl_May01_May21",
            "training_segments": _segments_minus_blackout(train_start, common_train_end, "2026-05-01", "2026-05-21"),
            "outer_cutoff": common_train_end,
            "test_start": common_test_start,
            "test_end": common_test_end,
            "diagnostic_group": "targeted_bakeoff_common",
            "blackout_start": "2026-05-01",
            "blackout_end": "2026-05-21",
            "blackout_length_days": 21,
        },
        {
            "id": "bakeoff_E_common_train_20260214_20260612_excl_20260426_20260523_test_20260613_20260713",
            "training_recipe": "E_Feb14_Jun12_excl_Apr26_May23",
            "training_segments": _segments_minus_blackout(train_start, common_train_end, "2026-04-26", "2026-05-23"),
            "outer_cutoff": common_train_end,
            "test_start": common_test_start,
            "test_end": common_test_end,
            "diagnostic_group": "targeted_bakeoff_common",
            "blackout_start": "2026-04-26",
            "blackout_end": "2026-05-23",
            "blackout_length_days": 28,
        },
    )


def _btc_only_fair_value_folds() -> tuple[dict[str, Any], ...]:
    return tuple(
        fold
        for fold in _targeted_training_bakeoff_folds()
        if str(fold.get("diagnostic_group")) == "targeted_bakeoff_common"
    )


def _execution_holdout_jul25_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    return (
        {
            "id": "exec_holdout_A_train_20260214_20260612_excl_20260429_20260518_test_20260714_20260725",
            "training_recipe": "A_Feb14_Jun12_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-12", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-07-14",
            "test_end": "2026-07-25",
            "diagnostic_group": "execution_holdout_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "exec_holdout_B_train_20260214_20260713_excl_20260429_20260518_test_20260714_20260725",
            "training_recipe": "B_Feb14_Jul13_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-07-13", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-07-13",
            "test_start": "2026-07-14",
            "test_end": "2026-07-25",
            "diagnostic_group": "execution_holdout_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "exec_holdout_C_train_20260214_20260426_test_20260714_20260725",
            "training_recipe": "C_Feb14_Apr26",
            "training_segments": ((train_start, "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": "2026-07-14",
            "test_end": "2026-07-25",
            "diagnostic_group": "execution_holdout_jul25",
        },
        {
            "id": "exec_holdout_D_train_20260214_20260720_excl_20260429_20260518_test_20260721_20260725",
            "training_recipe": "D_Feb14_Jul20_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-07-20", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-07-20",
            "test_start": "2026-07-21",
            "test_end": "2026-07-25",
            "diagnostic_group": "execution_holdout_jul25_adaptive",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
    )


def _execution_recent31_jul25_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    return (
        {
            "id": "exec_recent31_train_20260214_20260624_excl_20260429_20260518_test_20260625_20260725",
            "training_recipe": "Feb14_Jun24_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-24", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-24",
            "test_start": "2026-06-25",
            "test_end": "2026-07-25",
            "diagnostic_group": "execution_recent31_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
    )


def _degradation_jul25_folds() -> tuple[dict[str, Any], ...]:
    return (
        {
            "id": "degradation_jul25_e1_cutoff_20260419_test_20260420_20260725",
            "training_segments": (("2026-02-14", "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": "2026-04-20",
            "test_end": "2026-07-25",
            "diagnostic_group": "degradation_jul25",
        },
        {
            "id": "degradation_jul25_e2_cutoff_20260517_test_20260518_20260725",
            "training_segments": (("2026-02-14", "2026-05-17"),),
            "outer_cutoff": "2026-05-17",
            "test_start": "2026-05-18",
            "test_end": "2026-07-25",
            "diagnostic_group": "degradation_jul25",
        },
        {
            "id": "degradation_jul25_e3_cutoff_20260614_test_20260615_20260725",
            "training_segments": (("2026-02-14", "2026-06-14"),),
            "outer_cutoff": "2026-06-14",
            "test_start": "2026-06-15",
            "test_end": "2026-07-25",
            "diagnostic_group": "degradation_jul25",
        },
    )


def _stage3_training_bakeoff_jul25_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    test_start = "2026-06-25"
    test_end = "2026-07-25"
    return (
        {
            "id": "stage3_A_train_20260214_20260419_test_20260625_20260725",
            "training_recipe": "A_Feb14_Apr19",
            "training_segments": ((train_start, "2026-04-19"),),
            "outer_cutoff": "2026-04-19",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "stage3_training_bakeoff_jul25",
        },
        {
            "id": "stage3_B_train_20260214_20260426_test_20260625_20260725",
            "training_recipe": "B_Feb14_Apr26",
            "training_segments": ((train_start, "2026-04-26"),),
            "outer_cutoff": "2026-04-26",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "stage3_training_bakeoff_jul25",
        },
        {
            "id": "stage3_C_train_20260214_20260612_excl_20260429_20260518_test_20260625_20260725",
            "training_recipe": "C_Feb14_Jun12_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-12", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-12",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "stage3_training_bakeoff_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "stage3_D_train_20260214_20260624_excl_20260429_20260518_test_20260625_20260725",
            "training_recipe": "D_Feb14_Jun24_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-24", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-24",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "stage3_training_bakeoff_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "stage3_E_train_20260313_20260624_excl_20260429_20260518_test_20260625_20260725",
            "training_recipe": "E_Mar13_Jun24_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout("2026-03-13", "2026-06-24", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-24",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "stage3_training_bakeoff_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "stage3_F_train_20260519_20260624_test_20260625_20260725",
            "training_recipe": "F_May19_Jun24",
            "training_segments": (("2026-05-19", "2026-06-24"),),
            "outer_cutoff": "2026-06-24",
            "test_start": test_start,
            "test_end": test_end,
            "diagnostic_group": "stage3_training_bakeoff_jul25",
        },
    )


def _stage4_refinement_jul25_folds() -> tuple[dict[str, Any], ...]:
    return tuple(
        fold
        for fold in _stage3_training_bakeoff_jul25_folds()
        if str(fold.get("training_recipe", "")).startswith(("D_", "E_", "F_"))
    )


def _stage5_edge_sweep_jul25_folds() -> tuple[dict[str, Any], ...]:
    return tuple(
        fold
        for fold in _stage3_training_bakeoff_jul25_folds()
        if str(fold.get("training_recipe", "")).startswith(("D_", "E_"))
    )


def _stage6_time_edge_degradation_jul25_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    return (
        {
            "id": "stage6_A_train_20260214_20260531_excl_20260429_20260518_test_20260601_20260725",
            "training_recipe": "A_Feb14_May31_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-05-31", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-05-31",
            "test_start": "2026-06-01",
            "test_end": "2026-07-25",
            "diagnostic_group": "stage6_time_edge_degradation_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "stage6_B_train_20260214_20260612_excl_20260429_20260518_test_20260613_20260725",
            "training_recipe": "B_Feb14_Jun12_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-12", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-06-13",
            "test_end": "2026-07-25",
            "diagnostic_group": "stage6_time_edge_degradation_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "stage6_C_train_20260214_20260624_excl_20260429_20260518_test_20260625_20260725",
            "training_recipe": "C_Feb14_Jun24_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-24", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-24",
            "test_start": "2026-06-25",
            "test_end": "2026-07-25",
            "diagnostic_group": "stage6_time_edge_degradation_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "stage6_D_train_20260214_20260710_excl_20260429_20260518_test_20260711_20260725",
            "training_recipe": "D_Feb14_Jul10_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-07-10", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-07-10",
            "test_start": "2026-07-11",
            "test_end": "2026-07-25",
            "diagnostic_group": "stage6_time_edge_degradation_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
    )


def _layer2_edge_selector_train_jul25_folds() -> tuple[dict[str, Any], ...]:
    """Replay fold training segments so Layer 2 can learn edges out of sample.

    The value model is intentionally trained on these same segments.  This fold
    set is not a model OOS evaluation; it creates selector-training fill tapes.
    Held-out scoring must happen against the matching Stage 6 test folds.
    """
    folds: list[dict[str, Any]] = []
    for fold in _stage6_time_edge_degradation_jul25_folds():
        training_segments = tuple((str(start), str(end)) for start, end in fold["training_segments"])
        folds.append(
            {
                **fold,
                "id": str(fold["id"]).replace("stage6_", "layer2_edge_train_", 1),
                "diagnostic_group": "layer2_edge_selector_train_jul25",
                "test_start": training_segments[0][0],
                "test_end": training_segments[-1][1],
                "test_segments": training_segments,
                "allow_train_test_overlap": True,
            }
        )
    return tuple(folds)


def _large_edge_selector_full_train_jul25_folds() -> tuple[dict[str, Any], ...]:
    """Large overlapping edge-selector replay for March-July edge research.

    This intentionally trains the value surface on all available clean data and
    replays a large overlapping history.  It is for edge-application diagnostics,
    not a clean model OOS evaluation.
    """
    train_start = "2026-02-14"
    return (
        {
            "id": "stage6_A_large_train_20260214_20260725_excl_20260429_20260518_test_20260301_20260725",
            "training_recipe": "A_Feb14_Jul25_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-07-25", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-07-25",
            "test_start": "2026-03-01",
            "test_end": "2026-07-25",
            "diagnostic_group": "large_edge_selector_full_train_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
            "allow_train_test_overlap": True,
        },
    )


def _edge_selector_probe_jul25_folds() -> tuple[dict[str, Any], ...]:
    train_start = "2026-02-14"
    return (
        {
            "id": "edge_selector_A_train_20260214_20260531_excl_20260429_20260518_test_20260601_20260614",
            "training_recipe": "A_Feb14_May31_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-05-31", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-05-31",
            "test_start": "2026-06-01",
            "test_end": "2026-06-14",
            "diagnostic_group": "edge_selector_probe_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "edge_selector_B_train_20260214_20260612_excl_20260429_20260518_test_20260613_20260626",
            "training_recipe": "B_Feb14_Jun12_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-12", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-12",
            "test_start": "2026-06-13",
            "test_end": "2026-06-26",
            "diagnostic_group": "edge_selector_probe_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "edge_selector_C_train_20260214_20260624_excl_20260429_20260518_test_20260625_20260708",
            "training_recipe": "C_Feb14_Jun24_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-06-24", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-06-24",
            "test_start": "2026-06-25",
            "test_end": "2026-07-08",
            "diagnostic_group": "edge_selector_probe_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
        {
            "id": "edge_selector_D_train_20260214_20260710_excl_20260429_20260518_test_20260711_20260725",
            "training_recipe": "D_Feb14_Jul10_excl_Apr29_May18",
            "training_segments": _segments_minus_blackout(train_start, "2026-07-10", "2026-04-29", "2026-05-18"),
            "outer_cutoff": "2026-07-10",
            "test_start": "2026-07-11",
            "test_end": "2026-07-25",
            "diagnostic_group": "edge_selector_probe_jul25",
            "blackout_start": "2026-04-29",
            "blackout_end": "2026-05-18",
            "blackout_length_days": 20,
        },
    )


BLACKOUT_FOLD_SETS: dict[str, tuple[dict[str, Any], ...]] = {
    "continuous_regime_blackout_1m_20260713": _rolling_blackout_folds(
        fold_set_label="blackout_1m_step3",
        train_start="2026-02-14",
        train_end="2026-06-12",
        test_start="2026-06-13",
        test_end="2026-07-13",
        blackout_scan_start="2026-04-20",
        blackout_scan_end="2026-06-12",
        blackout_lengths=(7, 14, 28),
        step_days=3,
    ),
    "continuous_regime_blackout_1m_dense_20260713": _rolling_blackout_folds(
        fold_set_label="blackout_1m_daily",
        train_start="2026-02-14",
        train_end="2026-06-12",
        test_start="2026-06-13",
        test_end="2026-07-13",
        blackout_scan_start="2026-04-20",
        blackout_scan_end="2026-06-12",
        blackout_lengths=(7, 14, 28),
        step_days=1,
    ),
    "continuous_regime_blackout_pinpoint_heavy_1m_20260713": _rolling_blackout_folds_by_start(
        fold_set_label="blackout_pinpoint_heavy_1m",
        train_start="2026-02-14",
        train_end="2026-06-12",
        test_start="2026-06-13",
        test_end="2026-07-13",
        blackout_start_min="2026-04-23",
        blackout_start_max="2026-05-20",
        blackout_lengths=(7, 10, 14, 21, 28),
    ),
    "continuous_regime_blackout_pinpoint_stage1_1m_20260713": _pinpoint_stage1_folds(),
    "continuous_training_candidate_bakeoff_20260713": _targeted_training_bakeoff_folds(),
    "continuous_btc_only_fair_value_common_20260713": _btc_only_fair_value_folds(),
    "continuous_execution_holdout_jul25_20260725": _execution_holdout_jul25_folds(),
    "continuous_execution_recent31_jul25_20260725": _execution_recent31_jul25_folds(),
    "continuous_degradation_jul25_20260725": _degradation_jul25_folds(),
    "continuous_stage3_training_bakeoff_jul25_20260725": _stage3_training_bakeoff_jul25_folds(),
    "continuous_stage4_refinement_jul25_20260725": _stage4_refinement_jul25_folds(),
    "continuous_stage5_edge_sweep_jul25_20260725": _stage5_edge_sweep_jul25_folds(),
    "continuous_stage6_time_edge_degradation_jul25_20260725": _stage6_time_edge_degradation_jul25_folds(),
    "continuous_layer2_edge_selector_train_jul25_20260725": _layer2_edge_selector_train_jul25_folds(),
    "continuous_large_edge_selector_full_train_jul25_20260725": _large_edge_selector_full_train_jul25_folds(),
    "continuous_edge_selector_probe_jul25_20260725": _edge_selector_probe_jul25_folds(),
}
ALL_FOLD_SETS = {**FOLD_SETS, **LOOKBACK_FOLD_SETS, **REGIME_FOLD_SETS, **BLACKOUT_FOLD_SETS}


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value).split(",") if item.strip())


def parse_candidate_allowlist(value: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in parse_csv(value):
        if "|" not in item:
            raise argparse.ArgumentTypeError(
                "candidate allowlist entries must be '<surface_arm>|<spec_id>'"
            )
        surface_arm, spec_id = item.split("|", 1)
        surface_arm = surface_arm.strip()
        spec_id = spec_id.strip()
        if not surface_arm or not spec_id:
            raise argparse.ArgumentTypeError(
                "candidate allowlist entries must be '<surface_arm>|<spec_id>'"
            )
        pairs.append((surface_arm, spec_id))
    return tuple(pairs)


def parse_momentum_features(value: str) -> tuple[str, ...]:
    features = parse_csv(value)
    invalid = sorted(set(features) - set(MOMENTUM_FEATURES))
    if invalid:
        raise argparse.ArgumentTypeError(f"invalid momentum features: {invalid}; valid={MOMENTUM_FEATURES}")
    if not features:
        raise argparse.ArgumentTypeError("at least one momentum feature is required")
    return features


def parse_floats(value: str) -> tuple[float, ...]:
    return tuple(float(item.strip()) for item in str(value).split(",") if item.strip())


def parse_spline_bases(value: str) -> tuple[tuple[int, int], ...]:
    bases: list[tuple[int, int]] = []
    for item in str(value).split(","):
        text = item.strip().lower()
        if not text:
            continue
        if "x" not in text:
            raise argparse.ArgumentTypeError("spline bases must look like 5x5,7x6,9x7")
        raw_z, raw_p = text.split("x", 1)
        z_basis = int(raw_z.strip())
        p_basis = int(raw_p.strip())
        if z_basis < 3 or p_basis < 3:
            raise argparse.ArgumentTypeError("spline basis counts must be >= 3")
        pair = (z_basis, p_basis)
        if pair not in bases:
            bases.append(pair)
    if not bases:
        raise argparse.ArgumentTypeError("at least one spline basis is required")
    return tuple(bases)


def parse_recency_pairs(value: str) -> tuple[tuple[int | None, int | None], ...]:
    pairs: list[tuple[int | None, int | None]] = []
    for item in str(value).split(","):
        text = item.strip()
        if not text:
            continue
        if "/" not in text:
            raise argparse.ArgumentTypeError("recency pairs must look like WD/WE, e.g. 90/90,120/120,LIFETIME/LIFETIME")
        raw_weekday, raw_weekend = text.split("/", 1)
        weekday = parse_half_lives(raw_weekday.strip())
        weekend = parse_half_lives(raw_weekend.strip())
        if len(weekday) != 1 or len(weekend) != 1:
            raise argparse.ArgumentTypeError("each recency pair must contain exactly one weekday and one weekend half-life")
        pair = (weekday[0], weekend[0])
        if pair not in pairs:
            pairs.append(pair)
    if not pairs:
        raise argparse.ArgumentTypeError("at least one recency pair is required")
    return tuple(pairs)


def parse_price_bucket_schemes(value: str) -> tuple[str, ...]:
    schemes: list[str] = []
    for item in str(value).split(","):
        text = item.strip()
        if not text:
            continue
        try:
            scheme = normalize_price_bucket_scheme(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        if scheme not in schemes:
            schemes.append(scheme)
    if not schemes:
        raise argparse.ArgumentTypeError("at least one price bucket scheme is required")
    return tuple(schemes)


def parse_execution_latency_specs(value: str) -> tuple[str, ...]:
    specs: list[str] = []
    for item in str(value).replace(";", ",").split(","):
        text = item.strip()
        if not text:
            continue
        upper = text.upper()
        if upper == "EMPIRICAL":
            label = "empirical"
        else:
            raw = upper.removesuffix("MS")
            try:
                ms = int(raw)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    "execution latencies must be milliseconds or EMPIRICAL, e.g. 300,500,EMPIRICAL"
                ) from exc
            if ms < 0:
                raise argparse.ArgumentTypeError("execution latency milliseconds must be >= 0")
            label = f"{ms}ms"
        if label not in specs:
            specs.append(label)
    if not specs:
        raise argparse.ArgumentTypeError("at least one execution latency is required")
    return tuple(specs)


def parse_execution_entry_modes(value: str) -> tuple[str, ...]:
    modes: list[str] = []
    for item in str(value).replace(";", ",").split(","):
        text = item.strip().lower()
        if not text:
            continue
        if text not in EXECUTION_ENTRY_MODES:
            raise argparse.ArgumentTypeError(
                f"invalid execution entry mode: {text}; valid={sorted(EXECUTION_ENTRY_MODES)}"
            )
        if text not in modes:
            modes.append(text)
    if not modes:
        raise argparse.ArgumentTypeError("at least one execution entry mode is required")
    return tuple(modes)


def adaptive_guarded_mode_config(mode: str) -> tuple[int, float] | None:
    configs = {
        "adaptive_guarded_max1c": (1, 0.0),
        "adaptive_guarded_max2c": (2, 0.0),
        "adaptive_guarded_max3c": (3, 0.0),
        "adaptive_guarded_max2c_min_surplus_1c": (2, 0.01),
        "adaptive_guarded_max3c_min_surplus_2c": (3, 0.02),
    }
    return configs.get(str(mode))


def statsbot_execution_mode_config(mode: str) -> dict[str, float] | None:
    configs = {
        "statsbot_current": {
            "surplus_adaptive": 1.0,
            "resting_max_age_seconds": 3.0,
            "stale_quote_max_ticks": 2.0,
            "stale_quote_grace_seconds": 0.4,
        },
        "statsbot_no_surplus": {
            "surplus_adaptive": 0.0,
            "resting_max_age_seconds": 3.0,
            "stale_quote_max_ticks": 2.0,
            "stale_quote_grace_seconds": 0.4,
        },
        "statsbot_stale_grace_0p8": {
            "surplus_adaptive": 1.0,
            "resting_max_age_seconds": 3.0,
            "stale_quote_max_ticks": 2.0,
            "stale_quote_grace_seconds": 0.8,
        },
    }
    return configs.get(str(mode))


def statsbot_limit_price(
    ask: pd.Series,
    value: pd.Series,
    threshold: pd.Series,
    max_price: float,
    *,
    surplus_adaptive: bool,
) -> pd.Series:
    tick = 0.01
    base_ticks = 1
    base_limit = (np.minimum(ask + base_ticks * tick, max_price) * 100.0).round() / 100.0
    limit = pd.Series(base_limit, index=ask.index, dtype=float)
    if not surplus_adaptive:
        return limit
    surplus_at_base = value - limit - threshold
    spend_ticks = pd.Series(0, index=ask.index, dtype=int)
    eligible = surplus_at_base.ge(0.015 - 1e-12)
    steps = np.floor((surplus_at_base - 0.015 + 1e-12) / 0.010).clip(lower=0)
    spend_ticks.loc[eligible] = np.minimum(2, np.maximum(1, steps.loc[eligible].astype(int) + 1))
    adaptive_limit = (np.minimum(ask + (base_ticks + spend_ticks) * tick, max_price) * 100.0).round() / 100.0
    adaptive_limit = pd.Series(adaptive_limit, index=ask.index, dtype=float)
    edge_survives = (value - adaptive_limit).ge(threshold - 1e-12)
    return pd.Series(np.where(edge_survives, adaptive_limit, limit), index=ask.index, dtype=float)


def parse_execution_latches(value: str) -> tuple[tuple[str, float], ...]:
    latches: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in str(value).replace(";", ",").split(","):
        text = item.strip().lower()
        if not text:
            continue
        if text in {"none", "no", "off", "no_latch", "0", "0s"}:
            label, seconds = "no_latch", 0.0
        else:
            raw = text.removesuffix("sec").removesuffix("s")
            try:
                seconds = float(raw)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    "execution latches must look like no_latch,1s,2s,3s"
                ) from exc
            if seconds < 0.0:
                raise argparse.ArgumentTypeError("execution latch seconds must be >= 0")
            if seconds.is_integer():
                label = f"{int(seconds)}s"
            else:
                label = f"{seconds:g}s"
        if label not in seen:
            latches.append((label, seconds))
            seen.add(label)
    if not latches:
        raise argparse.ArgumentTypeError("at least one execution latch is required")
    return tuple(latches)


def surface_config_id(day_type_mode: str, weekday_hl: int | None, weekend_hl: int | None) -> str:
    if day_type_mode == "included":
        return f"dt_included__WD{half_life_label(weekday_hl)}__WE{half_life_label(weekend_hl)}"
    if day_type_mode == "excluded":
        return f"dt_excluded__REC{half_life_label(weekday_hl)}"
    raise ValueError(f"Unknown day type mode: {day_type_mode}")


def model_variant_id(variant: dict[str, Any], side_mode: str) -> str:
    scheme_suffix = ""
    if variant["model_kind"] in PRICE_BUCKET_MODEL_KINDS:
        scheme_suffix = f"__pb{normalize_price_bucket_scheme(str(variant.get('price_bucket_scheme', 'asymmetric')))}"
    momentum_suffix = ""
    if variant["model_kind"] in MOMENTUM_PRICE_BUCKET_MODEL_KINDS:
        momentum_suffix = f"__mom{str(variant.get('momentum_feature', '')).replace('_', '')}"
    if variant["model_kind"] == "surface_spline":
        return (
            f"{variant['model_kind']}__{side_mode}"
            f"__b{int(variant['spline_z_basis'])}x{int(variant['spline_p_basis'])}"
            f"__p{float(variant['spline_penalty']):g}"
        )
    if variant["model_kind"] in {"btc_spline_z", "btc_spline_state", "btc_spline_z_price_bucket"}:
        return (
            f"{variant['model_kind']}__{side_mode}"
            f"__b{int(variant['spline_z_basis'])}"
            f"__p{float(variant['spline_penalty']):g}"
            f"{scheme_suffix}"
        )
    return f"{variant['model_kind']}__{side_mode}__C{float(variant['c_value']):g}{scheme_suffix}{momentum_suffix}"


def iter_model_variants(
    model_kinds: tuple[str, ...],
    c_values: tuple[float, ...],
    spline_bases: tuple[tuple[int, int], ...],
    spline_penalties: tuple[float, ...],
    momentum_features: tuple[str, ...] = MOMENTUM_FEATURES,
) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for model_kind in model_kinds:
        if model_kind == "surface_spline":
            for z_basis, p_basis in spline_bases:
                for penalty in spline_penalties:
                    variants.append(
                        {
                            "model_kind": model_kind,
                            "c_value": 1.0 / max(float(penalty), 1e-9),
                            "spline_z_basis": int(z_basis),
                            "spline_p_basis": int(p_basis),
                            "spline_penalty": float(penalty),
                        }
                    )
        elif model_kind in {"btc_spline_z", "btc_spline_state", "btc_spline_z_price_bucket"}:
            for z_basis in sorted({int(z_basis) for z_basis, _p_basis in spline_bases}):
                for penalty in spline_penalties:
                    variants.append(
                        {
                            "model_kind": model_kind,
                            "c_value": 1.0 / max(float(penalty), 1e-9),
                            "spline_z_basis": int(z_basis),
                            "spline_p_basis": 0,
                            "spline_penalty": float(penalty),
                        }
                    )
        elif model_kind in MOMENTUM_PRICE_BUCKET_MODEL_KINDS:
            for c_value in c_values:
                for momentum_feature in momentum_features:
                    variants.append(
                        {
                            "model_kind": model_kind,
                            "c_value": float(c_value),
                            "spline_z_basis": 7,
                            "spline_p_basis": 6,
                            "spline_penalty": 10.0,
                            "momentum_feature": str(momentum_feature),
                        }
                    )
        else:
            for c_value in c_values:
                variants.append(
                    {
                        "model_kind": model_kind,
                        "c_value": float(c_value),
                        "spline_z_basis": 7,
                        "spline_p_basis": 6,
                        "spline_penalty": 10.0,
                        "momentum_feature": "",
                    }
                )
    if not variants:
        raise argparse.ArgumentTypeError("at least one model variant is required")
    return variants


def expand_price_bucket_variants(
    variants: list[dict[str, Any]],
    price_bucket_schemes: tuple[str, ...],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for variant in variants:
        if variant["model_kind"] in PRICE_BUCKET_MODEL_KINDS:
            for scheme in price_bucket_schemes:
                item = dict(variant)
                item["price_bucket_scheme"] = normalize_price_bucket_scheme(scheme)
                expanded.append(item)
        else:
            item = dict(variant)
            item["price_bucket_scheme"] = ""
            expanded.append(item)
    return expanded


def logit(values: pd.Series | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    clipped = np.clip(arr, EPS, 1.0 - EPS)
    return np.log(clipped / (1.0 - clipped))


def read_observations(distance_root: Path, cutoff: str, start_date: str = "") -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    root = distance_root / "observations"
    columns = [
        "timestamp_utc",
        "date_utc",
        "canonical_window_id",
        "side",
        "day_type",
        "tte_seconds",
        "rv_1h_bps",
        "distance_to_beat_bps",
        "side_distance_to_beat_bps",
        "side_distance_to_beat_z",
        "selected_bid",
        "selected_ask",
        "side_win",
    ]
    for path in sorted(root.glob("date_utc=*/part.parquet")):
        day = path.parent.name.split("=", 1)[-1]
        if day <= cutoff and (not start_date or day >= start_date):
            frames.append(pd.read_parquet(path, columns=columns))
    if not frames:
        raise FileNotFoundError(f"No distance observations in {root} through {cutoff}")
    return pd.concat(frames, ignore_index=True, sort=False)


def read_observations_for_segments(distance_root: Path, segments: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    root = distance_root / "observations"
    columns = [
        "timestamp_utc",
        "date_utc",
        "canonical_window_id",
        "side",
        "day_type",
        "tte_seconds",
        "rv_1h_bps",
        "distance_to_beat_bps",
        "side_distance_to_beat_bps",
        "side_distance_to_beat_z",
        "selected_bid",
        "selected_ask",
        "side_win",
    ]
    normalized = tuple((str(start), str(end)) for start, end in segments)
    for path in sorted(root.glob("date_utc=*/part.parquet")):
        day = path.parent.name.split("=", 1)[-1]
        if any(start <= day <= end for start, end in normalized):
            frames.append(pd.read_parquet(path, columns=columns))
    if not frames:
        raise FileNotFoundError(f"No distance observations in {root} for segments={normalized}")
    return pd.concat(frames, ignore_index=True, sort=False)


def ewma_half_life_minutes(distance_z_source: str) -> int | None:
    source = str(distance_z_source).lower()
    if source == "flat_60m":
        return None
    if source == "ewma_hl5":
        return 5
    if source == "ewma_hl10":
        return 10
    raise ValueError(f"Unsupported distance-z source: {distance_z_source}")


def build_distance_z_vol_lookup(distance_root: Path, distance_z_source: str) -> pd.DataFrame:
    half_life = ewma_half_life_minutes(distance_z_source)
    if half_life is None:
        return pd.DataFrame()
    minute_path = distance_root / "minute_regimes.parquet"
    if not minute_path.exists():
        raise FileNotFoundError(f"Missing minute regimes for EWMA distance-z source: {minute_path}")
    minute = pd.read_parquet(minute_path, columns=["minute_utc", "cb_close_1s"])
    minute["minute_utc"] = pd.to_datetime(minute["minute_utc"], utc=True)
    price = pd.to_numeric(minute["cb_close_1s"], errors="coerce").ffill()
    returns = np.log(price.where(price.gt(0))).diff()
    min_periods = max(10, int(half_life * 3))
    ewma_var = returns.pow(2).ewm(halflife=half_life, adjust=False, min_periods=min_periods).mean()
    out = pd.DataFrame(
        {
            "minute_utc": minute["minute_utc"] + pd.Timedelta(minutes=1),
            "distance_z_rv_bps": np.sqrt(60.0 * ewma_var) * 10_000.0,
        }
    )
    return out.dropna(subset=["distance_z_rv_bps"]).drop_duplicates("minute_utc", keep="last").reset_index(drop=True)


def apply_distance_z_source(
    frame: pd.DataFrame,
    *,
    distance_z_source: str,
    vol_lookup: pd.DataFrame,
    vol_floor_bps: float = 0.05,
) -> pd.DataFrame:
    if str(distance_z_source).lower() == "flat_60m":
        return frame
    if vol_lookup.empty:
        raise ValueError(f"distance_z_source={distance_z_source} requires a non-empty EWMA vol lookup")
    out = frame.copy()
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out["_distance_z_minute_utc"] = out["timestamp_utc"].dt.floor("min")
    lookup = vol_lookup.rename(columns={"minute_utc": "_distance_z_minute_utc"})
    out = out.merge(lookup, on="_distance_z_minute_utc", how="left", validate="many_to_one")
    tte_col = "tte_seconds" if "tte_seconds" in out.columns else "canonical_tte_seconds"
    tte = pd.to_numeric(out[tte_col], errors="coerce")
    rv = pd.to_numeric(out["distance_z_rv_bps"], errors="coerce")
    remaining = rv * np.sqrt(tte.clip(lower=1.0) / 3600.0)
    denom = remaining.clip(lower=float(vol_floor_bps))
    if "distance_to_beat_bps" in out.columns:
        neutral_bps = pd.to_numeric(out["distance_to_beat_bps"], errors="coerce")
        out["distance_to_beat_z"] = neutral_bps / denom
    if "side_distance_to_beat_bps" in out.columns:
        side_bps = pd.to_numeric(out["side_distance_to_beat_bps"], errors="coerce")
        out["side_distance_to_beat_z"] = side_bps / denom
    elif "side" in out.columns and "distance_to_beat_z" in out.columns:
        sign = np.where(out["side"].astype(str).str.upper().eq("UP"), 1.0, -1.0)
        out["side_distance_to_beat_z"] = pd.to_numeric(out["distance_to_beat_z"], errors="coerce") * sign
    if "rv_1h_bps" in out.columns:
        out["rv_1h_bps"] = rv
    if "coinbase_rv_1h_bps" in out.columns:
        out["coinbase_rv_1h_bps"] = rv
    return out.drop(columns=["_distance_z_minute_utc", "distance_z_rv_bps"], errors="ignore")


def apply_training_downweights(training: pd.DataFrame, downweights: tuple[tuple[str, str, float], ...]) -> pd.DataFrame:
    if not downweights:
        return training
    out = training.copy()
    multiplier = pd.Series(1.0, index=out.index)
    dates = out["date_utc"].astype(str)
    for start, end, weight in downweights:
        mask = dates.between(str(start), str(end))
        multiplier.loc[mask] *= float(weight)
    out["train_weight_multiplier"] = multiplier.astype(float)
    return out


def fold_training_segments(fold: dict[str, Any], default_start: str) -> tuple[tuple[str, str], ...]:
    raw_segments = fold.get("training_segments")
    if raw_segments:
        return tuple((str(start), str(end)) for start, end in raw_segments)
    return ((str(fold.get("training_start", default_start)), str(fold["outer_cutoff"])),)


def fold_test_segments(fold: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    raw_segments = fold.get("test_segments")
    if raw_segments:
        return tuple((str(start), str(end)) for start, end in raw_segments)
    return ((str(fold["test_start"]), str(fold["test_end"])),)


def date_range_for_fold(fold: dict[str, Any], day_limit: int = 0) -> list[str]:
    days: list[str] = []
    for start, end in fold_test_segments(fold):
        for day in date_range(start, end, 0):
            if day not in days:
                days.append(day)
    if day_limit and day_limit > 0:
        return days[: int(day_limit)]
    return days


def validate_fold_no_train_test_overlap(fold: dict[str, Any], default_start: str) -> None:
    if bool(fold.get("allow_train_test_overlap", False)):
        return
    test_segments = fold_test_segments(fold)
    for train_start, train_end in fold_training_segments(fold, default_start):
        if train_start > train_end:
            raise SystemExit(f"Invalid training segment in {fold['id']}: {train_start}>{train_end}")
        for test_start, test_end in test_segments:
            if max(train_start, test_start) <= min(train_end, test_end):
                raise SystemExit(
                    f"Training/test date overlap in {fold['id']}: "
                    f"train {train_start}..{train_end} overlaps test {test_start}..{test_end}"
                )


def clean_training(
    observations: pd.DataFrame,
    *,
    tte_min: int,
    tte_max: int,
    min_price: float,
    max_price: float,
) -> pd.DataFrame:
    out = observations.copy()
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out["date_utc"] = out["date_utc"].astype(str)
    out["side"] = out["side"].astype(str).str.upper()
    out["day_type"] = out["day_type"].astype(str)
    out["move_dz"] = pd.to_numeric(out["side_distance_to_beat_z"], errors="coerce")
    out["selected_ask"] = pd.to_numeric(out["selected_ask"], errors="coerce")
    out["selected_bid"] = pd.to_numeric(out["selected_bid"], errors="coerce")
    out["price_bucket"] = price_bucket_labels(out["selected_ask"])
    out["side_win"] = pd.to_numeric(out["side_win"], errors="coerce")
    out["tte_seconds"] = pd.to_numeric(out["tte_seconds"], errors="coerce")
    out["rv_1h_bps"] = pd.to_numeric(out.get("rv_1h_bps"), errors="coerce")
    mask = (
        out["timestamp_utc"].notna()
        & out["side"].isin(["UP", "DOWN"])
        & out["day_type"].isin(["weekday", "weekend"])
        & out["move_dz"].notna()
        & out["selected_ask"].between(min_price, max_price)
        & out["selected_bid"].notna()
        & out["selected_ask"].gt(out["selected_bid"])
        & out["side_win"].isin([0.0, 1.0])
        & out["tte_seconds"].between(int(tte_min), int(tte_max))
    )
    return out.loc[mask].reset_index(drop=True)


MOMENTUM_DAILY_COLUMNS = [
    "timestamp_utc",
    "date_utc",
    "canonical_window_id",
    "cb_close_1s",
]
MOMENTUM_OPTIONAL_DAILY_COLUMNS = [
    "distance_to_beat_z",
]


def momentum_horizons_from_features(features: tuple[str, ...]) -> tuple[int, ...]:
    horizons: list[int] = []
    for feature in features:
        try:
            horizon = int(str(feature).rsplit("_", 1)[1].removesuffix("s"))
        except (IndexError, ValueError):
            continue
        if horizon not in horizons:
            horizons.append(horizon)
    return tuple(sorted(horizons))


def load_momentum_daily_features(daily_dir: Path, distance_root: Path, day: str, horizons: tuple[int, ...]) -> pd.DataFrame:
    if not horizons:
        return pd.DataFrame()
    path = daily_part_path(daily_dir, day)
    available = parquet_columns(path)
    optional = [column for column in MOMENTUM_OPTIONAL_DAILY_COLUMNS if column in available]
    raw = pd.read_parquet(path, columns=[*MOMENTUM_DAILY_COLUMNS, *optional])
    raw["timestamp_utc"] = pd.to_datetime(raw["timestamp_utc"], utc=True, errors="coerce")
    raw["date_utc"] = raw["date_utc"].astype(str)
    raw["canonical_window_id"] = raw["canonical_window_id"].astype(str)
    if "distance_to_beat_z" not in raw.columns:
        raw = attach_distance_z(raw, distance_root, day)
    raw = raw.sort_values(["canonical_window_id", "timestamp_utc"], kind="stable").reset_index(drop=True)
    close = pd.to_numeric(raw["cb_close_1s"], errors="coerce")
    dz = pd.to_numeric(raw["distance_to_beat_z"], errors="coerce")
    for horizon in horizons:
        prior_close = np.full(len(raw), np.nan, dtype=float)
        prior_dz = np.full(len(raw), np.nan, dtype=float)
        delta = np.timedelta64(int(horizon), "s")
        for _window, idx in raw.groupby("canonical_window_id", sort=False).groups.items():
            positions = np.asarray(idx, dtype=np.int64)
            times = raw.loc[positions, "timestamp_utc"].dt.tz_convert(None).to_numpy(dtype="datetime64[ns]")
            targets = times - delta
            prior_pos = np.searchsorted(times, targets, side="right") - 1
            valid = prior_pos >= 0
            if valid.any():
                prior_indices = positions[prior_pos[valid]]
                prior_close[positions[valid]] = close.iloc[prior_indices].to_numpy(float)
                prior_dz[positions[valid]] = dz.iloc[prior_indices].to_numpy(float)
        raw[f"cb_ret_bps_{horizon}s"] = 10_000.0 * np.log(close / prior_close)
        raw[f"distance_z_delta_{horizon}s"] = dz - prior_dz
    keep = ["timestamp_utc", "canonical_window_id"]
    for horizon in horizons:
        keep.extend([f"cb_ret_bps_{horizon}s", f"distance_z_delta_{horizon}s"])
    return raw[keep]


def attach_training_momentum_features(
    training: pd.DataFrame,
    *,
    daily_dir: Path,
    distance_root: Path,
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    if not horizons or training.empty:
        return training
    pieces: list[pd.DataFrame] = []
    for day, part in training.groupby(training["date_utc"].astype(str), sort=True):
        daily = load_momentum_daily_features(daily_dir, distance_root, str(day), horizons)
        merged = part.merge(
            daily,
            on=["timestamp_utc", "canonical_window_id"],
            how="left",
            validate="many_to_one",
        )
        pieces.append(merged)
    out = pd.concat(pieces, ignore_index=True, sort=False) if pieces else training.copy()
    side_sign = np.where(out["side"].astype(str).str.upper().eq("UP"), 1.0, -1.0)
    for horizon in horizons:
        out[f"side_ret_bps_{horizon}s"] = side_sign * pd.to_numeric(out[f"cb_ret_bps_{horizon}s"], errors="coerce")
        out[f"side_dz_delta_{horizon}s"] = side_sign * pd.to_numeric(out[f"distance_z_delta_{horizon}s"], errors="coerce")
    return out


def attach_replay_momentum_features(
    frame: pd.DataFrame,
    *,
    daily_dir: Path,
    distance_root: Path,
    day: str,
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    if not horizons or frame.empty:
        return frame
    daily = load_momentum_daily_features(daily_dir, distance_root, day, horizons)
    if daily.empty:
        return frame
    out = frame.copy()
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out["canonical_window_id"] = out["canonical_window_id"].astype(str)
    merge_timestamp_col = "timestamp_utc"
    if "pm_event_base_second_utc" in out.columns:
        merge_timestamp_col = "momentum_base_timestamp_utc"
        out[merge_timestamp_col] = pd.to_datetime(out["pm_event_base_second_utc"], utc=True, errors="coerce")
    daily = daily.rename(columns={"timestamp_utc": merge_timestamp_col})
    merged = out.merge(
        daily,
        on=[merge_timestamp_col, "canonical_window_id"],
        how="left",
        validate="many_to_one" if merge_timestamp_col != "timestamp_utc" else "one_to_one",
    )
    if merge_timestamp_col != "timestamp_utc":
        merged = merged.drop(columns=[merge_timestamp_col])
    return merged


def recency_weights(frame: pd.DataFrame, cutoff: str, half_life: int | None) -> np.ndarray:
    if half_life is None:
        weights = np.ones(len(frame), dtype=float)
        if "train_weight_multiplier" in frame:
            weights *= pd.to_numeric(frame["train_weight_multiplier"], errors="coerce").fillna(1.0).to_numpy(float)
        return weights
    cutoff_ts = pd.Timestamp(cutoff, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
    age_days = (cutoff_ts - pd.to_datetime(frame["timestamp_utc"], utc=True)).dt.total_seconds().to_numpy(float) / 86400.0
    age_days = np.maximum(age_days, 0.0)
    weights = np.power(0.5, age_days / float(half_life))
    if "train_weight_multiplier" in frame:
        weights *= pd.to_numeric(frame["train_weight_multiplier"], errors="coerce").fillna(1.0).to_numpy(float)
    return weights


def rbf_features(z: np.ndarray, p: np.ndarray) -> np.ndarray:
    z_centers = np.array([-2.5, -1.5, -0.75, 0.0, 0.75, 1.5, 2.5], dtype=float)
    p_centers = np.array([0.25, 0.35, 0.45, 0.55, 0.65, 0.75], dtype=float)
    z_bw = 0.85
    p_bw = 0.085
    pieces = []
    for zc in z_centers:
        dz = ((z - zc) / z_bw) ** 2
        for pc in p_centers:
            dp = ((p - pc) / p_bw) ** 2
            pieces.append(np.exp(-0.5 * (dz + dp)))
    return np.column_stack(pieces)


def _side_indicator(frame: pd.DataFrame) -> np.ndarray:
    return frame["side"].astype(str).str.upper().eq("DOWN").to_numpy(float)


def _rv_log(frame: pd.DataFrame) -> np.ndarray:
    rv = pd.to_numeric(frame["rv_1h_bps"], errors="coerce").to_numpy(float)
    return np.log1p(np.maximum(rv, 0.0))


def _tte_fraction(frame: pd.DataFrame) -> np.ndarray:
    tte = pd.to_numeric(frame["tte_seconds"], errors="coerce").to_numpy(float)
    return np.clip(tte / 300.0, 0.0, 1.0)


def pm_price_feature_matrix(frame: pd.DataFrame, model_kind: str, side_mode: str) -> np.ndarray:
    z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
    p = pd.to_numeric(frame["selected_ask"], errors="coerce").to_numpy(float)
    lp = logit(p)
    base = [
        z,
        lp,
        z * lp,
        z * z,
        lp * lp,
    ]
    if side_mode == "with_side":
        base.append(_side_indicator(frame))
    x = np.column_stack(base)
    if model_kind == "surface_rbf":
        x = np.column_stack([x, rbf_features(z, p)])
    elif model_kind != "surface_linear":
        raise ValueError(f"Unknown model kind: {model_kind}")
    return x


def btc_linear_feature_matrix(
    frame: pd.DataFrame,
    model_kind: str,
    side_mode: str,
    momentum_feature: str = "",
) -> np.ndarray:
    z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
    pieces = [z]
    if model_kind == "btc_linear_state":
        rv = _rv_log(frame)
        tte = _tte_fraction(frame)
        pieces.extend([z * z, rv, tte, z * rv, z * tte])
    elif model_kind in MOMENTUM_PRICE_BUCKET_MODEL_KINDS:
        if momentum_feature not in MOMENTUM_FEATURES:
            raise ValueError(f"Unknown momentum feature for {model_kind}: {momentum_feature}")
        momentum = pd.to_numeric(frame[momentum_feature], errors="coerce").to_numpy(float)
        pieces.append(momentum)
    elif model_kind not in {"btc_linear_z", "btc_linear_z_price_bucket"}:
        raise ValueError(f"Unknown BTC linear model kind: {model_kind}")
    if side_mode == "with_side":
        pieces.append(_side_indicator(frame))
    return np.column_stack(pieces)


def feature_matrix(frame: pd.DataFrame, model_kind: str, side_mode: str, momentum_feature: str = "") -> np.ndarray:
    if model_kind in {"surface_linear", "surface_rbf"}:
        return pm_price_feature_matrix(frame, model_kind, side_mode)
    if model_kind in {"btc_linear_z", "btc_linear_state", "btc_linear_z_price_bucket", *MOMENTUM_PRICE_BUCKET_MODEL_KINDS}:
        return btc_linear_feature_matrix(frame, model_kind, side_mode, momentum_feature)
    raise ValueError(f"Unknown model kind: {model_kind}")


def feature_valid_mask(
    frame: pd.DataFrame,
    model_kind: str,
    price_bucket_scheme: str = "asymmetric",
    momentum_feature: str = "",
) -> pd.Series:
    move_dz = pd.to_numeric(frame["move_dz"], errors="coerce")
    selected_ask = pd.to_numeric(frame["selected_ask"], errors="coerce")
    mask = (
        np.isfinite(move_dz)
        & frame["side"].astype(str).str.upper().isin(["UP", "DOWN"])
    )
    if model_kind in PM_PRICE_MODEL_KINDS:
        mask &= np.isfinite(selected_ask) & selected_ask.gt(EPS) & selected_ask.lt(1.0 - EPS)
    if model_kind in PRICE_BUCKET_MODEL_KINDS:
        mask &= price_bucket_labels(selected_ask, price_bucket_scheme).notna()
    if model_kind in {"btc_linear_state", "btc_spline_state"}:
        rv = pd.to_numeric(frame["rv_1h_bps"], errors="coerce")
        tte = pd.to_numeric(frame["tte_seconds"], errors="coerce")
        mask &= np.isfinite(rv) & np.isfinite(tte)
    if model_kind in MOMENTUM_PRICE_BUCKET_MODEL_KINDS:
        if momentum_feature not in MOMENTUM_FEATURES:
            mask &= False
        elif momentum_feature not in frame.columns:
            mask &= False
        else:
            momentum = pd.to_numeric(frame[momentum_feature], errors="coerce")
            mask &= np.isfinite(momentum)
    return pd.Series(mask, index=frame.index).fillna(False)


class SurfaceModel:
    def __init__(
        self,
        *,
        model_kind: str,
        side_mode: str,
        c_value: float,
        spline_z_basis: int = 7,
        spline_p_basis: int = 6,
        spline_penalty: float = 10.0,
        price_bucket_scheme: str = "asymmetric",
        momentum_feature: str = "",
    ) -> None:
        self.model_kind = model_kind
        self.side_mode = side_mode
        self.c_value = float(c_value)
        self.spline_z_basis = int(spline_z_basis)
        self.spline_p_basis = int(spline_p_basis)
        self.spline_penalty = float(spline_penalty)
        self.price_bucket_scheme = normalize_price_bucket_scheme(price_bucket_scheme)
        self.momentum_feature = str(momentum_feature)
        self.scaler = StandardScaler()
        self.model = LogisticRegression(
            C=max(float(c_value), 1e-6),
            penalty="l2",
            solver="lbfgs",
            max_iter=500,
            n_jobs=1,
        )
        self.z_spline: SplineTransformer | None = None
        self.p_spline: SplineTransformer | None = None
        self.rows = 0

    @staticmethod
    def _spline_knots_for_basis(basis_count: int, degree: int = 3) -> int:
        return max(2, int(basis_count) - int(degree) + 1)

    def _spline_matrix(self, frame: pd.DataFrame, *, fit: bool) -> np.ndarray:
        z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
        p = pd.to_numeric(frame["selected_ask"], errors="coerce").to_numpy(float)
        lp = logit(p)
        if fit or self.z_spline is None or self.p_spline is None:
            self.z_spline = SplineTransformer(
                n_knots=self._spline_knots_for_basis(self.spline_z_basis),
                degree=3,
                include_bias=False,
                extrapolation="constant",
            )
            self.p_spline = SplineTransformer(
                n_knots=self._spline_knots_for_basis(self.spline_p_basis),
                degree=3,
                include_bias=False,
                extrapolation="constant",
            )
            z_basis = self.z_spline.fit_transform(z.reshape(-1, 1))
            p_basis = self.p_spline.fit_transform(p.reshape(-1, 1))
        else:
            z_basis = self.z_spline.transform(z.reshape(-1, 1))
            p_basis = self.p_spline.transform(p.reshape(-1, 1))
        interaction = (z_basis[:, :, None] * p_basis[:, None, :]).reshape(len(frame), -1)
        pieces = [z, lp, z * lp, z_basis, p_basis, interaction]
        if self.side_mode == "with_side":
            pieces.append(_side_indicator(frame))
        return np.column_stack(pieces)

    def _btc_spline_matrix(self, frame: pd.DataFrame, *, fit: bool) -> np.ndarray:
        z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
        if fit or self.z_spline is None:
            self.z_spline = SplineTransformer(
                n_knots=self._spline_knots_for_basis(self.spline_z_basis),
                degree=3,
                include_bias=False,
                extrapolation="constant",
            )
            z_basis = self.z_spline.fit_transform(z.reshape(-1, 1))
        else:
            z_basis = self.z_spline.transform(z.reshape(-1, 1))
        pieces: list[Any] = [z, z_basis]
        if self.model_kind == "btc_spline_state":
            rv = _rv_log(frame)
            tte = _tte_fraction(frame)
            pieces.extend([rv, tte, z * rv, z * tte, z_basis * rv[:, None], z_basis * tte[:, None]])
        elif self.model_kind not in {"btc_spline_z", "btc_spline_z_price_bucket"}:
            raise ValueError(f"Unknown BTC spline model kind: {self.model_kind}")
        if self.side_mode == "with_side":
            pieces.append(_side_indicator(frame))
        return np.column_stack(pieces)

    def _feature_matrix(self, frame: pd.DataFrame, *, fit: bool = False) -> np.ndarray:
        if self.model_kind == "surface_spline":
            return self._spline_matrix(frame, fit=fit)
        if self.model_kind in {"btc_spline_z", "btc_spline_state", "btc_spline_z_price_bucket"}:
            return self._btc_spline_matrix(frame, fit=fit)
        return feature_matrix(frame, self.model_kind, self.side_mode, self.momentum_feature)

    def fit(self, frame: pd.DataFrame, weights: np.ndarray) -> "SurfaceModel":
        x = self._feature_matrix(frame, fit=True)
        y = pd.to_numeric(frame["side_win"], errors="coerce").to_numpy(int)
        self.rows = int(len(frame))
        x_scaled = self.scaler.fit_transform(x)
        self.model.fit(x_scaled, y, sample_weight=weights)
        return self

    def fit_precomputed(
        self,
        *,
        x_scaled: np.ndarray,
        y: np.ndarray,
        weights: np.ndarray,
        scaler: StandardScaler,
        z_spline: SplineTransformer | None,
        p_spline: SplineTransformer | None,
        rows: int,
    ) -> "SurfaceModel":
        self.scaler = scaler
        self.z_spline = z_spline
        self.p_spline = p_spline
        self.rows = int(rows)
        self.model.fit(x_scaled, y.astype(int), sample_weight=weights)
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        x = self._feature_matrix(frame, fit=False)
        return self.model.predict_proba(self.scaler.transform(x))[:, 1]


def model_keys(day_type: str, side: str, side_mode: str) -> list[tuple[str, str]]:
    if side_mode == "separate":
        return [(day_type, side)]
    return [(day_type, "POOLED")]


def training_groups(
    training: pd.DataFrame,
    *,
    model_kind: str,
    day_type_mode: str,
    side_mode: str,
    weekday_hl: int | None,
    weekend_hl: int | None,
    price_bucket_scheme: str = "asymmetric",
    copy_parts: bool = False,
) -> list[tuple[str, str, int | None, pd.DataFrame]]:
    if day_type_mode == "included":
        day_groups = [
            ("weekday", training.loc[training["day_type"].eq("weekday")], weekday_hl),
            ("weekend", training.loc[training["day_type"].eq("weekend")], weekend_hl),
        ]
    elif day_type_mode == "excluded":
        day_groups = [("ALL", training, weekday_hl)]
    else:
        raise ValueError(f"Unknown day type mode: {day_type_mode}")
    groups: list[tuple[str, str, int | None, pd.DataFrame]] = []
    def add_groups(day_type: str, side_key: str, half_life: int | None, part: pd.DataFrame) -> None:
        if model_kind in PRICE_BUCKET_MODEL_KINDS:
            labels = price_bucket_labels(part["selected_ask"], price_bucket_scheme)
            for _low, _high, bucket_label in price_bucket_edges(price_bucket_scheme):
                bucket_part = part.loc[labels.eq(bucket_label)]
                groups.append((day_type, f"{side_key}|{bucket_label}", half_life, bucket_part.copy() if copy_parts else bucket_part))
        else:
            groups.append((day_type, side_key, half_life, part.copy() if copy_parts else part))

    for day_type, day_part, half_life in day_groups:
        if day_part.empty:
            continue
        if side_mode == "separate":
            for side in ("UP", "DOWN"):
                part = day_part.loc[day_part["side"].eq(side)]
                add_groups(day_type, side, half_life, part)
        else:
            add_groups(day_type, "POOLED", half_life, day_part)
    return groups


def model_group_has_support(
    part: pd.DataFrame,
    model_kind: str,
    *,
    price_bucket_min_rows: int,
    price_bucket_min_class: int,
) -> bool:
    if model_kind in PRICE_BUCKET_MODEL_KINDS:
        if len(part) < int(price_bucket_min_rows):
            return False
        counts = pd.to_numeric(part["side_win"], errors="coerce").value_counts()
        return int(counts.get(0.0, 0)) >= int(price_bucket_min_class) and int(counts.get(1.0, 0)) >= int(price_bucket_min_class)
    return len(part) >= 100 and part["side_win"].nunique() >= 2


def training_feature_cache_key(
    *,
    model_kind: str,
    side_mode: str,
    day_type_mode: str,
    spline_z_basis: int,
    spline_p_basis: int,
    price_bucket_min_rows: int,
    price_bucket_min_class: int,
    price_bucket_scheme: str,
    momentum_feature: str = "",
) -> tuple[Any, ...]:
    if model_kind == "surface_spline":
        basis_key = (int(spline_z_basis), int(spline_p_basis))
    elif model_kind in {"btc_spline_z", "btc_spline_state", "btc_spline_z_price_bucket"}:
        basis_key = (int(spline_z_basis),)
    else:
        basis_key = ()
    support_key = (
        int(price_bucket_min_rows),
        int(price_bucket_min_class),
        normalize_price_bucket_scheme(price_bucket_scheme),
    ) if model_kind in PRICE_BUCKET_MODEL_KINDS else ()
    momentum_key = str(momentum_feature) if model_kind in MOMENTUM_PRICE_BUCKET_MODEL_KINDS else ""
    return (model_kind, side_mode, day_type_mode, basis_key, support_key, momentum_key)


def build_training_feature_bundle(
    training: pd.DataFrame,
    *,
    model_kind: str,
    side_mode: str,
    day_type_mode: str,
    weekday_hl: int | None,
    weekend_hl: int | None,
    spline_z_basis: int,
    spline_p_basis: int,
    price_bucket_min_rows: int,
    price_bucket_min_class: int,
    price_bucket_scheme: str,
    momentum_feature: str = "",
) -> dict[tuple[str, str], dict[str, Any]]:
    bundle: dict[tuple[str, str], dict[str, Any]] = {}
    for day_type, side_key, _half_life, part in training_groups(
        training,
        model_kind=model_kind,
        day_type_mode=day_type_mode,
        side_mode=side_mode,
        weekday_hl=weekday_hl,
        weekend_hl=weekend_hl,
        price_bucket_scheme=price_bucket_scheme,
    ):
        part = part.loc[feature_valid_mask(part, model_kind, price_bucket_scheme, momentum_feature)]
        if not model_group_has_support(
            part,
            model_kind,
            price_bucket_min_rows=price_bucket_min_rows,
            price_bucket_min_class=price_bucket_min_class,
        ):
            continue
        temp = SurfaceModel(
            model_kind=model_kind,
            side_mode=side_mode,
            c_value=1.0,
            spline_z_basis=spline_z_basis,
            spline_p_basis=spline_p_basis,
            price_bucket_scheme=price_bucket_scheme,
            momentum_feature=momentum_feature,
        )
        x = temp._feature_matrix(part, fit=True)
        scaler = StandardScaler()
        x_scaled = scaler.fit_transform(x)
        y = pd.to_numeric(part["side_win"], errors="coerce").to_numpy(int)
        timestamp_columns = ["timestamp_utc"]
        if "train_weight_multiplier" in part.columns:
            timestamp_columns.append("train_weight_multiplier")
        bundle[(day_type, side_key)] = {
            "x_scaled": x_scaled,
            "y": y,
            "timestamps": part[timestamp_columns].copy(),
            "scaler": scaler,
            "z_spline": temp.z_spline,
            "p_spline": temp.p_spline,
            "rows": int(len(part)),
        }
    return bundle


def get_training_feature_bundle(
    cache: OrderedDict[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
    *,
    cache_size: int,
    training: pd.DataFrame,
    model_kind: str,
    side_mode: str,
    day_type_mode: str,
    weekday_hl: int | None,
    weekend_hl: int | None,
    spline_z_basis: int,
    spline_p_basis: int,
    price_bucket_min_rows: int,
    price_bucket_min_class: int,
    price_bucket_scheme: str,
    momentum_feature: str = "",
) -> dict[tuple[str, str], dict[str, Any]]:
    key = training_feature_cache_key(
        model_kind=model_kind,
        side_mode=side_mode,
        day_type_mode=day_type_mode,
        spline_z_basis=spline_z_basis,
        spline_p_basis=spline_p_basis,
        price_bucket_min_rows=price_bucket_min_rows,
        price_bucket_min_class=price_bucket_min_class,
        price_bucket_scheme=price_bucket_scheme,
        momentum_feature=momentum_feature,
    )
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    bundle = build_training_feature_bundle(
        training,
        model_kind=model_kind,
        side_mode=side_mode,
        day_type_mode=day_type_mode,
        weekday_hl=weekday_hl,
        weekend_hl=weekend_hl,
        spline_z_basis=spline_z_basis,
        spline_p_basis=spline_p_basis,
        price_bucket_min_rows=price_bucket_min_rows,
        price_bucket_min_class=price_bucket_min_class,
        price_bucket_scheme=price_bucket_scheme,
        momentum_feature=momentum_feature,
    )
    cache[key] = bundle
    cache.move_to_end(key)
    while len(cache) > max(1, int(cache_size)):
        cache.popitem(last=False)
    return bundle


def train_surface_models(
    training: pd.DataFrame,
    *,
    cutoff: str,
    model_kind: str,
    side_mode: str,
    day_type_mode: str,
    weekday_hl: int | None,
    weekend_hl: int | None,
    c_value: float,
    spline_z_basis: int = 7,
    spline_p_basis: int = 6,
    spline_penalty: float = 10.0,
    price_bucket_min_rows: int = 5000,
    price_bucket_min_class: int = 100,
    price_bucket_scheme: str = "asymmetric",
    momentum_feature: str = "",
) -> dict[tuple[str, str], SurfaceModel]:
    models: dict[tuple[str, str], SurfaceModel] = {}
    for day_type, side_key, half_life, part in training_groups(
        training,
        model_kind=model_kind,
        day_type_mode=day_type_mode,
        side_mode=side_mode,
        weekday_hl=weekday_hl,
        weekend_hl=weekend_hl,
        price_bucket_scheme=price_bucket_scheme,
    ):
        part = part.loc[feature_valid_mask(part, model_kind, price_bucket_scheme, momentum_feature)]
        if not model_group_has_support(
            part,
            model_kind,
            price_bucket_min_rows=price_bucket_min_rows,
            price_bucket_min_class=price_bucket_min_class,
        ):
            continue
        weights = recency_weights(part, cutoff, half_life)
        model = SurfaceModel(
            model_kind=model_kind,
            side_mode=side_mode,
            c_value=c_value,
            spline_z_basis=spline_z_basis,
            spline_p_basis=spline_p_basis,
            spline_penalty=spline_penalty,
            price_bucket_scheme=price_bucket_scheme,
            momentum_feature=momentum_feature,
        ).fit(part, weights)
        models[(day_type, side_key)] = model
    return models


def train_surface_models_reusing_features(
    training: pd.DataFrame,
    *,
    cutoff: str,
    model_kind: str,
    side_mode: str,
    day_type_mode: str,
    weekday_hl: int | None,
    weekend_hl: int | None,
    c_value: float,
    spline_z_basis: int = 7,
    spline_p_basis: int = 6,
    spline_penalty: float = 10.0,
    feature_cache: OrderedDict[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
    feature_cache_size: int,
    price_bucket_min_rows: int = 5000,
    price_bucket_min_class: int = 100,
    price_bucket_scheme: str = "asymmetric",
    momentum_feature: str = "",
) -> dict[tuple[str, str], SurfaceModel]:
    bundle = get_training_feature_bundle(
        feature_cache,
        cache_size=feature_cache_size,
        training=training,
        model_kind=model_kind,
        side_mode=side_mode,
        day_type_mode=day_type_mode,
        weekday_hl=weekday_hl,
        weekend_hl=weekend_hl,
        spline_z_basis=spline_z_basis,
        spline_p_basis=spline_p_basis,
        price_bucket_min_rows=price_bucket_min_rows,
        price_bucket_min_class=price_bucket_min_class,
        price_bucket_scheme=price_bucket_scheme,
        momentum_feature=momentum_feature,
    )
    models: dict[tuple[str, str], SurfaceModel] = {}
    for day_type, side_key, half_life, _part in training_groups(
        training,
        model_kind=model_kind,
        day_type_mode=day_type_mode,
        side_mode=side_mode,
        weekday_hl=weekday_hl,
        weekend_hl=weekend_hl,
        price_bucket_scheme=price_bucket_scheme,
    ):
        cached = bundle.get((day_type, side_key))
        if cached is None:
            continue
        weights = recency_weights(cached["timestamps"], cutoff, half_life)
        model = SurfaceModel(
            model_kind=model_kind,
            side_mode=side_mode,
            c_value=c_value,
            spline_z_basis=spline_z_basis,
            spline_p_basis=spline_p_basis,
            spline_penalty=spline_penalty,
            price_bucket_scheme=price_bucket_scheme,
            momentum_feature=momentum_feature,
        ).fit_precomputed(
            x_scaled=cached["x_scaled"],
            y=cached["y"],
            weights=weights,
            scaler=cached["scaler"],
            z_spline=cached["z_spline"],
            p_spline=cached["p_spline"],
            rows=cached["rows"],
        )
        models[(day_type, side_key)] = model
    return models


def test_side_context(frame: pd.DataFrame, distance_root: Path, day: str, side: str) -> pd.DataFrame:
    with_dz = frame.copy() if "distance_to_beat_z" in frame.columns else attach_distance_z(frame, distance_root, day)
    outcome_col = "polymarket_UP_outcome" if "polymarket_UP_outcome" in with_dz.columns else "cb_UP_outcome"
    sign = 1.0 if side == "UP" else -1.0
    prefix = side.lower()
    out = pd.DataFrame(index=with_dz.index)
    out["timestamp_utc"] = with_dz["timestamp_utc"]
    out["entry_date"] = with_dz["date_utc"].astype("string")
    out["canonical_window_id"] = with_dz["canonical_window_id"].astype(str)
    out["day_type"] = with_dz["derived_day_type"].astype(str)
    out["macro_session"] = with_dz["macro_session"].astype(str)
    out["tte_seconds"] = pd.to_numeric(with_dz["canonical_tte_seconds"], errors="coerce")
    out["rv_1h_bps"] = pd.to_numeric(with_dz["coinbase_rv_1h_bps"], errors="coerce")
    out["up_win"] = pd.to_numeric(with_dz[outcome_col], errors="coerce")
    out["side"] = side
    out["move_dz"] = pd.to_numeric(with_dz["distance_to_beat_z"], errors="coerce") * sign
    out["selected_ask"] = pd.to_numeric(with_dz[f"polymarket_{prefix}_ask"], errors="coerce")
    out["selected_bid"] = pd.to_numeric(with_dz[f"polymarket_{prefix}_bid"], errors="coerce")
    out["price_bucket"] = price_bucket_labels(out["selected_ask"])
    for horizon in (2, 5, 10):
        ret_col = f"cb_ret_bps_{horizon}s"
        dz_col = f"distance_z_delta_{horizon}s"
        if ret_col in with_dz.columns:
            out[f"side_ret_bps_{horizon}s"] = sign * pd.to_numeric(with_dz[ret_col], errors="coerce")
        if dz_col in with_dz.columns:
            out[f"side_dz_delta_{horizon}s"] = sign * pd.to_numeric(with_dz[dz_col], errors="coerce")
    out["side_win"] = out["up_win"] if side == "UP" else 1.0 - out["up_win"]
    out["trade_style"] = np.select(
        [out["move_dz"].gt(0.0), out["move_dz"].lt(0.0)],
        ["continuation", "mean_reversion"],
        default="neutral",
    )
    return out


def coalesce_decision_frame(frame: pd.DataFrame, coalesce_ms: int) -> pd.DataFrame:
    if frame.empty or int(coalesce_ms) <= 0:
        return frame
    out = frame.copy()
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp_utc", "canonical_window_id"])
    if out.empty:
        return out
    bucket_ns = int(coalesce_ms) * 1_000_000
    if bucket_ns <= 0:
        return out
    ts_ns = out["timestamp_utc"].astype("int64")
    out["_decision_coalesce_bucket"] = ts_ns // bucket_ns
    out = (
        out.sort_values(["canonical_window_id", "_decision_coalesce_bucket", "timestamp_utc"], kind="stable")
        .drop_duplicates(["canonical_window_id", "_decision_coalesce_bucket"], keep="last")
        .drop(columns=["_decision_coalesce_bucket"])
        .sort_values(["timestamp_utc", "canonical_window_id"], kind="stable")
        .reset_index(drop=True)
    )
    out["decision_coalesce_ms"] = int(coalesce_ms)
    return out


def predict_context(
    context: pd.DataFrame,
    models: dict[tuple[str, str], SurfaceModel],
    side_mode: str,
    day_type_mode: str,
) -> pd.DataFrame:
    out = context.copy()
    out["policy_value"] = np.nan
    out["policy_support"] = np.nan
    out["policy_level"] = ""
    for (day_type, side_key), model in models.items():
        mask = out["day_type"].eq(day_type) if day_type_mode == "included" else pd.Series(True, index=out.index)
        raw_side_key = str(side_key)
        base_side_key, bucket_label = (
            raw_side_key.split("|", 1) if "|" in raw_side_key else (raw_side_key, "")
        )
        if side_mode == "separate":
            mask &= out["side"].eq(base_side_key)
        if bucket_label:
            mask &= price_bucket_labels(out["selected_ask"], model.price_bucket_scheme).eq(bucket_label)
        mask &= feature_valid_mask(out, model.model_kind, model.price_bucket_scheme, model.momentum_feature)
        if not bool(mask.any()):
            continue
        out.loc[mask, "policy_value"] = model.predict(out.loc[mask])
        out.loc[mask, "policy_support"] = model.rows
        out.loc[mask, "policy_level"] = f"{day_type}|{side_key}"
    return out


def policy_score_stats(part: pd.DataFrame) -> dict[str, float]:
    if part.empty or "value" not in part or "settlement_value" not in part:
        return {"log_loss": np.nan, "brier": np.nan, "auc": np.nan, "ece10": np.nan, "calibration_gap": np.nan}
    y = pd.to_numeric(part["settlement_value"], errors="coerce").to_numpy(float)
    pred = pd.to_numeric(part["value"], errors="coerce").clip(EPS, 1.0 - EPS).to_numpy(float)
    valid = np.isfinite(y) & np.isfinite(pred)
    y = y[valid]
    pred = pred[valid]
    if len(y) == 0:
        return {"log_loss": np.nan, "brier": np.nan, "auc": np.nan, "ece10": np.nan, "calibration_gap": np.nan}
    log_loss = float(-(y * np.log(pred) + (1.0 - y) * np.log(1.0 - pred)).mean())
    brier = float(np.mean((pred - y) ** 2))
    auc = float(roc_auc_score(y, pred)) if len(np.unique(y)) == 2 else np.nan
    bins = np.linspace(0.0, 1.0, 11)
    bucket = np.clip(np.digitize(pred, bins, right=True) - 1, 0, 9)
    ece = 0.0
    for index in range(10):
        mask = bucket == index
        if not mask.any():
            continue
        ece += float(mask.mean()) * abs(float(pred[mask].mean()) - float(y[mask].mean()))
    return {
        "log_loss": log_loss,
        "brier": brier,
        "auc": auc,
        "ece10": float(ece),
        "calibration_gap": float(pred.mean() - y.mean()),
    }


def pnl_path_stats(part: pd.DataFrame, fee: float) -> dict[str, float]:
    if part.empty:
        return {"positive_day_rate": np.nan, "max_drawdown": 0.0}
    ordered = part.copy()
    ordered["net"] = pd.to_numeric(ordered["gross_pnl"], errors="coerce") - float(fee)
    ordered["entry_date"] = ordered["entry_date"].astype(str)
    daily = ordered.groupby("entry_date", sort=True)["net"].sum()
    cumulative = ordered.sort_values(["fill_time_utc", "timestamp_utc"], kind="stable")["net"].cumsum()
    drawdown = cumulative.cummax() - cumulative
    return {
        "positive_day_rate": float(daily.gt(0.0).mean()) if len(daily) else np.nan,
        "max_drawdown": float(drawdown.max()) if len(drawdown) else 0.0,
    }


def continuous_candidates(
    frame: pd.DataFrame,
    distance_root: Path,
    day: str,
    models: dict[tuple[str, str], SurfaceModel],
    side_mode: str,
    day_type_mode: str,
    spec: Spec,
    arm_id: str,
    *,
    min_price: float,
    max_price: float,
    execution_entry_mode: str = "guarded_fast_1c",
) -> pd.DataFrame:
    if execution_entry_mode not in EXECUTION_ENTRY_MODES:
        raise ValueError(f"Unsupported execution entry mode: {execution_entry_mode}")
    contexts = {
        side: predict_context(test_side_context(frame, distance_root, day, side), models, side_mode, day_type_mode)
        for side in ("UP", "DOWN")
    }
    side_data: dict[str, dict[str, np.ndarray]] = {}
    for side, context in contexts.items():
        value = pd.to_numeric(context["policy_value"], errors="coerce")
        ask = pd.to_numeric(context["selected_ask"], errors="coerce")
        bid = pd.to_numeric(context["selected_bid"], errors="coerce")
        threshold = pd.Series(
            np.where(
                context["trade_style"].eq("continuation"),
                float(spec.continuation_edge),
                np.where(context["trade_style"].eq("mean_reversion"), float(spec.mean_reversion_edge), np.nan),
            ),
            index=context.index,
        )
        if execution_entry_mode == "passive_maker":
            raw_limit = ask - 0.01
            limit = (raw_limit * 100.0).round() / 100.0
            execution_price_ok = bid.notna() & limit.ge(bid) & limit.lt(ask)
            edge_check_price = limit
            limit_edge_required = True
        elif execution_entry_mode == "touch_limit":
            limit = (ask * 100.0).round() / 100.0
            execution_price_ok = limit.ge(ask)
            edge_check_price = limit
            limit_edge_required = True
        elif execution_entry_mode in {"guarded_fast_1c", "guarded_fast_2c"}:
            ticks = 1 if execution_entry_mode.endswith("1c") else 2
            limit = (np.minimum(ask + ticks * 0.01, max_price) * 100.0).round() / 100.0
            execution_price_ok = limit.ge(ask)
            edge_check_price = limit
            limit_edge_required = True
        elif execution_entry_mode in {"unguarded_fast_1c", "unguarded_fast_2c"}:
            ticks = 1 if execution_entry_mode.endswith("1c") else 2
            limit = (np.minimum(ask + ticks * 0.01, max_price) * 100.0).round() / 100.0
            execution_price_ok = limit.ge(ask)
            edge_check_price = ask
            limit_edge_required = False
        elif (adaptive_config := adaptive_guarded_mode_config(execution_entry_mode)) is not None:
            ticks, surplus_buffer = adaptive_config
            max_limit = ask + ticks * 0.01
            fair_limit = value - threshold - float(surplus_buffer)
            raw_limit = np.minimum(np.minimum(max_limit, fair_limit), max_price)
            limit = np.floor((raw_limit * 100.0) + 1e-9) / 100.0
            limit = pd.Series(limit, index=context.index)
            execution_price_ok = limit.ge(ask)
            edge_check_price = limit + float(surplus_buffer)
            limit_edge_required = True
        elif (statsbot_config := statsbot_execution_mode_config(execution_entry_mode)) is not None:
            limit = statsbot_limit_price(
                ask,
                value,
                threshold,
                max_price,
                surplus_adaptive=bool(statsbot_config["surplus_adaptive"]),
            )
            execution_price_ok = limit.ge(ask)
            edge_check_price = limit
            limit_edge_required = True
        else:
            raise ValueError(f"Unsupported execution entry mode: {execution_entry_mode}")
        edge = value - ask
        edge_at_limit = value - limit
        tte = pd.to_numeric(context["tte_seconds"], errors="coerce")
        style_ok = pd.Series(
            np.where(
                context["trade_style"].eq("continuation"),
                tte.between(int(spec.continuation_tte_min), int(spec.continuation_tte_max)),
                np.where(
                    context["trade_style"].eq("mean_reversion"),
                    tte.between(int(spec.mean_reversion_tte_min), int(spec.mean_reversion_tte_max)),
                    False,
                ),
            ),
            index=context.index,
        )
        ok = (
            ask.between(min_price, max_price)
            & bid.notna()
            & ask.gt(bid)
            & value.notna()
            & threshold.notna()
            & execution_price_ok
            & (value - edge_check_price).ge(threshold - 1e-12)
            & style_ok
        )
        side_data[side] = {
            "ok": ok.to_numpy(bool),
            "value": value.to_numpy(float),
            "support": pd.to_numeric(context["policy_support"], errors="coerce").to_numpy(float),
            "policy_level": context["policy_level"].astype("string").to_numpy(),
            "edge": edge.to_numpy(float),
            "edge_at_limit": edge_at_limit.to_numpy(float),
            "ask": ask.to_numpy(float),
            "bid": bid.to_numpy(float),
            "limit": limit.to_numpy(float),
            "threshold": threshold.to_numpy(float),
            "style": context["trade_style"].astype("string").to_numpy(),
            "move_z": pd.to_numeric(context["move_dz"], errors="coerce").to_numpy(float),
            "limit_edge_required": np.full(len(context), bool(limit_edge_required), dtype=bool),
        }
    up, down = side_data["UP"], side_data["DOWN"]
    choose_up = up["ok"] & (
        ~down["ok"]
        | (up["edge"] > down["edge"])
        | (np.isclose(up["edge"], down["edge"], equal_nan=False) & (up["value"] >= down["value"]))
    )
    choose_down = down["ok"] & ~choose_up
    mask = choose_up | choose_down
    if not mask.any():
        return pd.DataFrame()
    indices = np.flatnonzero(mask)
    sides = np.where(choose_up[indices], "UP", "DOWN")
    selected = pd.DataFrame(
        {
            "timestamp_utc": frame.iloc[indices]["timestamp_utc"].to_numpy(),
            "entry_date": frame.iloc[indices]["date_utc"].astype("string").to_numpy(),
            "canonical_window_id": frame.iloc[indices]["canonical_window_id"].astype(str).to_numpy(),
            "day_type": frame.iloc[indices]["derived_day_type"].to_numpy(),
            "macro_session": frame.iloc[indices]["macro_session"].to_numpy(),
            "tte_seconds": frame.iloc[indices]["canonical_tte_seconds"].to_numpy(),
            "local_vol_1h_bps": frame.iloc[indices]["coinbase_rv_1h_bps"].to_numpy()
            if "coinbase_rv_1h_bps" in frame.columns
            else np.nan,
            "up_win": frame.iloc[indices][
                "polymarket_UP_outcome" if "polymarket_UP_outcome" in frame.columns else "cb_UP_outcome"
            ].to_numpy(),
            "side": sides,
            "strategy": "continuous_move_dz_surface",
            "spec_id": spec.spec_id,
            "continuation_threshold": spec.continuation_edge,
            "mean_reversion_threshold": spec.mean_reversion_edge,
            "arm_id": arm_id,
        }
    )
    for field in (
        "value", "support", "policy_level", "edge", "edge_at_limit", "ask", "bid", "limit", "threshold",
        "style", "move_z", "limit_edge_required",
    ):
        selected[field] = np.where(choose_up[indices], up[field][indices], down[field][indices])
    selected["execution_entry_mode"] = execution_entry_mode
    return selected.sort_values(["canonical_window_id", "timestamp_utc"], kind="stable").reset_index(drop=True)


def _latency_ms_for_label(label: str, profile: LatencyProfile, key: str) -> float:
    if label == "empirical":
        return float(profile.sample_ms(key))
    if label.endswith("ms"):
        return float(label[:-2])
    raise ValueError(f"Unsupported latency label: {label}")


def _quote_events_by_window_side(frame: pd.DataFrame) -> dict[tuple[str, str], pd.DataFrame]:
    events: dict[tuple[str, str], pd.DataFrame] = {}
    if frame.empty:
        return events
    source = frame.copy()
    source["timestamp_utc"] = pd.to_datetime(source["timestamp_utc"], utc=True)
    for side in ("UP", "DOWN"):
        ask_col = f"polymarket_{side.lower()}_ask"
        bid_col = f"polymarket_{side.lower()}_bid"
        cols = ["canonical_window_id", "timestamp_utc", ask_col]
        if bid_col in source.columns:
            cols.append(bid_col)
        part = source[cols].copy()
        part["ask"] = pd.to_numeric(part[ask_col], errors="coerce")
        part = part.loc[part["ask"].notna()].sort_values(["canonical_window_id", "timestamp_utc"], kind="stable")
        for window, group in part.groupby(part["canonical_window_id"].astype(str), sort=False):
            slim = group[["timestamp_utc", "ask"]].drop_duplicates("timestamp_utc", keep="last").reset_index(drop=True)
            if not slim.empty:
                events[(str(window), side)] = slim
    return events


def _asof_quote_index(events: pd.DataFrame, timestamp: pd.Timestamp) -> int | None:
    times = events["timestamp_utc"].to_numpy(dtype="datetime64[ns]")
    ts_pd = pd.Timestamp(timestamp)
    if ts_pd.tzinfo is None:
        ts_pd = ts_pd.tz_localize("UTC")
    else:
        ts_pd = ts_pd.tz_convert("UTC")
    ts = np.datetime64(ts_pd.tz_localize(None), "ns")
    index = int(np.searchsorted(times, ts, side="right") - 1)
    return index if index >= 0 else None


def replay_subsecond_pm_event_execution(
    candidates: pd.DataFrame,
    quote_events: dict[tuple[str, str], pd.DataFrame],
    arm: Arm,
    profile: LatencyProfile,
    *,
    latency_label: str,
    latch_label: str,
    latch_seconds: float,
) -> tuple[pd.DataFrame, dict[str, int]]:
    if candidates.empty:
        return candidates.copy(), {
            "attempts": 0,
            "fills": 0,
            "miss_price_moved_before_arrival": 0,
            "miss_policy_not_viable_after_arrival": 0,
            "miss_entry_resting_max_age": 0,
            "miss_entry_stale_quote_distance": 0,
            "miss_no_quote_after_arrival": 0,
            "miss_other": 0,
            "latch_kept_events": 0,
            "latch_converted_fills": 0,
        }
    fills: list[dict[str, Any]] = []
    diagnostics = {
        "attempts": 0,
        "fills": 0,
        "miss_price_moved_before_arrival": 0,
        "miss_policy_not_viable_after_arrival": 0,
        "miss_entry_resting_max_age": 0,
        "miss_entry_stale_quote_distance": 0,
        "miss_no_quote_after_arrival": 0,
        "miss_other": 0,
        "latch_kept_events": 0,
        "latch_converted_fills": 0,
    }
    for window, group in candidates.groupby("canonical_window_id", sort=False):
        group = group.sort_values("timestamp_utc", kind="stable")
        viable = {(pd.Timestamp(row.timestamp_utc), str(row.side)) for row in group.itertuples()}
        cooldown_until: pd.Timestamp | None = None
        attempts = 0
        for row in group.itertuples():
            signal_time = pd.Timestamp(row.timestamp_utc)
            if cooldown_until is not None and signal_time < cooldown_until:
                continue
            if attempts >= profile.max_attempts_per_window:
                break
            attempts += 1
            diagnostics["attempts"] += 1
            latency_key = f"{window}|{signal_time.isoformat()}|{attempts}|{latency_label}"
            latency_ms = _latency_ms_for_label(latency_label, profile, latency_key)
            arrival = signal_time + pd.Timedelta(milliseconds=latency_ms)
            events = quote_events.get((str(window), str(row.side)))
            if events is None or events.empty:
                diagnostics["miss_no_quote_after_arrival"] += 1
                break
            start_index = _asof_quote_index(events, arrival)
            if start_index is None:
                diagnostics["miss_no_quote_after_arrival"] += 1
                break
            filled_price = None
            fill_time = None
            cancel_started = None
            latch_kept_for_attempt = False
            entry_mode_config = statsbot_execution_mode_config(str(getattr(row, "execution_entry_mode", ""))) or {}
            max_age_seconds = float(entry_mode_config.get("resting_max_age_seconds", math.inf))
            stale_quote_max_ticks = int(entry_mode_config.get("stale_quote_max_ticks", 0) or 0)
            stale_quote_grace_seconds = float(entry_mode_config.get("stale_quote_grace_seconds", 0.0) or 0.0)
            hard_cancel_time = (
                signal_time + pd.Timedelta(seconds=max_age_seconds)
                if math.isfinite(max_age_seconds)
                else None
            )
            stale_quote_started: pd.Timestamp | None = None
            first_arrival_ask = float(events.iloc[start_index]["ask"])
            if first_arrival_ask <= float(row.limit) + 1e-12:
                filled_price = first_arrival_ask
                fill_time = arrival
            else:
                if first_arrival_ask > float(row.ask) + 1e-12:
                    diagnostics["miss_price_moved_before_arrival"] += 1
                for event_row in events.iloc[start_index + 1 :].itertuples(index=False):
                    event_time = pd.Timestamp(event_row.timestamp_utc)
                    ask = float(event_row.ask)
                    if hard_cancel_time is not None and event_time >= hard_cancel_time:
                        diagnostics["miss_entry_resting_max_age"] += 1
                        cooldown_until = hard_cancel_time + pd.Timedelta(seconds=profile.retry_cooldown_seconds)
                        break
                    if ask <= float(row.limit) + 1e-12:
                        filled_price = ask
                        fill_time = event_time
                        break
                    if stale_quote_max_ticks > 0:
                        max_distance = stale_quote_max_ticks * 0.01
                        if ask - float(row.limit) > max_distance + 1e-12:
                            if stale_quote_started is None:
                                stale_quote_started = event_time
                            away_seconds = float((event_time - stale_quote_started).total_seconds())
                            if away_seconds >= stale_quote_grace_seconds:
                                diagnostics["miss_entry_stale_quote_distance"] += 1
                                cancel_started = event_time
                                cancel_complete = event_time + pd.Timedelta(seconds=profile.cancel_confirmation_seconds)
                                race = events.loc[
                                    events["timestamp_utc"].gt(event_time)
                                    & events["timestamp_utc"].le(cancel_complete)
                                ]
                                for race_row in race.itertuples(index=False):
                                    race_ask = float(race_row.ask)
                                    race_time = pd.Timestamp(race_row.timestamp_utc)
                                    if race_ask <= float(row.limit) + 1e-12:
                                        filled_price = race_ask
                                        fill_time = race_time
                                        break
                                if filled_price is None:
                                    cooldown_until = cancel_complete + pd.Timedelta(seconds=profile.retry_cooldown_seconds)
                                break
                        else:
                            stale_quote_started = None
                    if (event_time, str(row.side)) not in viable:
                        edge_at_limit = float(getattr(row, "edge_at_limit", np.nan))
                        latch_active = (
                            latch_seconds > 0.0
                            and float((event_time - signal_time).total_seconds()) <= float(latch_seconds)
                            and np.isfinite(edge_at_limit)
                            and edge_at_limit + 1e-12 >= LATCH_MIN_EDGE
                        )
                        if latch_active:
                            latch_kept_for_attempt = True
                            diagnostics["latch_kept_events"] += 1
                            continue
                        cancel_started = event_time
                        cancel_complete = event_time + pd.Timedelta(seconds=profile.cancel_confirmation_seconds)
                        race = events.loc[
                            events["timestamp_utc"].gt(event_time)
                            & events["timestamp_utc"].le(cancel_complete)
                        ]
                        for race_row in race.itertuples(index=False):
                            race_ask = float(race_row.ask)
                            race_time = pd.Timestamp(race_row.timestamp_utc)
                            if race_ask <= float(row.limit) + 1e-12:
                                filled_price = race_ask
                                fill_time = race_time
                                break
                        if filled_price is None:
                            diagnostics["miss_policy_not_viable_after_arrival"] += 1
                            cooldown_until = cancel_complete + pd.Timedelta(seconds=profile.retry_cooldown_seconds)
                        break
            if filled_price is not None:
                item = row._asdict()
                settlement = float(row.up_win) if row.side == "UP" else 1.0 - float(row.up_win)
                item.update(
                    {
                        "arm_id": arm.arm_id,
                        "execution_model": "PM_EVENT_SUBSECOND",
                        "execution_latency_label": latency_label,
                        "execution_latch_label": latch_label,
                        "execution_latch_seconds": float(latch_seconds),
                        "signal_time_utc": signal_time,
                        "fill_time_utc": fill_time,
                        "sampled_submit_latency_ms": latency_ms,
                        "arrival_delay_seconds": float(latency_ms) / 1000.0,
                        "fill_delay_seconds": float((fill_time - signal_time).total_seconds()),
                        "fill_price": float(filled_price),
                        "attempts_before_fill": attempts,
                        "cancel_race_fill": cancel_started is not None,
                        "latch_kept_before_fill": latch_kept_for_attempt,
                        "settlement_value": settlement,
                        "gross_pnl": (settlement - float(filled_price)) * 5.0,
                    }
                )
                fills.append(item)
                diagnostics["fills"] += 1
                if latch_kept_for_attempt:
                    diagnostics["latch_converted_fills"] += 1
                break
            if cooldown_until is None:
                diagnostics["miss_other"] += 1
                break
    return pd.DataFrame(fills), diagnostics


_WORKER: dict[str, Any] = {}


def init_worker(*args: Any) -> None:
    keys = (
        "daily_dir",
        "feature_daily_dir",
        "minute_multi",
        "outcomes",
        "profile",
        "distance_root",
        "models_by_arm",
        "side_modes_by_arm",
        "day_type_modes_by_arm",
        "specs",
        "replay_tte_min",
        "replay_tte_max",
        "min_price",
        "max_price",
        "fee",
        "momentum_horizons",
        "execution_replay",
        "execution_latency_specs",
        "execution_entry_modes",
        "execution_latches",
        "decision_coalesce_ms",
        "candidate_allowlist",
        "distance_z_source",
        "distance_z_vol_lookup",
    )
    _WORKER.update(dict(zip(keys, args)))


def score_day(day: str, target: Path) -> tuple[str, int]:
    daily_dir: Path = _WORKER["daily_dir"]
    feature_daily_dir: Path = _WORKER.get("feature_daily_dir", daily_dir)
    minute_multi: pd.DataFrame = _WORKER["minute_multi"]
    outcomes: pd.Series = _WORKER["outcomes"]
    profile: Any = _WORKER["profile"]
    distance_root: Path = _WORKER["distance_root"]
    models_by_arm: dict[str, dict[tuple[str, str], SurfaceModel]] = _WORKER["models_by_arm"]
    side_modes_by_arm: dict[str, str] = _WORKER["side_modes_by_arm"]
    day_type_modes_by_arm: dict[str, str] = _WORKER["day_type_modes_by_arm"]
    specs: list[Spec] = _WORKER["specs"]
    candidate_allowlist: set[tuple[str, str]] = set(_WORKER.get("candidate_allowlist") or ())
    minute = minute_for_mode(minute_multi, "LOCAL_60M")
    frame = load_frame(
        daily_dir,
        day,
        minute,
        outcomes,
        tte_min=int(_WORKER["replay_tte_min"]),
        tte_max=int(_WORKER["replay_tte_max"]),
    )
    frame = apply_distance_z_source(
        frame,
        distance_z_source=str(_WORKER.get("distance_z_source") or "flat_60m"),
        vol_lookup=_WORKER.get("distance_z_vol_lookup", pd.DataFrame()),
    )
    frame = attach_replay_momentum_features(
        frame,
        daily_dir=feature_daily_dir,
        distance_root=distance_root,
        day=day,
        horizons=tuple(_WORKER.get("momentum_horizons", ())),
    )
    decision_coalesce_ms = int(_WORKER.get("decision_coalesce_ms") or 0)
    decision_frame = coalesce_decision_frame(frame, decision_coalesce_ms)
    execution_replay = str(_WORKER.get("execution_replay") or "current")
    quotes = quote_lookup(frame) if execution_replay == "current" else {}
    quote_end = (
        frame.groupby(frame["canonical_window_id"].astype(str))["timestamp_utc"].max().to_dict()
        if execution_replay == "current"
        else {}
    )
    quote_events = _quote_events_by_window_side(frame) if execution_replay == "pm_event_subsecond" else {}
    fills: list[pd.DataFrame] = []
    eligibility: list[dict[str, Any]] = []
    for arm_id, models in models_by_arm.items():
        side_mode = side_modes_by_arm[arm_id]
        day_type_mode = day_type_modes_by_arm[arm_id]
        for spec in specs:
            if candidate_allowlist and (arm_id, spec.spec_id) not in candidate_allowlist:
                continue
            entry_modes = (
                tuple(_WORKER.get("execution_entry_modes") or ())
                if execution_replay == "pm_event_subsecond"
                else ("guarded_fast_1c",)
            )
            for entry_mode in entry_modes:
                candidate_id = f"{arm_id}__{spec.spec_id}__mode_{entry_mode}"
                candidates = continuous_candidates(
                    decision_frame,
                    distance_root,
                    day,
                    models,
                    side_mode,
                    day_type_mode,
                    spec,
                    candidate_id,
                    min_price=float(_WORKER["min_price"]),
                    max_price=float(_WORKER["max_price"]),
                    execution_entry_mode=entry_mode,
                )
                if not candidates.empty:
                    candidates["surface_arm"] = arm_id
                    candidates["decision_coalesce_ms"] = decision_coalesce_ms
                eligible = int(candidates["canonical_window_id"].nunique()) if not candidates.empty else 0
                if execution_replay == "pm_event_subsecond":
                    for latency_label in tuple(_WORKER.get("execution_latency_specs") or ()):
                        for latch_label, latch_seconds in tuple(_WORKER.get("execution_latches") or (("no_latch", 0.0),)):
                            latency_candidate_id = f"{candidate_id}__exec_{latency_label}__latch_{latch_label}"
                            latency_candidates = candidates.copy()
                            latency_candidates["arm_id"] = latency_candidate_id
                            latency_candidates["execution_latency_label"] = latency_label
                            latency_candidates["execution_entry_mode"] = entry_mode
                            latency_candidates["execution_latch_label"] = latch_label
                            arm = Arm(
                                latency_candidate_id,
                                "continuous_move_dz_surface",
                                "COINBASE_ONLY",
                                spec.continuation_edge,
                                spec.mean_reversion_edge,
                            )
                            part, diagnostics = replay_subsecond_pm_event_execution(
                                latency_candidates,
                                quote_events,
                                arm,
                                profile,
                                latency_label=latency_label,
                                latch_label=latch_label,
                                latch_seconds=float(latch_seconds),
                            )
                            if not part.empty:
                                fills.append(part)
                            eligibility.append(
                                {
                                    "entry_date": day,
                                    "arm_id": latency_candidate_id,
                                    "surface_arm": arm_id,
                                    "spec_id": spec.spec_id,
                                    "execution_replay": execution_replay,
                                    "execution_entry_mode": entry_mode,
                                    "execution_latency_label": latency_label,
                                    "execution_latch_label": latch_label,
                                    "execution_latch_seconds": float(latch_seconds),
                                    "decision_coalesce_ms": decision_coalesce_ms,
                                    "eligible_windows": eligible,
                                    "filled_windows": int(part["canonical_window_id"].nunique()) if not part.empty else 0,
                                    **{f"execution_{key}": value for key, value in diagnostics.items()},
                                }
                            )
                    continue
                arm = Arm(candidate_id, "continuous_move_dz_surface", "COINBASE_ONLY", spec.continuation_edge, spec.mean_reversion_edge)
                part = replay_current_execution(candidates, quotes, quote_end, arm, profile)
                if not part.empty:
                    fills.append(part)
                eligibility.append(
                    {
                        "entry_date": day,
                        "arm_id": candidate_id,
                        "surface_arm": arm_id,
                        "spec_id": spec.spec_id,
                        "execution_replay": execution_replay,
                        "execution_entry_mode": entry_mode,
                        "execution_latency_label": "legacy_ceil_seconds",
                        "execution_latch_label": "legacy_no_latch",
                        "execution_latch_seconds": 0.0,
                        "decision_coalesce_ms": decision_coalesce_ms,
                        "eligible_windows": eligible,
                        "filled_windows": int(part["canonical_window_id"].nunique()) if not part.empty else 0,
                    }
                )
    target.mkdir(parents=True, exist_ok=True)
    out_fills = pd.concat(fills, ignore_index=True, sort=False) if fills else pd.DataFrame()
    out_fills.to_parquet(target / "fills.parquet", index=False)
    pd.DataFrame(eligibility).to_csv(target / "eligibility.csv", index=False)
    return day, len(out_fills)


def replay_period(days: list[str], output: Path, workers: int, worker_args: tuple[Any, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    fills: list[pd.DataFrame] = []
    eligibility: list[pd.DataFrame] = []
    pending: list[tuple[str, Path]] = []
    completed = 0
    started = time.perf_counter()
    for day in days:
        target = output / f"date_utc={day}"
        if (target / "fills.parquet").exists() and (target / "eligibility.csv").exists():
            fills.append(pd.read_parquet(target / "fills.parquet"))
            eligibility.append(pd.read_csv(target / "eligibility.csv"))
            completed += 1
            print(f"[{stamp()}] REUSE day={day} progress={completed}/{len(days)}", flush=True)
        else:
            pending.append((day, target))
    if pending:
        with ProcessPoolExecutor(max_workers=max(1, workers), initializer=init_worker, initargs=worker_args) as executor:
            futures = {executor.submit(score_day, day, target): day for day, target in pending}
            for future in as_completed(futures):
                day, rows = future.result()
                target = output / f"date_utc={day}"
                fills.append(pd.read_parquet(target / "fills.parquet"))
                eligibility.append(pd.read_csv(target / "eligibility.csv"))
                completed += 1
                elapsed = time.perf_counter() - started
                eta = elapsed / completed * (len(days) - completed)
                print(
                    f"[{stamp()}] WRITE day={day} rows={rows:,} progress={completed}/{len(days)} "
                    f"workers={workers} elapsed={int(elapsed)}s eta={int(eta)}s",
                    flush=True,
                )
    return (
        pd.concat(fills, ignore_index=True, sort=False) if fills else pd.DataFrame(),
        pd.concat(eligibility, ignore_index=True, sort=False) if eligibility else pd.DataFrame(),
    )


def summarize(fills: pd.DataFrame, eligibility: pd.DataFrame, fee: float, period_days: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for arm_id in eligibility["arm_id"].drop_duplicates().astype(str):
        elig = eligibility.loc[eligibility["arm_id"].astype(str).eq(arm_id)]
        part = fills.loc[fills.get("arm_id", pd.Series(dtype=object)).astype(str).eq(arm_id)].copy() if not fills.empty else pd.DataFrame()
        eligible = int(elig["eligible_windows"].sum())
        base = {
            "arm_id": arm_id,
            "surface_arm": str(elig["surface_arm"].iloc[0]) if not elig.empty else "",
            "spec_id": str(elig["spec_id"].iloc[0]) if not elig.empty else "",
            "execution_replay": str(elig["execution_replay"].iloc[0]) if "execution_replay" in elig and not elig.empty else "",
            "execution_entry_mode": (
                str(elig["execution_entry_mode"].iloc[0])
                if "execution_entry_mode" in elig and not elig.empty
                else ""
            ),
            "execution_latency_label": (
                str(elig["execution_latency_label"].iloc[0])
                if "execution_latency_label" in elig and not elig.empty
                else ""
            ),
            "execution_latch_label": (
                str(elig["execution_latch_label"].iloc[0])
                if "execution_latch_label" in elig and not elig.empty
                else ""
            ),
            "execution_latch_seconds": (
                float(elig["execution_latch_seconds"].iloc[0])
                if "execution_latch_seconds" in elig and not elig.empty
                else 0.0
            ),
            "decision_coalesce_ms": (
                int(float(elig["decision_coalesce_ms"].iloc[0]))
                if "decision_coalesce_ms" in elig and not elig.empty
                else 0
            ),
            "days": period_days,
            "eligible_windows": eligible,
            "execution_attempts": int(pd.to_numeric(elig.get("execution_attempts", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
            "execution_miss_price_moved_before_arrival": int(
                pd.to_numeric(elig.get("execution_miss_price_moved_before_arrival", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "execution_miss_policy_not_viable_after_arrival": int(
                pd.to_numeric(elig.get("execution_miss_policy_not_viable_after_arrival", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "execution_miss_entry_resting_max_age": int(
                pd.to_numeric(elig.get("execution_miss_entry_resting_max_age", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "execution_miss_entry_stale_quote_distance": int(
                pd.to_numeric(elig.get("execution_miss_entry_stale_quote_distance", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "execution_miss_no_quote_after_arrival": int(
                pd.to_numeric(elig.get("execution_miss_no_quote_after_arrival", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "execution_miss_other": int(
                pd.to_numeric(elig.get("execution_miss_other", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
            ),
            "execution_latch_kept_events": int(
                pd.to_numeric(elig.get("execution_latch_kept_events", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "execution_latch_converted_fills": int(
                pd.to_numeric(elig.get("execution_latch_converted_fills", pd.Series(dtype=float)), errors="coerce")
                .fillna(0)
                .sum()
            ),
        }
        if part.empty:
            rows.append(
                {
                    **base,
                    "trades": 0,
                    "trades_day": 0.0,
                    "fill_rate": 0.0 if eligible else np.nan,
                    "net_pnl": 0.0,
                    "pnl_day": 0.0,
                    "pnl_trade": np.nan,
                    "win_rate": np.nan,
                    "continuation_trades": 0,
                    "continuation_pnl": 0.0,
                    "mean_reversion_trades": 0,
                    "mean_reversion_pnl": 0.0,
                    "positive_day_rate": np.nan,
                    "max_drawdown": 0.0,
                    "log_loss": np.nan,
                    "brier": np.nan,
                    "auc": np.nan,
                    "ece10": np.nan,
                    "calibration_gap": np.nan,
                }
            )
            continue
        part["net"] = pd.to_numeric(part["gross_pnl"], errors="coerce") - float(fee)
        cont = part.loc[part["style"].eq("continuation"), "net"]
        mr = part.loc[part["style"].eq("mean_reversion"), "net"]
        pnl = float(part["net"].sum())
        rows.append(
            {
                **base,
                "trades": int(len(part)),
                "trades_day": int(len(part)) / max(1, period_days),
                "fill_rate": float(part["canonical_window_id"].nunique() / eligible) if eligible else np.nan,
                "net_pnl": pnl,
                "pnl_day": pnl / max(1, period_days),
                "pnl_trade": pnl / len(part),
                "win_rate": float(pd.to_numeric(part["gross_pnl"], errors="coerce").gt(0).mean()),
                "continuation_trades": int(len(cont)),
                "continuation_pnl": float(cont.sum()),
                "mean_reversion_trades": int(len(mr)),
                "mean_reversion_pnl": float(mr.sum()),
                **pnl_path_stats(part, fee),
                **policy_score_stats(part),
            }
        )
    return pd.DataFrame(rows).sort_values(["net_pnl"], ascending=False)


def summarize_execution_stress(fills: pd.DataFrame, fee: float, period_days: int) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    stress_specs = [
        {"stress_mode": "base_replay", "slip_cents": 0, "edge_filter": False, "max_fill_delay": None},
        {"stress_mode": "worse_fill_1c", "slip_cents": 1, "edge_filter": False, "max_fill_delay": None},
        {"stress_mode": "worse_fill_2c", "slip_cents": 2, "edge_filter": False, "max_fill_delay": None},
        {"stress_mode": "worse_fill_3c", "slip_cents": 3, "edge_filter": False, "max_fill_delay": None},
        {"stress_mode": "edge_survives_1c", "slip_cents": 1, "edge_filter": True, "max_fill_delay": None},
        {"stress_mode": "edge_survives_2c", "slip_cents": 2, "edge_filter": True, "max_fill_delay": None},
        {"stress_mode": "edge_survives_3c", "slip_cents": 3, "edge_filter": True, "max_fill_delay": None},
        {"stress_mode": "fill_delay_lte_1s", "slip_cents": 0, "edge_filter": False, "max_fill_delay": 1.0},
        {"stress_mode": "fill_delay_lte_2s", "slip_cents": 0, "edge_filter": False, "max_fill_delay": 2.0},
    ]
    rows: list[dict[str, Any]] = []
    base = fills.copy()
    base["fill_price"] = pd.to_numeric(base["fill_price"], errors="coerce")
    base["value"] = pd.to_numeric(base["value"], errors="coerce")
    base["threshold"] = pd.to_numeric(base["threshold"], errors="coerce")
    base["fill_delay_seconds"] = pd.to_numeric(base["fill_delay_seconds"], errors="coerce")
    extra_cols = [
        col
        for col in (
            "surface_arm",
            "execution_replay",
            "execution_entry_mode",
            "execution_latency_label",
            "execution_latch_label",
            "execution_latch_seconds",
        )
        if col in base.columns
    ]
    all_arms = base[["arm_id", "spec_id", *extra_cols]].drop_duplicates().copy()
    all_arms["arm_id"] = all_arms["arm_id"].astype(str)
    all_arms["spec_id"] = all_arms["spec_id"].astype(str)
    if "surface_arm" in all_arms.columns:
        all_arms["surface_arm"] = all_arms["surface_arm"].astype(str)
    else:
        all_arms["surface_arm"] = all_arms["arm_id"].str.rsplit("__", n=1).str[0]
    for mode in stress_specs:
        stressed = base.copy()
        stressed_price = (stressed["fill_price"] + float(mode["slip_cents"]) / 100.0).clip(upper=0.99)
        mask = stressed_price.notna() & stressed["value"].notna() & stressed["threshold"].notna()
        if bool(mode["edge_filter"]):
            mask &= (stressed["value"] - stressed_price).ge(stressed["threshold"] - 1e-12)
        max_fill_delay = mode["max_fill_delay"]
        if max_fill_delay is not None:
            mask &= stressed["fill_delay_seconds"].le(float(max_fill_delay))
        stressed = stressed.loc[mask].copy()
        groups = {str(arm_id): part.copy() for arm_id, part in stressed.groupby(stressed["arm_id"].astype(str), sort=False)}
        if not stressed.empty:
            stressed["stressed_fill_price"] = (stressed["fill_price"] + float(mode["slip_cents"]) / 100.0).clip(upper=0.99)
        for arm in all_arms.itertuples(index=False):
            arm_id = str(arm.arm_id)
            part = groups.get(arm_id, pd.DataFrame())
            if part.empty:
                rows.append(
                    {
                        "stress_mode": mode["stress_mode"],
                        "arm_id": arm_id,
                        "surface_arm": str(arm.surface_arm),
                        "spec_id": str(arm.spec_id),
                        "execution_replay": str(getattr(arm, "execution_replay", "")),
                        "execution_entry_mode": str(getattr(arm, "execution_entry_mode", "")),
                        "execution_latency_label": str(getattr(arm, "execution_latency_label", "")),
                        "execution_latch_label": str(getattr(arm, "execution_latch_label", "")),
                        "execution_latch_seconds": float(getattr(arm, "execution_latch_seconds", 0.0) or 0.0),
                        "days": period_days,
                        "trades": 0,
                        "trades_day": 0.0,
                        "net_pnl": 0.0,
                        "pnl_day": 0.0,
                        "pnl_trade": np.nan,
                        "win_rate": np.nan,
                        "positive_day_rate": np.nan,
                        "max_drawdown": 0.0,
                    }
                )
                continue
            part = part.copy()
            part["stressed_fill_price"] = (part["fill_price"] + float(mode["slip_cents"]) / 100.0).clip(upper=0.99)
            part["gross_pnl"] = (
                pd.to_numeric(part["settlement_value"], errors="coerce") - part["stressed_fill_price"]
            ) * 5.0
            part["net"] = pd.to_numeric(part["gross_pnl"], errors="coerce") - float(fee)
            cont = part.loc[part["style"].eq("continuation"), "net"]
            mr = part.loc[part["style"].eq("mean_reversion"), "net"]
            pnl = float(part["net"].sum())
            rows.append(
                {
                    "stress_mode": mode["stress_mode"],
                    "arm_id": arm_id,
                    "surface_arm": str(arm.surface_arm),
                    "spec_id": str(arm.spec_id),
                    "execution_replay": str(getattr(arm, "execution_replay", "")),
                    "execution_entry_mode": str(getattr(arm, "execution_entry_mode", "")),
                    "execution_latency_label": str(getattr(arm, "execution_latency_label", "")),
                    "execution_latch_label": str(getattr(arm, "execution_latch_label", "")),
                    "execution_latch_seconds": float(getattr(arm, "execution_latch_seconds", 0.0) or 0.0),
                    "days": period_days,
                    "trades": int(len(part)),
                    "trades_day": int(len(part)) / max(1, period_days),
                    "net_pnl": pnl,
                    "pnl_day": pnl / max(1, period_days),
                    "pnl_trade": pnl / len(part),
                    "win_rate": float(pd.to_numeric(part["gross_pnl"], errors="coerce").gt(0).mean()),
                    "continuation_trades": int(len(cont)),
                    "continuation_pnl": float(cont.sum()),
                    "mean_reversion_trades": int(len(mr)),
                    "mean_reversion_pnl": float(mr.sum()),
                    **pnl_path_stats(part, fee),
                    **policy_score_stats(part),
                }
            )
    return pd.DataFrame(rows).sort_values(["stress_mode", "net_pnl"], ascending=[True, False])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-dir", default=str(DEFAULT_DAILY))
    parser.add_argument(
        "--train-daily-dir",
        default="",
        help="Optional base-cadence daily dir for training-only momentum attachment. Defaults to --daily-dir.",
    )
    parser.add_argument("--outcomes", default=str(DEFAULT_OUTCOMES))
    parser.add_argument("--minute-cache", default=str(DEFAULT_MINUTE))
    parser.add_argument("--latency-config", default=str(DEFAULT_LATENCY_CONFIG))
    parser.add_argument("--distance-root", default=str(DEFAULT_DZ_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--fold-set", choices=tuple(ALL_FOLD_SETS), default="manipulation_4week_20260713")
    parser.add_argument("--specs", default=DEFAULT_SPECS)
    parser.add_argument("--tte-min", type=int, default=45)
    parser.add_argument("--tte-max", type=int, default=265)
    parser.add_argument("--tte-ranges", default="")
    parser.add_argument("--train-tte-min", type=int, default=45)
    parser.add_argument("--train-tte-max", type=int, default=265)
    parser.add_argument("--training-start", default="2026-02-14")
    parser.add_argument("--model-kinds", default="surface_linear,surface_rbf")
    parser.add_argument("--momentum-features", type=parse_momentum_features, default=parse_momentum_features(",".join(MOMENTUM_FEATURES)))
    parser.add_argument("--day-type-modes", default="included")
    parser.add_argument("--side-modes", default="with_side")
    parser.add_argument("--c-values", type=parse_floats, default=parse_floats("0.03,0.1,0.3,1.0"))
    parser.add_argument("--spline-bases", type=parse_spline_bases, default=parse_spline_bases("7x6"))
    parser.add_argument("--spline-penalties", type=parse_floats, default=parse_floats("10"))
    parser.add_argument("--weekday-half-lives", type=parse_half_lives, default=parse_half_lives("120"))
    parser.add_argument("--weekend-half-lives", type=parse_half_lives, default=parse_half_lives("120"))
    parser.add_argument("--recency-pairs", type=parse_recency_pairs, default=())
    parser.add_argument("--excluded-half-lives", type=parse_half_lives, default=parse_half_lives("120"))
    parser.add_argument("--min-price", type=float, default=0.20)
    parser.add_argument("--max-price", type=float, default=0.80)
    parser.add_argument("--train-min-price", type=float, default=None)
    parser.add_argument("--train-max-price", type=float, default=None)
    parser.add_argument("--price-bucket-schemes", type=parse_price_bucket_schemes, default=parse_price_bucket_schemes("asymmetric"))
    parser.add_argument(
        "--distance-z-source",
        choices=("flat_60m", "ewma_hl5", "ewma_hl10"),
        default="flat_60m",
        help="Volatility denominator used for move_dz. EWMA sources are computed from distance_root/minute_regimes.parquet.",
    )
    parser.add_argument("--price-bucket-min-rows", type=int, default=5000)
    parser.add_argument("--price-bucket-min-class", type=int, default=100)
    parser.add_argument("--fee-per-trade", type=float, default=0.01)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-folds", type=int, default=0)
    parser.add_argument("--day-limit", type=int, default=0)
    parser.add_argument("--print-arms", action="store_true")
    parser.add_argument("--execution-stress", action="store_true")
    parser.add_argument(
        "--execution-replay",
        choices=("current", "pm_event_subsecond"),
        default="current",
        help="current uses the legacy whole-second empirical replay; pm_event_subsecond replays PM quote-event rows at exact millisecond latency.",
    )
    parser.add_argument(
        "--execution-latencies",
        type=parse_execution_latency_specs,
        default=parse_execution_latency_specs(DEFAULT_SUBSECOND_LATENCIES),
        help="Comma-separated latencies for --execution-replay pm_event_subsecond, e.g. 300,500,700,1000,EMPIRICAL.",
    )
    parser.add_argument(
        "--execution-entry-modes",
        type=parse_execution_entry_modes,
        default=parse_execution_entry_modes(DEFAULT_EXECUTION_ENTRY_MODES),
        help="Comma-separated execution modes for PM-event replay.",
    )
    parser.add_argument(
        "--execution-latches",
        type=parse_execution_latches,
        default=parse_execution_latches(DEFAULT_EXECUTION_LATCHES),
        help="Comma-separated latch durations for PM-event replay, e.g. no_latch,1s,2s,3s.",
    )
    parser.add_argument(
        "--decision-coalesce-ms",
        type=int,
        default=0,
        help=(
            "Optional decision-cadence sensitivity for PM-event replay. "
            "0 evaluates every event row; positive values keep the latest state per market/window per N ms bucket."
        ),
    )
    parser.add_argument(
        "--candidate-allowlist",
        type=parse_candidate_allowlist,
        default=(),
        help="Comma-separated '<surface_arm>|<spec_id>' pairs to train/replay exactly.",
    )
    parser.add_argument("--reuse-train-features", action="store_true")
    parser.add_argument("--train-feature-cache-size", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output_dir).expanduser().resolve()
    if args.overwrite and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    tte_ranges = parse_tte_ranges(args.tte_ranges, args.tte_min, args.tte_max) if args.tte_ranges else None
    specs = parse_specs(args.specs, args.tte_min, args.tte_max, tte_ranges)
    replay_tte_min = min(tte_min for _, tte_min, _ in (tte_ranges or [("", args.tte_min, args.tte_max)]))
    replay_tte_max = max(tte_max for _, _, tte_max in (tte_ranges or [("", args.tte_min, args.tte_max)]))
    folds = list(ALL_FOLD_SETS[args.fold_set])
    if args.max_folds:
        folds = folds[: int(args.max_folds)]
    for fold in folds:
        validate_fold_no_train_test_overlap(fold, args.training_start)

    model_kinds = parse_csv(args.model_kinds)
    valid_model_kinds = PM_PRICE_MODEL_KINDS | BTC_ONLY_MODEL_KINDS
    invalid_model_kinds = sorted(set(model_kinds) - valid_model_kinds)
    if invalid_model_kinds:
        raise SystemExit(f"Invalid model kinds: {invalid_model_kinds}")
    base_model_variants = iter_model_variants(
        model_kinds,
        args.c_values,
        args.spline_bases,
        args.spline_penalties,
        args.momentum_features,
    )
    model_variants = expand_price_bucket_variants(base_model_variants, args.price_bucket_schemes)
    uses_momentum = any(str(variant["model_kind"]) in MOMENTUM_PRICE_BUCKET_MODEL_KINDS for variant in model_variants)
    momentum_horizons = momentum_horizons_from_features(tuple(args.momentum_features)) if uses_momentum else ()
    day_type_modes = parse_csv(args.day_type_modes)
    invalid_day_type_modes = sorted(set(day_type_modes) - {"included", "excluded"})
    if invalid_day_type_modes:
        raise SystemExit(f"Invalid day type modes: {invalid_day_type_modes}")
    side_modes = parse_csv(args.side_modes)
    invalid_side_modes = sorted(set(side_modes) - {"pooled", "with_side", "separate"})
    if invalid_side_modes:
        raise SystemExit(f"Invalid side modes: {invalid_side_modes}")
    included_recency_pairs = (
        tuple(args.recency_pairs)
        if args.recency_pairs
        else tuple(
            (weekday_hl, weekend_hl)
            for weekday_hl in args.weekday_half_lives
            for weekend_hl in args.weekend_half_lives
        )
    )
    surface_configs: list[dict[str, Any]] = []
    if "included" in day_type_modes:
        surface_configs.extend(
            {
                "day_type_mode": "included",
                "weekday_hl": weekday_hl,
                "weekend_hl": weekend_hl,
                "recency_label": f"WD{half_life_label(weekday_hl)}/WE{half_life_label(weekend_hl)}",
            }
            for weekday_hl, weekend_hl in included_recency_pairs
        )
    if "excluded" in day_type_modes:
        surface_configs.extend(
            {
                "day_type_mode": "excluded",
                "weekday_hl": half_life,
                "weekend_hl": half_life,
                "recency_label": half_life_label(half_life),
            }
            for half_life in args.excluded_half_lives
        )
    if not surface_configs:
        raise SystemExit("No surface configs selected")

    planned_surface_arms = [
        (
            f"{model_variant_id(variant, side_mode)}__"
            f"{surface_config_id(config['day_type_mode'], config['weekday_hl'], config['weekend_hl'])}"
        )
        for variant in model_variants
        for side_mode in side_modes
        for config in surface_configs
    ]
    candidate_allowlist = set(args.candidate_allowlist)
    if candidate_allowlist:
        valid_surface_arms = set(planned_surface_arms)
        valid_spec_ids = {spec.spec_id for spec in specs}
        invalid_surface_arms = sorted({surface_arm for surface_arm, _spec_id in candidate_allowlist} - valid_surface_arms)
        invalid_spec_ids = sorted({spec_id for _surface_arm, spec_id in candidate_allowlist} - valid_spec_ids)
        if invalid_surface_arms or invalid_spec_ids:
            raise SystemExit(
                "Invalid candidate allowlist: "
                f"surface_arms={invalid_surface_arms[:5]} spec_ids={invalid_spec_ids[:5]}"
            )
        allowed_surface_arms = {surface_arm for surface_arm, _spec_id in candidate_allowlist}
        planned_surface_arms = [arm_id for arm_id in planned_surface_arms if arm_id in allowed_surface_arms]
    execution_latency_labels = tuple(args.execution_latencies) if args.execution_replay == "pm_event_subsecond" else ()
    execution_entry_modes = tuple(args.execution_entry_modes) if args.execution_replay == "pm_event_subsecond" else ("guarded_fast_1c",)
    execution_latches = tuple(args.execution_latches) if args.execution_replay == "pm_event_subsecond" else (("legacy_no_latch", 0.0),)
    if execution_latency_labels:
        planned_replay_arms = [
            f"{arm_id}__{spec.spec_id}__mode_{entry_mode}__exec_{latency_label}__latch_{latch_label}"
            for arm_id in planned_surface_arms
            for spec in specs
            for entry_mode in execution_entry_modes
            for latency_label in execution_latency_labels
            for latch_label, _latch_seconds in execution_latches
            if not candidate_allowlist or (arm_id, spec.spec_id) in candidate_allowlist
        ]
    else:
        planned_replay_arms = [
            f"{arm_id}__{spec.spec_id}__mode_{entry_mode}"
            for arm_id in planned_surface_arms
            for spec in specs
            for entry_mode in execution_entry_modes
            if not candidate_allowlist or (arm_id, spec.spec_id) in candidate_allowlist
        ]
    print(
        f"[{stamp()}] ARM PLAN surface_arms={len(planned_surface_arms)} "
        f"specs={len(specs)} execution_replay={args.execution_replay} "
        f"entry_modes={execution_entry_modes} "
        f"latencies={execution_latency_labels or ('legacy_ceil_seconds',)} "
        f"latches={tuple(label for label, _seconds in execution_latches)} "
        f"replay_arms={len(planned_replay_arms)} folds={len(folds)}",
        flush=True,
    )
    if args.print_arms:
        for arm_id in planned_replay_arms:
            print(f"[{stamp()}] ARM {arm_id}", flush=True)

    minute_multi = pd.read_parquet(args.minute_cache)
    minute_multi["minute_utc"] = pd.to_datetime(minute_multi["minute_utc"], utc=True)
    outcomes = load_outcomes(Path(args.outcomes))
    profile = latency_profile_from_config(json.loads(Path(args.latency_config).read_text(encoding="utf-8")))
    distance_root = Path(args.distance_root).expanduser().resolve()
    distance_z_vol_lookup = build_distance_z_vol_lookup(distance_root, args.distance_z_source)

    all_summaries: list[pd.DataFrame] = []
    all_fills: list[pd.DataFrame] = []
    manifest_arms: list[dict[str, Any]] = []
    for index, fold in enumerate(folds, start=1):
        training_segments = fold_training_segments(fold, args.training_start)
        downweight_segments = tuple(
            (str(start), str(end), float(weight))
            for start, end, weight in fold.get("downweight_segments", ())
        )
        print(
            f"[{stamp()}] FOLD START {index}/{len(folds)} {fold['id']} "
            f"group={fold.get('diagnostic_group', '')} training_segments={training_segments} "
            f"downweights={downweight_segments} "
            f"model_kinds={model_kinds} day_type_modes={day_type_modes} side_modes={side_modes} "
            f"c_values={args.c_values} spline_bases={args.spline_bases} spline_penalties={args.spline_penalties} "
            f"price_bucket_schemes={args.price_bucket_schemes} "
            f"distance_z_source={args.distance_z_source} "
            f"momentum_features={args.momentum_features if uses_momentum else ()} "
            f"surface_configs={tuple((config['day_type_mode'], config['recency_label']) for config in surface_configs)}",
            flush=True,
        )
        fold_training_start = training_segments[0][0]
        raw_training = (
            read_observations_for_segments(distance_root, training_segments)
            if "training_segments" in fold
            else read_observations(distance_root, fold["outer_cutoff"], fold_training_start)
        )
        raw_training = apply_distance_z_source(
            raw_training,
            distance_z_source=args.distance_z_source,
            vol_lookup=distance_z_vol_lookup,
        )
        training = clean_training(
            raw_training,
            tte_min=args.train_tte_min,
            tte_max=args.train_tte_max,
            min_price=args.min_price if args.train_min_price is None else float(args.train_min_price),
            max_price=args.max_price if args.train_max_price is None else float(args.train_max_price),
        )
        if momentum_horizons:
            training = attach_training_momentum_features(
                training,
                daily_dir=Path(args.train_daily_dir or args.daily_dir).expanduser().resolve(),
                distance_root=distance_root,
                horizons=momentum_horizons,
            )
        training = apply_training_downweights(training, downweight_segments)
        print(
            f"[{stamp()}] TRAIN rows={len(training):,} segments={training_segments} cutoff={fold['outer_cutoff']}",
            flush=True,
        )
        models_by_arm: dict[str, dict[tuple[str, str], SurfaceModel]] = {}
        side_modes_by_arm: dict[str, str] = {}
        day_type_modes_by_arm: dict[str, str] = {}
        feature_cache: OrderedDict[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]] = OrderedDict()
        for variant in model_variants:
            model_kind = str(variant["model_kind"])
            for side_mode in side_modes:
                for config in surface_configs:
                    day_type_mode = str(config["day_type_mode"])
                    weekday_hl = config["weekday_hl"]
                    weekend_hl = config["weekend_hl"]
                    price_bucket_scheme = str(variant.get("price_bucket_scheme") or "asymmetric")
                    momentum_feature = str(variant.get("momentum_feature") or "")
                    arm_id = (
                        f"{model_variant_id(variant, side_mode)}"
                        f"__{surface_config_id(day_type_mode, weekday_hl, weekend_hl)}"
                    )
                    if candidate_allowlist and arm_id not in allowed_surface_arms:
                        continue
                    started = time.perf_counter()
                    if args.reuse_train_features:
                        models = train_surface_models_reusing_features(
                            training,
                            cutoff=fold["outer_cutoff"],
                            model_kind=model_kind,
                            side_mode=side_mode,
                            day_type_mode=day_type_mode,
                            weekday_hl=weekday_hl,
                            weekend_hl=weekend_hl,
                            c_value=float(variant["c_value"]),
                            spline_z_basis=int(variant["spline_z_basis"]),
                            spline_p_basis=int(variant["spline_p_basis"]),
                            spline_penalty=float(variant["spline_penalty"]),
                            feature_cache=feature_cache,
                            feature_cache_size=int(args.train_feature_cache_size),
                            price_bucket_min_rows=int(args.price_bucket_min_rows),
                            price_bucket_min_class=int(args.price_bucket_min_class),
                            price_bucket_scheme=price_bucket_scheme,
                            momentum_feature=momentum_feature,
                        )
                    else:
                        models = train_surface_models(
                            training,
                            cutoff=fold["outer_cutoff"],
                            model_kind=model_kind,
                            side_mode=side_mode,
                            day_type_mode=day_type_mode,
                            weekday_hl=weekday_hl,
                            weekend_hl=weekend_hl,
                            c_value=float(variant["c_value"]),
                            spline_z_basis=int(variant["spline_z_basis"]),
                            spline_p_basis=int(variant["spline_p_basis"]),
                            spline_penalty=float(variant["spline_penalty"]),
                            price_bucket_min_rows=int(args.price_bucket_min_rows),
                            price_bucket_min_class=int(args.price_bucket_min_class),
                            price_bucket_scheme=price_bucket_scheme,
                            momentum_feature=momentum_feature,
                        )
                    if not models:
                        raise SystemExit(f"No models trained for {arm_id}")
                    models_by_arm[arm_id] = models
                    side_modes_by_arm[arm_id] = side_mode
                    day_type_modes_by_arm[arm_id] = day_type_mode
                    manifest_arms.append(
                        {
                            "fold": fold["id"],
                            "arm_id": arm_id,
                            "training_start": fold_training_start,
                            "training_end": fold["outer_cutoff"],
                            "training_segments": [list(segment) for segment in training_segments],
                            "downweight_segments": [list(segment) for segment in downweight_segments],
                            "diagnostic_group": str(fold.get("diagnostic_group", "")),
                            "training_recipe": str(fold.get("training_recipe", "")),
                            "test_start": fold["test_start"],
                            "test_end": fold["test_end"],
                            "test_segments": [list(segment) for segment in fold_test_segments(fold)],
                            "model_kind": model_kind,
                            "day_type_mode": day_type_mode,
                            "side_mode": side_mode,
                            "c_value": float(variant["c_value"]),
                            "spline_z_basis": int(variant["spline_z_basis"]),
                            "spline_p_basis": int(variant["spline_p_basis"]),
                            "spline_penalty": float(variant["spline_penalty"]),
                            "price_bucket_scheme": price_bucket_scheme if model_kind in PRICE_BUCKET_MODEL_KINDS else "",
                            "momentum_feature": momentum_feature,
                            "weekday_hl": half_life_label(weekday_hl),
                            "weekend_hl": half_life_label(weekend_hl),
                            "model_keys": [f"{key[0]}|{key[1]}" for key in sorted(models)],
                        }
                    )
                    print(
                        f"[{stamp()}] TRAINED {arm_id} models={len(models)} elapsed={time.perf_counter() - started:.1f}s",
                        flush=True,
                    )

        days = date_range_for_fold(fold, args.day_limit)
        fills, eligibility = replay_period(
            days,
            output / fold["id"] / "replay",
            args.workers,
            (
                Path(args.daily_dir).expanduser().resolve(),
                Path(args.train_daily_dir or args.daily_dir).expanduser().resolve(),
                minute_multi,
                outcomes,
                profile,
                distance_root,
                models_by_arm,
                side_modes_by_arm,
                day_type_modes_by_arm,
                specs,
                replay_tte_min,
                replay_tte_max,
                args.min_price,
                args.max_price,
                args.fee_per_trade,
                momentum_horizons,
                args.execution_replay,
                tuple(args.execution_latencies) if args.execution_replay == "pm_event_subsecond" else (),
                tuple(args.execution_entry_modes) if args.execution_replay == "pm_event_subsecond" else ("guarded_fast_1c",),
                tuple(args.execution_latches) if args.execution_replay == "pm_event_subsecond" else (("legacy_no_latch", 0.0),),
                int(args.decision_coalesce_ms) if args.execution_replay == "pm_event_subsecond" else 0,
                tuple(args.candidate_allowlist),
                args.distance_z_source,
                distance_z_vol_lookup,
            ),
        )
        summary = summarize(fills, eligibility, args.fee_per_trade, len(days))
        summary.insert(0, "fold", fold["id"])
        summary.insert(1, "test_start", fold["test_start"])
        summary.insert(2, "test_end", fold["test_end"])
        summary.insert(3, "diagnostic_group", str(fold.get("diagnostic_group", "")))
        summary.insert(4, "training_recipe", str(fold.get("training_recipe", "")))
        summary.insert(5, "training_segments", json.dumps([list(segment) for segment in training_segments]))
        summary.insert(6, "downweight_segments", json.dumps([list(segment) for segment in downweight_segments]))
        summary.insert(7, "blackout_start", str(fold.get("blackout_start", "")))
        summary.insert(8, "blackout_end", str(fold.get("blackout_end", "")))
        summary.insert(9, "blackout_length_days", fold.get("blackout_length_days", ""))
        summary.insert(10, "test_segments", json.dumps([list(segment) for segment in fold_test_segments(fold)]))
        summary.to_csv(output / fold["id"] / "fold_summary.csv", index=False)
        if args.execution_stress:
            stress = summarize_execution_stress(fills, args.fee_per_trade, len(days))
            stress.insert(0, "fold", fold["id"])
            stress.insert(1, "test_start", fold["test_start"])
            stress.insert(2, "test_end", fold["test_end"])
            stress.insert(3, "diagnostic_group", str(fold.get("diagnostic_group", "")))
            stress.insert(4, "training_recipe", str(fold.get("training_recipe", "")))
            stress.insert(5, "training_segments", json.dumps([list(segment) for segment in training_segments]))
            stress.insert(6, "downweight_segments", json.dumps([list(segment) for segment in downweight_segments]))
            stress.insert(7, "blackout_start", str(fold.get("blackout_start", "")))
            stress.insert(8, "blackout_end", str(fold.get("blackout_end", "")))
            stress.insert(9, "blackout_length_days", fold.get("blackout_length_days", ""))
            stress.insert(10, "test_segments", json.dumps([list(segment) for segment in fold_test_segments(fold)]))
            stress.to_csv(output / fold["id"] / "execution_stress_summary.csv", index=False)
        all_summaries.append(summary)
        if not fills.empty:
            fills["fold"] = fold["id"]
            all_fills.append(fills)
        pd.concat(all_summaries, ignore_index=True).to_csv(output / "oos_fold_summary.csv", index=False)

    fold_summary = pd.concat(all_summaries, ignore_index=True)
    aggregate_keys = ["surface_arm", "spec_id"]
    if "execution_replay" in fold_summary.columns:
        aggregate_keys.append("execution_replay")
    if "execution_entry_mode" in fold_summary.columns:
        aggregate_keys.append("execution_entry_mode")
    if "execution_latency_label" in fold_summary.columns:
        aggregate_keys.append("execution_latency_label")
    if "execution_latch_label" in fold_summary.columns:
        aggregate_keys.append("execution_latch_label")
    if "decision_coalesce_ms" in fold_summary.columns:
        aggregate_keys.append("decision_coalesce_ms")
    aggregate = fold_summary.groupby(aggregate_keys, as_index=False, dropna=False).agg(
        folds=("fold", "nunique"),
        positive_folds=("net_pnl", lambda x: int((x > 0).sum())),
        total_days=("days", "sum"),
        trades=("trades", "sum"),
        total_pnl=("net_pnl", "sum"),
        worst_fold_pnl=("net_pnl", "min"),
        execution_attempts=("execution_attempts", "sum"),
        execution_miss_price_moved_before_arrival=("execution_miss_price_moved_before_arrival", "sum"),
        execution_miss_policy_not_viable_after_arrival=("execution_miss_policy_not_viable_after_arrival", "sum"),
        execution_miss_no_quote_after_arrival=("execution_miss_no_quote_after_arrival", "sum"),
        execution_miss_other=("execution_miss_other", "sum"),
        execution_latch_kept_events=("execution_latch_kept_events", "sum"),
        execution_latch_converted_fills=("execution_latch_converted_fills", "sum"),
        continuation_trades=("continuation_trades", "sum"),
        continuation_pnl=("continuation_pnl", "sum"),
        mean_reversion_trades=("mean_reversion_trades", "sum"),
        mean_reversion_pnl=("mean_reversion_pnl", "sum"),
    )
    aggregate["pnl_day"] = aggregate["total_pnl"] / aggregate["total_days"]
    aggregate["trades_day"] = aggregate["trades"] / aggregate["total_days"]
    aggregate["pnl_trade"] = np.where(aggregate["trades"].gt(0), aggregate["total_pnl"] / aggregate["trades"], np.nan)
    aggregate.to_csv(output / "oos_aggregate.csv", index=False)
    if all_fills:
        fills_out = pd.concat(all_fills, ignore_index=True, sort=False)
        fills_out["net_pnl"] = pd.to_numeric(fills_out["gross_pnl"], errors="coerce") - float(args.fee_per_trade)
        fills_out.to_parquet(output / "oos_fills.parquet", index=False)
        if args.execution_stress:
            stress_frames = [
                pd.read_csv(output / fold["id"] / "execution_stress_summary.csv")
                for fold in folds
                if (output / fold["id"] / "execution_stress_summary.csv").exists()
            ]
            if stress_frames:
                stress_summary = pd.concat(stress_frames, ignore_index=True, sort=False)
                stress_summary.to_csv(output / "oos_execution_stress_summary.csv", index=False)
                stress_keys = ["stress_mode", "surface_arm", "spec_id"]
                if "execution_replay" in stress_summary.columns:
                    stress_keys.append("execution_replay")
                if "execution_entry_mode" in stress_summary.columns:
                    stress_keys.append("execution_entry_mode")
                if "execution_latency_label" in stress_summary.columns:
                    stress_keys.append("execution_latency_label")
                if "execution_latch_label" in stress_summary.columns:
                    stress_keys.append("execution_latch_label")
                if "decision_coalesce_ms" in stress_summary.columns:
                    stress_keys.append("decision_coalesce_ms")
                stress_aggregate = stress_summary.groupby(stress_keys, as_index=False, dropna=False).agg(
                    folds=("fold", "nunique"),
                    positive_folds=("net_pnl", lambda x: int((x > 0).sum())),
                    total_days=("days", "sum"),
                    trades=("trades", "sum"),
                    total_pnl=("net_pnl", "sum"),
                    worst_fold_pnl=("net_pnl", "min"),
                    mean_positive_day_rate=("positive_day_rate", "mean"),
                    max_drawdown=("max_drawdown", "max"),
                    continuation_trades=("continuation_trades", "sum"),
                    continuation_pnl=("continuation_pnl", "sum"),
                    mean_reversion_trades=("mean_reversion_trades", "sum"),
                    mean_reversion_pnl=("mean_reversion_pnl", "sum"),
                    mean_log_loss=("log_loss", "mean"),
                    mean_brier=("brier", "mean"),
                    mean_auc=("auc", "mean"),
                    mean_ece10=("ece10", "mean"),
                    mean_calibration_gap=("calibration_gap", "mean"),
                )
                stress_aggregate["pnl_day"] = stress_aggregate["total_pnl"] / stress_aggregate["total_days"]
                stress_aggregate["trades_day"] = stress_aggregate["trades"] / stress_aggregate["total_days"]
                stress_aggregate["pnl_trade"] = np.where(
                    stress_aggregate["trades"].gt(0),
                    stress_aggregate["total_pnl"] / stress_aggregate["trades"],
                    np.nan,
                )
                stress_aggregate.to_csv(output / "oos_execution_stress_aggregate.csv", index=False)
    manifest = {
        "fold_set": args.fold_set,
        "daily_dir": str(Path(args.daily_dir).expanduser().resolve()),
        "train_daily_dir": str(Path(args.train_daily_dir or args.daily_dir).expanduser().resolve()),
        "outcomes": str(Path(args.outcomes).expanduser().resolve()),
        "minute_cache": str(Path(args.minute_cache).expanduser().resolve()),
        "distance_root": str(Path(args.distance_root).expanduser().resolve()),
        "model_kinds": list(model_kinds),
        "momentum_features": list(args.momentum_features) if uses_momentum else [],
        "momentum_horizons": list(momentum_horizons),
        "price_bucket_schemes": list(args.price_bucket_schemes),
        "distance_z_source": str(args.distance_z_source),
        "day_type_modes": list(day_type_modes),
        "side_modes": list(side_modes),
        "c_values": list(args.c_values),
        "spline_bases": [{"move_dz": int(z_basis), "price": int(p_basis)} for z_basis, p_basis in args.spline_bases],
        "spline_penalties": list(args.spline_penalties),
        "weekday_half_lives": [half_life_label(value) for value in args.weekday_half_lives],
        "weekend_half_lives": [half_life_label(value) for value in args.weekend_half_lives],
        "recency_pairs": [
            {"weekday": half_life_label(weekday_hl), "weekend": half_life_label(weekend_hl)}
            for weekday_hl, weekend_hl in included_recency_pairs
        ],
        "excluded_half_lives": [half_life_label(value) for value in args.excluded_half_lives],
        "surface_configs": [
            {
                "day_type_mode": str(config["day_type_mode"]),
                "weekday_hl": half_life_label(config["weekday_hl"]),
                "weekend_hl": half_life_label(config["weekend_hl"]),
                "recency_label": str(config["recency_label"]),
            }
            for config in surface_configs
        ],
        "specs": [asdict(spec) for spec in specs],
        "tte_ranges": [
            {"label": label, "tte_min": tte_min, "tte_max": tte_max}
            for label, tte_min, tte_max in (tte_ranges or [("", args.tte_min, args.tte_max)])
        ],
        "training_start": args.training_start,
        "train_tte_min": args.train_tte_min,
        "train_tte_max": args.train_tte_max,
        "train_min_price": args.min_price if args.train_min_price is None else float(args.train_min_price),
        "train_max_price": args.max_price if args.train_max_price is None else float(args.train_max_price),
        "replay_min_price": float(args.min_price),
        "replay_max_price": float(args.max_price),
        "price_bucket_scheme_definitions": {
            scheme: [{"label": label, "low": low, "high": high} for low, high, label in price_bucket_edges(scheme)]
            for scheme in args.price_bucket_schemes
        },
        "price_bucket_min_rows": int(args.price_bucket_min_rows),
        "price_bucket_min_class": int(args.price_bucket_min_class),
        "folds": [
            {
                "id": fold["id"],
                "diagnostic_group": str(fold.get("diagnostic_group", "")),
                "training_recipe": str(fold.get("training_recipe", "")),
                "training_start": fold_training_segments(fold, args.training_start)[0][0],
                "training_end": fold["outer_cutoff"],
                "training_segments": [list(segment) for segment in fold_training_segments(fold, args.training_start)],
                "downweight_segments": [
                    [str(start), str(end), float(weight)]
                    for start, end, weight in fold.get("downweight_segments", ())
                ],
                "blackout_start": str(fold.get("blackout_start", "")),
                "blackout_end": str(fold.get("blackout_end", "")),
                "blackout_length_days": fold.get("blackout_length_days", ""),
                "test_start": fold["test_start"],
                "test_end": fold["test_end"],
                "test_segments": [list(segment) for segment in fold_test_segments(fold)],
                "allow_train_test_overlap": bool(fold.get("allow_train_test_overlap", False)),
            }
            for fold in folds
        ],
        "execution_stress": bool(args.execution_stress),
        "execution_replay": args.execution_replay,
        "execution_latencies": list(args.execution_latencies) if args.execution_replay == "pm_event_subsecond" else [],
        "execution_entry_modes": list(args.execution_entry_modes) if args.execution_replay == "pm_event_subsecond" else ["guarded_fast_1c"],
        "execution_latches": [
            {"label": label, "seconds": seconds}
            for label, seconds in (args.execution_latches if args.execution_replay == "pm_event_subsecond" else (("legacy_no_latch", 0.0),))
        ],
        "decision_coalesce_ms": int(args.decision_coalesce_ms) if args.execution_replay == "pm_event_subsecond" else 0,
        "candidate_allowlist": [
            {"surface_arm": surface_arm, "spec_id": spec_id}
            for surface_arm, spec_id in args.candidate_allowlist
        ],
        "latch_min_edge": LATCH_MIN_EDGE,
        "reuse_train_features": bool(args.reuse_train_features),
        "train_feature_cache_size": int(args.train_feature_cache_size),
        "arms": manifest_arms,
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[{stamp()}] DONE output={output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
