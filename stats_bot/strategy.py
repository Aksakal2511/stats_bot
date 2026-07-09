from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LONDON_TZ = ZoneInfo("Europe/London")
SESSION_ORDER = [
    "ASIA_DEEP_LOW",
    "ASIA_PRE_ACTIVE",
    "ASIA_CORE_EARLY",
    "ASIA_EUROPE_HANDOVER",
    "EUROPE_OPEN",
    "EUROPE_MIDDAY",
    "US_PREOPEN",
    "US_OPEN_EARLY",
    "US_OPEN_LATE",
    "US_LATE_CORE",
    "US_AFTERHOURS_EARLY",
    "US_AFTERHOURS_LATE",
]
POLICY_KEY_FIELDS = (
    "day_type",
    "session_bucket",
    "tte_bucket",
    "signed_bps_bucket",
    "volatility_bucket",
    "up_ask_bucket",
    "down_ask_bucket",
)


def trade_style_from_bps_bucket(side: str, signed_bps_bucket: str | None) -> str | None:
    side = side.upper()
    label = str(signed_bps_bucket or "")
    if label == "near_zero":
        return "neutral"
    if label.startswith("pos") or label == "gt_pos25":
        return "continuation" if side == "UP" else "mean_reversion"
    if label.startswith("neg") or label == "le_neg25":
        return "continuation" if side == "DOWN" else "mean_reversion"
    return None


@dataclass(frozen=True)
class PolicyRecord:
    key: tuple[str, str, str, str, str, str, str]
    raw: dict[str, Any]

    @property
    def key_text(self) -> str:
        return "|".join(self.key)

    def side_value_valid(self, side: str) -> bool:
        return truthy(self.raw.get(f"{side.lower()}_value_valid"))

    def side_trade_value(self, side: str) -> float | None:
        return finite_float(self.raw.get(f"{side.lower()}_trade_value"))

    def side_max_live_ask_for_min_edge(self, side: str) -> float | None:
        return finite_float(self.raw.get(f"{side.lower()}_max_live_ask_for_min_edge"))

    def side_trade_style(self, side: str) -> str | None:
        value = self.raw.get(f"{side.lower()}_trade_style")
        if value not in (None, ""):
            return str(value)
        return trade_style_from_bps_bucket(side, self.key[3])



@dataclass(frozen=True)
class PolicyCube:
    records: dict[tuple[str, str, str, str, str, str, str], PolicyRecord]

    def get(self, key: tuple[str, str, str, str, str, str, str] | None) -> PolicyRecord | None:
        if key is None:
            return None
        return self.records.get(key)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class VolatilityEdges:
    smoothing_seconds: int
    global_p33: float
    global_p66: float
    partitions: dict[tuple[str, str], tuple[float, float]]


@dataclass(frozen=True)
class EntryEdgeThreshold:
    threshold: float
    enabled: bool = True
    source_bundle: str = ""
    source_penalty: float | None = None
    source_current_threshold: float | None = None
    notes: str = ""


EntryEdgeThresholdKey = tuple[str, str] | tuple[str, str, str]
EntryEdgeThresholdLike = EntryEdgeThreshold | float
EntryEdgeThresholdMap = dict[EntryEdgeThresholdKey, EntryEdgeThresholdLike]


@dataclass(frozen=True)
class EntryDecision:
    viable: bool
    blockers: tuple[str, ...]
    policy_key: tuple[str, ...] | None
    policy_record: Any | None
    selected_side: str | None
    selected_bid: float | None
    selected_ask: float | None
    selected_mid: float | None
    selected_spread: float | None
    selected_trade_value: float | None
    selected_policy_edge: float | None
    selected_trade_style: str | None
    entry_edge_threshold: float | None
    entry_session_enabled: bool | None
    threshold_bps: float | None

    @property
    def policy_key_text(self) -> str:
        if self.policy_key is None:
            return ""
        return "|".join(self.policy_key)


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    policy_no_longer_supportive: bool
    signed_bps_materially_against: bool
    current_policy_side: str | None
    current_policy_key: tuple[str, str, str, str, str, str, str] | None
    current_bps: float | None
    entry_bps: float
    threshold_bps: float
    exit_bid: float | None


@dataclass(frozen=True)
class SideCandidate:
    side: str
    bid: float | None
    ask: float | None
    mid: float | None
    spread: float | None
    trade_value: float | None
    policy_edge: float | None
    entry_edge_threshold: float | None
    max_live_ask_for_min_edge: float | None
    trade_style: str | None
    value_valid: bool
    quote_valid: bool
    price_in_range: bool
    edge_valid: bool


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def classify_day_type_and_session(timestamp_utc: datetime) -> tuple[str, str, datetime]:
    if timestamp_utc.tzinfo is None:
        timestamp_utc = timestamp_utc.replace(tzinfo=timezone.utc)
    london_ts = timestamp_utc.astimezone(LONDON_TZ)
    day_type = "weekend" if london_ts.weekday() >= 5 else "weekday"
    hour = london_ts.hour
    if 0 <= hour < 2:
        session = "ASIA_DEEP_LOW"
    elif 2 <= hour < 4:
        session = "ASIA_PRE_ACTIVE"
    elif 4 <= hour < 6:
        session = "ASIA_CORE_EARLY"
    elif 6 <= hour < 8:
        session = "ASIA_EUROPE_HANDOVER"
    elif 8 <= hour < 10:
        session = "EUROPE_OPEN"
    elif 10 <= hour < 12:
        session = "EUROPE_MIDDAY"
    elif 12 <= hour < 14:
        session = "US_PREOPEN"
    elif 14 <= hour < 16:
        session = "US_OPEN_EARLY"
    elif 16 <= hour < 18:
        session = "US_OPEN_LATE"
    elif 18 <= hour < 20:
        session = "US_LATE_CORE"
    elif 20 <= hour < 22:
        session = "US_AFTERHOURS_EARLY"
    else:
        session = "US_AFTERHOURS_LATE"
    return day_type, session, london_ts


def bucket_signed_bps(value: Any) -> str | None:
    v = finite_float(value)
    if v is None:
        return None
    if v <= -25:
        return "le_neg25"
    if v <= -20:
        return "neg25_to_neg20"
    if v <= -15:
        return "neg20_to_neg15"
    if v <= -10:
        return "neg15_to_neg10"
    if v <= -7.5:
        return "neg10_to_neg7p5"
    if v <= -5:
        return "neg7p5_to_neg5"
    if v <= -2.5:
        return "neg5_to_neg2p5"
    if v <= -1:
        return "neg2p5_to_neg1"
    if v <= -0.01:
        return "neg1_to_neg0p01"
    if v <= 0.01:
        return "near_zero"
    if v <= 1:
        return "pos0p01_to_pos1"
    if v <= 2.5:
        return "pos1_to_pos2p5"
    if v <= 5:
        return "pos2p5_to_pos5"
    if v <= 7.5:
        return "pos5_to_pos7p5"
    if v <= 10:
        return "pos7p5_to_pos10"
    if v <= 15:
        return "pos10_to_pos15"
    if v <= 20:
        return "pos15_to_pos20"
    if v <= 25:
        return "pos20_to_pos25"
    return "gt_pos25"


def bucket_tte(value: Any) -> str | None:
    v = finite_float(value)
    if v is None:
        return None
    second = int(math.floor(v))
    if 60 <= second <= 89:
        return "60-89"
    if 90 <= second <= 119:
        return "90-119"
    if 120 <= second <= 149:
        return "120-149"
    if 150 <= second <= 179:
        return "150-179"
    if 180 <= second <= 209:
        return "180-209"
    if 210 <= second <= 239:
        return "210-239"
    if 240 <= second <= 269:
        return "240-269"
    if 270 <= second <= 300:
        return "270-300"
    return None


def bucket_price(value: Any) -> str | None:
    v = finite_float(value)
    if v is None:
        return None
    if 0.05 <= v < 0.15:
        return "p05_to_p15"
    if 0.15 <= v < 0.25:
        return "p15_to_p25"
    if 0.25 <= v < 0.35:
        return "p25_to_p35"
    if 0.35 <= v < 0.40:
        return "p35_to_p40"
    if 0.40 <= v < 0.45:
        return "p40_to_p45"
    if 0.45 <= v < 0.48:
        return "p45_to_p48"
    if 0.48 <= v < 0.52:
        return "p48_to_p52"
    if 0.52 <= v < 0.55:
        return "p52_to_p55"
    if 0.55 <= v < 0.60:
        return "p55_to_p60"
    if 0.60 <= v < 0.65:
        return "p60_to_p65"
    if 0.65 <= v < 0.75:
        return "p65_to_p75"
    if 0.75 <= v < 0.85:
        return "p75_to_p85"
    if 0.85 <= v <= 0.95:
        return "p85_to_p95"
    return None


def bucket_volatility(value: Any, day_type: str, session_bucket: str, edges: VolatilityEdges) -> str | None:
    v = finite_float(value)
    if v is None:
        return None
    p33, p66 = edges.partitions.get((day_type, session_bucket), (edges.global_p33, edges.global_p66))
    if v <= p33:
        return "low"
    if v <= p66:
        return "medium"
    return "high"


def _rank_tuple(raw: dict[str, Any]) -> tuple[float, ...]:
    fields = [
        "valid_side_count",
        "up_positive_date_rate_path",
        "down_positive_date_rate_path",
        "up_prob_profitable_bid_available_before_terminal",
        "down_prob_profitable_bid_available_before_terminal",
        "up_median_adverse_before_profit_before_terminal",
        "down_median_adverse_before_profit_before_terminal",
        "up_median_best_available_edge_before_terminal",
        "down_median_best_available_edge_before_terminal",
        "unique_market_count",
        "row_count",
    ]
    values: list[float] = []
    for field in fields:
        value = finite_float(raw.get(field))
        values.append(value if value is not None else float("-inf"))
    return tuple(values)


def load_policy_cube(path: Path) -> PolicyCube:
    rows: list[dict[str, Any]] = []
    required_side_fields = (
        "up_trade_value",
        "down_trade_value",
        "up_value_valid",
        "down_value_valid",
    )
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            missing = [field for field in POLICY_KEY_FIELDS if raw.get(field) in (None, "")]
            if missing:
                raise ValueError(f"{path}:{line_no} missing policy key fields: {missing}")
            missing_side_fields = [field for field in required_side_fields if raw.get(field) in (None, "")]
            if missing_side_fields:
                raise ValueError(f"{path}:{line_no} missing side-value fields: {missing_side_fields}")
            if not (truthy(raw.get("up_value_valid")) or truthy(raw.get("down_value_valid"))):
                continue
            for side in ("up", "down"):
                if truthy(raw.get(f"{side}_value_valid")):
                    trade_value = finite_float(raw.get(f"{side}_trade_value"))
                    if trade_value is None:
                        raise ValueError(f"{path}:{line_no} valid {side.upper()} side is missing trade value")
            rows.append(raw)
    rows.sort(key=_rank_tuple, reverse=True)
    records: dict[tuple[str, str, str, str, str, str, str], PolicyRecord] = {}
    for raw in rows:
        key = tuple(str(raw[field]) for field in POLICY_KEY_FIELDS)
        if key in records:
            continue
        records[key] = PolicyRecord(key=key, raw=raw)
    return PolicyCube(records)


def load_exit_thresholds(path: Path) -> dict[tuple[str, str], float]:
    thresholds: dict[tuple[str, str], float] = {}
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            day_type = str(row.get("day_type") or row.get("entry_day_type") or row.get("Day Type") or "").strip()
            session_bucket = str(
                row.get("session_bucket") or row.get("entry_session_bucket") or row.get("Session Bucket") or ""
            ).strip()
            threshold = finite_float(
                row.get("exit_bps_threshold")
                or row.get("bps")
                or row.get("BPS")
                or row.get("Exit BPS Threshold")
            )
            if day_type and session_bucket and threshold is not None:
                thresholds[(day_type, session_bucket)] = threshold
    if not thresholds:
        raise ValueError(f"No exit thresholds loaded from {path}")
    return thresholds


def entry_threshold_value(value: EntryEdgeThresholdLike | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, EntryEdgeThreshold):
        return value.threshold
    return finite_float(value)


def entry_threshold_enabled(value: EntryEdgeThresholdLike | None) -> bool | None:
    if value is None:
        return None
    if isinstance(value, EntryEdgeThreshold):
        return value.enabled
    return True


def normalize_trade_style(value: Any) -> str:
    return str(value or "").strip().lower()


def entry_threshold_policy(
    thresholds: EntryEdgeThresholdMap,
    day_type: str,
    session_bucket: str,
    trade_style: str | None = None,
) -> EntryEdgeThresholdLike | None:
    style = normalize_trade_style(trade_style)
    if style:
        style_policy = thresholds.get((day_type, session_bucket, style))
        if style_policy is not None:
            return style_policy
    return thresholds.get((day_type, session_bucket))


def load_entry_edge_thresholds(path: Path) -> dict[EntryEdgeThresholdKey, EntryEdgeThreshold]:
    thresholds: dict[EntryEdgeThresholdKey, EntryEdgeThreshold] = {}
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            day_type = str(row.get("day_type") or row.get("entry_day_type") or row.get("Day Type") or "").strip()
            session_bucket = str(
                row.get("session_bucket") or row.get("entry_session_bucket") or row.get("Session Bucket") or ""
            ).strip()
            trade_style = normalize_trade_style(
                row.get("trade_style") or row.get("selected_trade_style") or row.get("signal_trade_style")
            )
            threshold = finite_float(
                row.get("entry_edge_threshold")
                or row.get("edge_threshold")
                or row.get("threshold")
                or row.get("Entry Edge Threshold")
            )
            if day_type and session_bucket and threshold is not None:
                enabled_raw = row.get("enabled")
                enabled = True if enabled_raw in (None, "") else truthy(enabled_raw)
                key: EntryEdgeThresholdKey = (
                    (day_type, session_bucket, trade_style) if trade_style else (day_type, session_bucket)
                )
                thresholds[key] = EntryEdgeThreshold(
                    threshold=threshold,
                    enabled=enabled,
                    source_bundle=str(row.get("source_bundle") or "").strip(),
                    source_penalty=finite_float(row.get("source_penalty") or row.get("penalty")),
                    source_current_threshold=finite_float(
                        row.get("source_current_threshold") or row.get("current_threshold")
                    ),
                    notes=str(row.get("notes") or "").strip(),
                )
    if not thresholds:
        raise ValueError(f"No entry edge thresholds loaded from {path}")
    return thresholds


def load_volatility_edges(path: Path) -> VolatilityEdges:
    obj = json.loads(path.read_text(encoding="utf-8"))
    global_obj = obj.get("global") or {}
    global_p33 = finite_float(global_obj.get("p33"))
    global_p66 = finite_float(global_obj.get("p66"))
    if global_p33 is None or global_p66 is None:
        raise ValueError(f"Missing global p33/p66 in {path}")
    partitions: dict[tuple[str, str], tuple[float, float]] = {}
    for row in obj.get("partitions", []):
        p33 = finite_float(row.get("p33"))
        p66 = finite_float(row.get("p66"))
        day_type = str(row.get("day_type") or "").strip()
        session_bucket = str(row.get("session_bucket") or "").strip()
        if day_type and session_bucket and p33 is not None and p66 is not None:
            partitions[(day_type, session_bucket)] = (p33, p66)
    return VolatilityEdges(
        smoothing_seconds=int(obj.get("smoothing_seconds") or 30),
        global_p33=global_p33,
        global_p66=global_p66,
        partitions=partitions,
    )


def build_policy_key(snapshot: dict[str, Any]) -> tuple[str, str, str, str, str, str, str] | None:
    values = [
        snapshot.get("day_type"),
        snapshot.get("session_bucket"),
        snapshot.get("tte_bucket"),
        snapshot.get("signed_bps_bucket"),
        snapshot.get("volatility_bucket"),
        snapshot.get("up_ask_bucket"),
        snapshot.get("down_ask_bucket"),
    ]
    if any(value in (None, "") for value in values):
        return None
    return tuple(str(value) for value in values)


def _side_candidate(
    snapshot: dict[str, Any],
    record: PolicyRecord,
    side: str,
    *,
    min_price: float,
    max_price: float,
    entry_edge_threshold: float | None,
) -> SideCandidate:
    side = side.upper()
    prefix = side.lower()
    bid = finite_float(snapshot.get(f"{prefix}_bid"))
    ask = finite_float(snapshot.get(f"{prefix}_ask"))
    mid = finite_float(snapshot.get(f"{prefix}_mid"))
    spread = finite_float(snapshot.get(f"{prefix}_spread", snapshot.get(f"spread_{prefix}")))
    trade_value = record.side_trade_value(side)
    max_live_ask = record.side_max_live_ask_for_min_edge(side)
    policy_edge = trade_value - ask if trade_value is not None and ask is not None else None
    quote_valid = bool(
        bid is not None
        and ask is not None
        and mid is not None
        and 0.0 <= bid <= ask <= 1.0
        and 0.0 <= mid <= 1.0
    )
    price_in_range = bool(ask is not None and min_price <= ask <= max_price)
    edge_valid = bool(
        record.side_value_valid(side)
        and quote_valid
        and price_in_range
        and policy_edge is not None
        and entry_edge_threshold is not None
        and policy_edge >= entry_edge_threshold - 1e-12
    )
    return SideCandidate(
        side=side,
        bid=bid,
        ask=ask,
        mid=mid,
        spread=spread,
        trade_value=trade_value,
        policy_edge=policy_edge,
        entry_edge_threshold=entry_edge_threshold,
        max_live_ask_for_min_edge=max_live_ask,
        trade_style=record.side_trade_style(side),
        value_valid=record.side_value_valid(side),
        quote_valid=quote_valid,
        price_in_range=price_in_range,
        edge_valid=edge_valid,
    )


def policy_side_candidates(
    snapshot: dict[str, Any],
    record: PolicyRecord,
    *,
    min_price: float,
    max_price: float,
    entry_edge_threshold: float | None,
) -> dict[str, SideCandidate]:
    return {
        side: _side_candidate(
            snapshot,
            record,
            side,
            min_price=min_price,
            max_price=max_price,
            entry_edge_threshold=entry_edge_threshold,
        )
        for side in ("UP", "DOWN")
    }


def choose_policy_side(candidates: dict[str, SideCandidate]) -> SideCandidate | None:
    executable = [candidate for candidate in candidates.values() if candidate.edge_valid]
    if not executable:
        return None
    return max(
        executable,
        key=lambda candidate: (
            candidate.policy_edge if candidate.policy_edge is not None else float("-inf"),
            candidate.trade_value if candidate.trade_value is not None else float("-inf"),
            1 if candidate.side == "UP" else 0,
        ),
    )


def current_policy_side(
    snapshot: dict[str, Any],
    policy_cube: PolicyCube,
    entry_edge_thresholds: EntryEdgeThresholdMap,
) -> tuple[str | None, PolicyRecord | None]:
    key = build_policy_key(snapshot)
    record = policy_cube.get(key)
    if record is None:
        return None, None
    day_type = str(snapshot.get("day_type") or "")
    session_bucket = str(snapshot.get("session_bucket") or "")
    candidates = policy_side_candidates(
        snapshot,
        record,
        min_price=0.0,
        max_price=1.0,
        entry_edge_threshold=None,
    )
    styled_candidates: dict[str, SideCandidate] = {}
    for side, candidate in candidates.items():
        threshold_policy = entry_threshold_policy(entry_edge_thresholds, day_type, session_bucket, candidate.trade_style)
        if entry_threshold_enabled(threshold_policy) is False:
            continue
        threshold = entry_threshold_value(threshold_policy)
        styled_candidates[side] = _side_candidate(
            snapshot,
            record,
            side,
            min_price=0.0,
            max_price=1.0,
            entry_edge_threshold=threshold,
        )
    candidate = choose_policy_side(styled_candidates)
    return (candidate.side if candidate is not None else None), record


def evaluate_entry(
    snapshot: dict[str, Any] | None,
    policy_cube: PolicyCube,
    exit_thresholds: dict[tuple[str, str], float],
    entry_edge_thresholds: EntryEdgeThresholdMap,
    *,
    min_price: float,
    max_price: float,
    min_tte: int,
    max_tte: int,
    start_block_seconds: int,
) -> EntryDecision:
    blockers: list[str] = []
    if snapshot is None:
        return EntryDecision(
            viable=False,
            blockers=("NO_SNAPSHOT",),
            policy_key=None,
            policy_record=None,
            selected_side=None,
            selected_bid=None,
            selected_ask=None,
            selected_mid=None,
            selected_spread=None,
            selected_trade_value=None,
            selected_policy_edge=None,
            selected_trade_style=None,
            entry_edge_threshold=None,
            entry_session_enabled=None,
            threshold_bps=None,
        )

    tte = finite_float(snapshot.get("canonical_tte_seconds", snapshot.get("tte_seconds")))
    elapsed = finite_float(snapshot.get("elapsed_seconds_since_market_start"))
    if tte is None:
        blockers.append("TTE_MISSING")
    elif not (min_tte <= tte <= max_tte):
        blockers.append("TTE_OUTSIDE_ENTRY_RANGE")
    if elapsed is None:
        blockers.append("ELAPSED_SECONDS_MISSING")
    elif elapsed < start_block_seconds:
        blockers.append("WINDOW_START_BLOCK_ACTIVE")

    cb_bps = finite_float(snapshot.get("cb_signed_bps_raw", snapshot.get("signed_bps_raw")))
    if cb_bps is None:
        blockers.append("CB_SIGNED_BPS_MISSING")
    vol = finite_float(snapshot.get("volatility_smoothed_raw"))
    if vol is None:
        blockers.append("VOLATILITY_MISSING")

    for side in ("up", "down"):
        bid = finite_float(snapshot.get(f"{side}_bid"))
        ask = finite_float(snapshot.get(f"{side}_ask"))
        mid = finite_float(snapshot.get(f"{side}_mid"))
        if bid is None or ask is None or mid is None:
            blockers.append(f"{side.upper()}_QUOTE_MISSING")
        elif not (0.0 <= bid <= ask <= 1.0 and 0.0 <= mid <= 1.0):
            blockers.append(f"{side.upper()}_QUOTE_INVALID")

    key = build_policy_key(snapshot)
    record = policy_cube.get(key)
    if key is None:
        blockers.append("POLICY_KEY_INCOMPLETE")
    elif record is None:
        blockers.append("POLICY_KEY_NOT_FOUND")

    selected_candidate: SideCandidate | None = None
    selected_bid = selected_ask = selected_mid = selected_spread = None
    selected_trade_value = selected_policy_edge = None
    selected_trade_style = None
    day_type = str(snapshot.get("day_type") or "")
    session_bucket = str(snapshot.get("session_bucket") or "")
    entry_edge_threshold = None
    entry_session_enabled = None
    if record is not None and entry_session_enabled is not False:
        candidates: dict[str, SideCandidate] = {}
        threshold_missing = True
        threshold_disabled = True
        for side in ("UP", "DOWN"):
            style = record.side_trade_style(side)
            threshold_policy = entry_threshold_policy(entry_edge_thresholds, day_type, session_bucket, style)
            side_threshold = entry_threshold_value(threshold_policy)
            side_enabled = entry_threshold_enabled(threshold_policy)
            threshold_missing = threshold_missing and side_threshold is None
            threshold_disabled = threshold_disabled and side_enabled is False
            if side_enabled is False:
                side_threshold = None
            candidates[side] = _side_candidate(
                snapshot,
                record,
                side,
                min_price=min_price,
                max_price=max_price,
                entry_edge_threshold=side_threshold,
            )
        if threshold_missing:
            blockers.append("ENTRY_EDGE_THRESHOLD_NOT_FOUND")
        elif threshold_disabled:
            blockers.append("ENTRY_SESSION_DISABLED")
        selected_candidate = choose_policy_side(candidates)
        if selected_candidate is None:
            if not any(candidate.value_valid for candidate in candidates.values()):
                blockers.append("POLICY_NO_SIDE_VALUE_VALID")
            elif any(candidate.value_valid and candidate.quote_valid and not candidate.price_in_range for candidate in candidates.values()):
                blockers.append("ENTRY_PRICE_OUTSIDE_RANGE")
            elif any(candidate.value_valid and not candidate.quote_valid for candidate in candidates.values()):
                blockers.append("SELECTED_SIDE_QUOTE_MISSING")
            else:
                blockers.append("POLICY_NO_SIDE_EDGE")
        else:
            selected_bid = selected_candidate.bid
            selected_ask = selected_candidate.ask
            selected_mid = selected_candidate.mid
            selected_spread = selected_candidate.spread
            selected_trade_value = selected_candidate.trade_value
            selected_policy_edge = selected_candidate.policy_edge
            selected_trade_style = selected_candidate.trade_style
            entry_edge_threshold = selected_candidate.entry_edge_threshold
            entry_session_enabled = True

    threshold = exit_thresholds.get((day_type, session_bucket))
    if threshold is None:
        blockers.append("EXIT_THRESHOLD_NOT_FOUND")

    return EntryDecision(
        viable=not blockers,
        blockers=tuple(blockers),
        policy_key=key,
        policy_record=record,
        selected_side=selected_candidate.side if selected_candidate is not None else None,
        selected_bid=selected_bid,
        selected_ask=selected_ask,
        selected_mid=selected_mid,
        selected_spread=selected_spread,
        selected_trade_value=selected_trade_value,
        selected_policy_edge=selected_policy_edge,
        selected_trade_style=selected_trade_style,
        entry_edge_threshold=entry_edge_threshold,
        entry_session_enabled=entry_session_enabled,
        threshold_bps=threshold,
    )


def evaluate_exit(
    *,
    held_side: str,
    entry_bps: float,
    threshold_bps: float,
    entry_edge_threshold: float | None,
    snapshot: dict[str, Any] | None,
    policy_cube: PolicyCube,
) -> ExitDecision:
    if snapshot is None:
        return ExitDecision(False, "NO_SNAPSHOT", False, False, None, None, None, entry_bps, threshold_bps, None)
    current_bps = finite_float(snapshot.get("cb_signed_bps_raw", snapshot.get("signed_bps_raw")))
    policy_key = build_policy_key(snapshot)
    record = policy_cube.get(policy_key)
    held_side = held_side.upper()
    current_side = None
    held_side_supported = False
    if record is not None:
        candidates = policy_side_candidates(
            snapshot,
            record,
            min_price=0.0,
            max_price=1.0,
            entry_edge_threshold=entry_edge_threshold,
        )
        current_candidate = choose_policy_side(candidates)
        current_side = current_candidate.side if current_candidate is not None else None
        held_candidate = candidates.get(held_side)
        held_side_supported = bool(held_candidate and held_candidate.edge_valid)
    policy_no_longer_supportive = not held_side_supported
    if current_bps is None:
        materially_against = False
    elif held_side == "UP":
        materially_against = current_bps <= entry_bps - threshold_bps
    else:
        materially_against = current_bps >= entry_bps + threshold_bps
    prefix = held_side.lower()
    exit_bid = finite_float(snapshot.get(f"{prefix}_bid"))
    should_exit = bool(policy_no_longer_supportive and materially_against)
    reason = "HOLD"
    if should_exit:
        reason = "policy_no_longer_supportive,signed_bps_materially_against"
    elif policy_no_longer_supportive:
        reason = "POLICY_NOT_SUPPORTIVE_ONLY"
    elif materially_against:
        reason = "SIGNED_BPS_AGAINST_ONLY"
    return ExitDecision(
        should_exit=should_exit,
        reason=reason,
        policy_no_longer_supportive=policy_no_longer_supportive,
        signed_bps_materially_against=materially_against,
        current_policy_side=current_side,
        current_policy_key=policy_key,
        current_bps=current_bps,
        entry_bps=entry_bps,
        threshold_bps=threshold_bps,
        exit_bid=exit_bid,
    )
