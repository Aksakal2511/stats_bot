from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .strategy import (
    VolatilityEdges,
    bucket_price,
    bucket_signed_bps,
    bucket_tte,
    bucket_volatility,
    classify_day_type_and_session,
    finite_float,
)


VOLATILITY_WINDOW_SECONDS = 60
BTC_MOMENTUM_GUARD_WINDOW_SECONDS = 20
MOVE_DZ_MOMENTUM_SECONDS = 2
BTC_FIELDS = ("open_1s", "high_1s", "low_1s", "close_1s", "volume_1s")
PM_FIELDS = (
    "up_bid",
    "up_ask",
    "up_mid",
    "up_spread",
    "up_bid_depth_total",
    "up_ask_depth_total",
    "up_best_bid_depth",
    "up_best_ask_depth",
    "down_bid",
    "down_ask",
    "down_mid",
    "down_spread",
    "down_bid_depth_total",
    "down_ask_depth_total",
    "down_best_bid_depth",
    "down_best_ask_depth",
)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _coerce_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return _ensure_utc(to_pydatetime())
        except Exception:
            return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _coerce_rows(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    to_dict = getattr(table, "to_dict", None)
    if callable(to_dict):
        try:
            rows = to_dict("records")
        except TypeError:
            rows = to_dict(orient="records")
        return [row for row in rows if isinstance(row, dict)]
    return [row for row in table if isinstance(row, dict)]


def _prepare_history(table: Any, fields: tuple[str, ...]) -> dict[datetime, dict[str, float]]:
    rows_by_ts: dict[datetime, dict[str, float]] = {}
    for raw in _coerce_rows(table):
        ts = _coerce_timestamp(raw.get("timestamp_utc"))
        if ts is None:
            continue
        normalized: dict[str, float] = {}
        for field in fields:
            value = finite_float(raw.get(field))
            if value is not None:
                normalized[field] = value
        rows_by_ts[ts] = normalized
    return rows_by_ts


def _latest_volatility(close_values: list[float | None], smoothing_seconds: int) -> tuple[float | None, float | None]:
    raw_values: list[float | None] = []
    for idx in range(len(close_values)):
        raw: float | None = None
        if idx >= VOLATILITY_WINDOW_SECONDS:
            sumsq = 0.0
            valid = True
            for ret_idx in range(idx - VOLATILITY_WINDOW_SECONDS + 1, idx + 1):
                prev_close = close_values[ret_idx - 1]
                curr_close = close_values[ret_idx]
                if (
                    prev_close is None
                    or curr_close is None
                    or not math.isfinite(prev_close)
                    or not math.isfinite(curr_close)
                    or prev_close <= 0.0
                    or curr_close <= 0.0
                ):
                    valid = False
                    break
                log_ret = math.log(curr_close / prev_close)
                sumsq += log_ret * log_ret
            if valid:
                raw = math.sqrt(sumsq) * 10000.0
        raw_values.append(raw)

    latest_raw = raw_values[-1] if raw_values else None
    smoothing_window = max(1, int(smoothing_seconds))
    recent = raw_values[-smoothing_window:]
    valid_recent = [value for value in recent if value is not None and math.isfinite(value)]
    smoothed = (sum(valid_recent) / len(valid_recent)) if valid_recent else None
    return latest_raw, smoothed


def _momentum_bps(rows: list[dict[str, Any]], *, latest_ts: datetime, latest_close: float, seconds: int) -> float | None:
    target_ts = latest_ts.timestamp() - float(seconds)
    prior_close: float | None = None
    for row in rows:
        ts = _coerce_timestamp(row.get("timestamp_utc"))
        if ts is None or ts.timestamp() > target_ts:
            break
        close = finite_float(row.get("close_1s"))
        if close is not None and close > 0.0:
            prior_close = close
    if prior_close is None or prior_close <= 0.0 or latest_close <= 0.0:
        return None
    return 10000.0 * (latest_close - prior_close) / prior_close


def _distance_to_beat_z(
    *,
    close: float | None,
    anchor: float | None,
    local_volatility_1h_bps: float | None,
    tte_seconds: float | None,
) -> float | None:
    if (
        close is None
        or anchor is None
        or local_volatility_1h_bps is None
        or tte_seconds is None
        or anchor <= 0.0
        or local_volatility_1h_bps <= 0.0
    ):
        return None
    distance_bps = 10_000.0 * (float(close) - float(anchor)) / float(anchor)
    remaining_vol = max(0.05, float(local_volatility_1h_bps) * math.sqrt(max(1.0, float(tte_seconds)) / 3600.0))
    return float(distance_bps / remaining_vol)


def _distance_z_delta(
    rows: list[dict[str, Any]],
    *,
    now_utc: datetime,
    market_end_utc: datetime,
    latest_close: float,
    anchor: float,
    local_volatility_1h_bps: float | None,
    current_tte_seconds: float,
    seconds: int,
) -> tuple[float | None, float | None]:
    current_z = _distance_to_beat_z(
        close=latest_close,
        anchor=anchor,
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=current_tte_seconds,
    )
    target_ts = now_utc.timestamp() - float(seconds)
    prior_close: float | None = None
    for row in rows:
        ts = _coerce_timestamp(row.get("timestamp_utc"))
        if ts is None or ts.timestamp() > target_ts:
            break
        close = finite_float(row.get("close_1s"))
        if close is not None and close > 0.0:
            prior_close = close
    if current_z is None or prior_close is None:
        return current_z, None
    prior_dt = datetime.fromtimestamp(target_ts, tz=timezone.utc)
    prior_tte = max(0.0, (market_end_utc - prior_dt).total_seconds())
    prior_z = _distance_to_beat_z(
        close=prior_close,
        anchor=anchor,
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=prior_tte,
    )
    if prior_z is None:
        return current_z, None
    return current_z, float(current_z - prior_z)


def compute_live_snapshot(
    *,
    btc_history,
    pm_history,
    now_utc: datetime,
    market_start_utc: datetime,
    market_end_utc: datetime,
    window_open_btc_price: float,
    volatility_edges: VolatilityEdges,
    market_slug: str,
    local_volatility_1h_bps: float | None = None,
    local_volatility_status: str = "LOCAL_VOL_WARMUP",
    local_volatility_age_minutes: int | None = None,
) -> dict[str, Any] | None:
    now_utc = _ensure_utc(now_utc)
    market_start_utc = _ensure_utc(market_start_utc)
    market_end_utc = _ensure_utc(market_end_utc)
    open_price = finite_float(window_open_btc_price)
    if open_price is None or open_price <= 0.0:
        return None

    btc_rows = _prepare_history(btc_history, BTC_FIELDS)
    pm_rows = _prepare_history(pm_history, PM_FIELDS)
    if not btc_rows or not pm_rows:
        return None

    timeline = sorted(set(btc_rows) | set(pm_rows))
    if not timeline:
        return None

    current: dict[str, float] = {}
    merged_rows: list[dict[str, Any]] = []
    for ts in timeline:
        btc_row = btc_rows.get(ts)
        if btc_row:
            current.update(btc_row)
        pm_row = pm_rows.get(ts)
        if pm_row:
            current.update(pm_row)
        merged_rows.append({"timestamp_utc": ts, **current})

    close_values = [finite_float(row.get("close_1s")) for row in merged_rows]
    if not any(value is not None for value in close_values):
        return None

    latest = merged_rows[-1]
    latest_ts = _coerce_timestamp(latest.get("timestamp_utc")) or now_utc
    latest_close = finite_float(latest.get("close_1s"))
    if latest_close is None:
        return None
    btc_mom_20s_bps = _momentum_bps(
        merged_rows,
        latest_ts=latest_ts,
        latest_close=latest_close,
        seconds=BTC_MOMENTUM_GUARD_WINDOW_SECONDS,
    )

    volatility_raw, volatility_smoothed_raw = _latest_volatility(close_values, volatility_edges.smoothing_seconds)
    signed_bps_raw = 10000.0 * (latest_close - open_price) / open_price
    day_type, session_bucket, london_ts = classify_day_type_and_session(now_utc)
    tte_seconds = max(0.0, (market_end_utc - now_utc).total_seconds())
    elapsed_seconds = max(0.0, (now_utc - market_start_utc).total_seconds())
    distance_z, distance_z_delta_2s = _distance_z_delta(
        merged_rows,
        now_utc=now_utc,
        market_end_utc=market_end_utc,
        latest_close=latest_close,
        anchor=float(open_price),
        local_volatility_1h_bps=local_volatility_1h_bps,
        current_tte_seconds=float(tte_seconds),
        seconds=MOVE_DZ_MOMENTUM_SECONDS,
    )
    signed_bps_bucket = bucket_signed_bps(signed_bps_raw)
    tte_bucket = bucket_tte(tte_seconds)
    volatility_bucket = (
        bucket_volatility(volatility_smoothed_raw, day_type, session_bucket, volatility_edges)
        if volatility_smoothed_raw is not None
        else None
    )
    up_ask = finite_float(latest.get("up_ask"))
    down_ask = finite_float(latest.get("down_ask"))
    up_ask_bucket = bucket_price(up_ask)
    down_ask_bucket = bucket_price(down_ask)

    def val(name: str) -> float:
        value = finite_float(latest.get(name))
        return float("nan") if value is None else value

    up_bid = val("up_bid")
    down_bid = val("down_bid")
    up_mid = val("up_mid")
    down_mid = val("down_mid")
    up_spread = val("up_spread")
    down_spread = val("down_spread")
    yes_no_spread = float("nan")
    if math.isfinite(up_spread) and math.isfinite(down_spread):
        yes_no_spread = float(up_spread + down_spread)

    return {
        "timestamp_utc": now_utc,
        "timestamp_london": london_ts,
        "canonical_window_id": market_slug,
        "market_start_utc": market_start_utc,
        "market_end_utc": market_end_utc,
        "canonical_tte_seconds": float(tte_seconds),
        "tte_seconds": float(tte_seconds),
        "elapsed_seconds_since_market_start": float(elapsed_seconds),
        "day_type": day_type,
        "session_bucket": session_bucket,
        "cb_close_1s": latest_close,
        "btc_mom_20s_bps": float("nan") if btc_mom_20s_bps is None else float(btc_mom_20s_bps),
        "distance_to_beat_z": float("nan") if distance_z is None else float(distance_z),
        "distance_z_delta_2s": float("nan") if distance_z_delta_2s is None else float(distance_z_delta_2s),
        "up_side_dz_delta_2s": float("nan") if distance_z_delta_2s is None else float(distance_z_delta_2s),
        "down_side_dz_delta_2s": float("nan") if distance_z_delta_2s is None else float(-distance_z_delta_2s),
        "window_open_btc_price": float(open_price),
        "cb_price_to_beat_anchor": float(open_price),
        "cb_signed_bps_raw": float(signed_bps_raw),
        "signed_bps_raw": float(signed_bps_raw),
        "signed_bps_bucket": signed_bps_bucket,
        "tte_bucket": tte_bucket,
        "cb_Volatility_raw": float("nan") if volatility_raw is None else float(volatility_raw),
        "volatility_smoothed_raw": float("nan") if volatility_smoothed_raw is None else float(volatility_smoothed_raw),
        "volatility_bucket": volatility_bucket,
        "local_volatility_1h_bps": (
            float("nan") if local_volatility_1h_bps is None else float(local_volatility_1h_bps)
        ),
        "local_volatility_status": local_volatility_status,
        "local_volatility_age_minutes": local_volatility_age_minutes,
        "up_bid": up_bid,
        "up_ask": up_ask if up_ask is not None else float("nan"),
        "up_mid": up_mid,
        "up_spread": up_spread,
        "up_ask_bucket": up_ask_bucket,
        "up_bid_depth_total": val("up_bid_depth_total"),
        "up_ask_depth_total": val("up_ask_depth_total"),
        "up_best_bid_depth": val("up_best_bid_depth"),
        "up_best_ask_depth": val("up_best_ask_depth"),
        "down_bid": down_bid,
        "down_ask": down_ask if down_ask is not None else float("nan"),
        "down_mid": down_mid,
        "down_spread": down_spread,
        "down_ask_bucket": down_ask_bucket,
        "down_bid_depth_total": val("down_bid_depth_total"),
        "down_ask_depth_total": val("down_ask_depth_total"),
        "down_best_bid_depth": val("down_best_bid_depth"),
        "down_best_ask_depth": val("down_best_ask_depth"),
        "yes_no_spread": yes_no_spread,
    }
