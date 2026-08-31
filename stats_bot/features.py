from __future__ import annotations

import math
import bisect
from datetime import datetime, timedelta, timezone
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
TWAP_SAMPLE_SECONDS = 30
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


def _discrete_ffill_twap(
    rows_by_ts: dict[datetime, dict[str, float]],
    *,
    target_utc: datetime,
    sample_seconds: int = TWAP_SAMPLE_SECONDS,
) -> tuple[float | None, int | None]:
    """Coinbase 1s ffilled TWAP over [target-sample_seconds, target)."""
    target_utc = _ensure_utc(target_utc)
    target_second = int(target_utc.timestamp())
    close_by_second: dict[int, float] = {}
    for ts, row in rows_by_ts.items():
        close = finite_float(row.get("close_1s"))
        if close is not None and close > 0.0:
            close_by_second[int(_ensure_utc(ts).timestamp())] = close
    if not close_by_second:
        return None, None
    seconds = sorted(close_by_second)
    samples: list[float] = []
    max_staleness = 0
    for sample_second in range(target_second - int(sample_seconds), target_second):
        idx = bisect.bisect_right(seconds, sample_second) - 1
        if idx < 0:
            return None, None
        source_second = seconds[idx]
        samples.append(close_by_second[source_second])
        max_staleness = max(max_staleness, sample_second - source_second)
    if len(samples) != int(sample_seconds):
        return None, None
    return float(sum(samples) / len(samples)), int(max_staleness)


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


def _analytic_twap_pre_z(
    *,
    spot_distance_bps: float | None,
    local_volatility_1h_bps: float | None,
    tte_seconds: float | None,
    window_seconds: int = 30,
) -> tuple[float | None, float | None]:
    """Pre-settlement TWAP z using variance shortened by 2w/3."""
    if (
        spot_distance_bps is None
        or local_volatility_1h_bps is None
        or tte_seconds is None
        or not math.isfinite(float(spot_distance_bps))
        or not math.isfinite(float(local_volatility_1h_bps))
        or not math.isfinite(float(tte_seconds))
        or local_volatility_1h_bps <= 0.0
    ):
        return None, None
    effective_tte = max(1.0, float(tte_seconds) - (2.0 * float(window_seconds) / 3.0))
    denom = max(0.05, float(local_volatility_1h_bps) * math.sqrt(effective_tte / 3600.0))
    return float(float(spot_distance_bps) / denom), float(effective_tte)


def _normal_cdf(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return float(0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0))))


def compute_twap_provider_diagnostics(
    *,
    provider_history: Any,
    now_utc: datetime,
    market_start_utc: datetime,
    market_end_utc: datetime,
    local_volatility_1h_bps: float | None,
    window_seconds: int = 60,
) -> dict[str, Any]:
    rows = _coerce_rows(provider_history)
    rows_by_ts = _prepare_history(rows, BTC_FIELDS)
    now_utc = _ensure_utc(now_utc)
    market_start_utc = _ensure_utc(market_start_utc)
    market_end_utc = _ensure_utc(market_end_utc)

    latest_ts: datetime | None = None
    latest_close: float | None = None
    for row in rows:
        ts = _coerce_timestamp(row.get("timestamp_utc"))
        close = finite_float(row.get("close_1s"))
        if ts is None or close is None or close <= 0.0 or ts > now_utc:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_close = close

    twap_now, twap_now_staleness = _discrete_ffill_twap(
        rows_by_ts=rows_by_ts,
        target_utc=now_utc,
        sample_seconds=window_seconds,
    )
    twap_open, twap_open_staleness = _discrete_ffill_twap(
        rows_by_ts=rows_by_ts,
        target_utc=market_start_utc,
        sample_seconds=window_seconds,
    )
    signed_bps = (
        None
        if twap_now is None or twap_open is None or twap_open <= 0.0
        else 10000.0 * (float(twap_now) - float(twap_open)) / float(twap_open)
    )
    tte_seconds = max(0.0, (market_end_utc - now_utc).total_seconds())
    analytic_z, effective_tte = _analytic_twap_pre_z(
        spot_distance_bps=signed_bps,
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=tte_seconds,
        window_seconds=window_seconds,
    )
    return {
        "spot_price": latest_close,
        "spot_observed_at_utc": latest_ts,
        f"twap_{window_seconds}s_discrete_1s": twap_now,
        f"twap_open_{window_seconds}s_discrete_1s": twap_open,
        f"twap_{window_seconds}s_signed_bps_raw": signed_bps,
        "twap_max_sample_staleness_seconds": max(
            [value for value in (twap_now_staleness, twap_open_staleness) if value is not None],
            default=None,
        ),
        f"analytic_twap_stream_pre_effective_tte_w{window_seconds}s": effective_tte,
        f"analytic_twap_stream_pre_z_w{window_seconds}s": analytic_z,
        f"analytic_twap_stream_pre_fair_up_w{window_seconds}s": _normal_cdf(analytic_z),
        f"analytic_twap_stream_pre_fair_down_w{window_seconds}s": (
            None if analytic_z is None else _normal_cdf(-float(analytic_z))
        ),
        f"up_analytic_twap_stream_pre_z_w{window_seconds}s": analytic_z,
        f"down_analytic_twap_stream_pre_z_w{window_seconds}s": None if analytic_z is None else -float(analytic_z),
    }


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
    signal_price_mode: str = "spot",
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
    latest_spot_close = finite_float(latest.get("close_1s"))
    if latest_spot_close is None:
        return None
    btc_mom_20s_bps = _momentum_bps(
        merged_rows,
        latest_ts=latest_ts,
        latest_close=latest_spot_close,
        seconds=BTC_MOMENTUM_GUARD_WINDOW_SECONDS,
    )

    volatility_raw, volatility_smoothed_raw = _latest_volatility(close_values, volatility_edges.smoothing_seconds)
    day_type, session_bucket, london_ts = classify_day_type_and_session(now_utc)
    tte_seconds = max(0.0, (market_end_utc - now_utc).total_seconds())
    elapsed_seconds = max(0.0, (now_utc - market_start_utc).total_seconds())
    spot_signed_bps_raw = 10000.0 * (latest_spot_close - open_price) / open_price
    analytic_w30_z: float | None = None
    analytic_w30_effective_tte: float | None = None
    analytic_w30_fair_up: float | None = None
    analytic_w60_z: float | None = None
    analytic_w60_effective_tte: float | None = None
    analytic_w60_fair_up: float | None = None
    analytic_w60_stream_z: float | None = None
    analytic_w60_stream_effective_tte: float | None = None
    analytic_w60_stream_fair_up: float | None = None
    spot_distance_z, spot_distance_z_delta_2s = _distance_z_delta(
        merged_rows,
        now_utc=now_utc,
        market_end_utc=market_end_utc,
        latest_close=latest_spot_close,
        anchor=float(open_price),
        local_volatility_1h_bps=local_volatility_1h_bps,
        current_tte_seconds=float(tte_seconds),
        seconds=MOVE_DZ_MOMENTUM_SECONDS,
    )
    normalized_signal_mode = str(signal_price_mode or "spot").strip().lower()
    signal_close = latest_spot_close
    signal_anchor = float(open_price)
    signal_max_staleness_seconds: int | None = None
    twap_now: float | None = None
    twap_open: float | None = None
    twap_prior: float | None = None
    twap_60_now: float | None = None
    twap_60_open: float | None = None
    twap_60_now_staleness: int | None = None
    twap_60_open_staleness: int | None = None
    twap_now_staleness: int | None = None
    twap_open_staleness: int | None = None
    is_discrete_twap_signal = normalized_signal_mode in {
        "twap_30s",
        "twap_30s_discrete_1s",
        "coinbase_twap_30s_discrete_1s",
    }
    is_analytic_w30_signal = normalized_signal_mode in {
        "analytic_twap_pre_w30s",
        "coinbase_analytic_twap_pre_w30s",
        "coinbase_twap_analytic_pre_w30s",
    }
    is_analytic_w60_signal = normalized_signal_mode in {
        "analytic_twap_pre_w60s",
        "coinbase_analytic_twap_pre_w60s",
        "coinbase_twap_analytic_pre_w60s",
    }
    is_analytic_w60_stream_signal = normalized_signal_mode in {
        "analytic_twap_stream_pre_w60s",
        "coinbase_analytic_twap_stream_pre_w60s",
        "coinbase_twap_stream_analytic_pre_w60s",
    }
    is_spot_vs_twap60_open_signal = normalized_signal_mode in {
        "spot_vs_twap60_open",
        "coinbase_spot_vs_twap60_open",
        "coinbase_spot_vs_twap60_open_spot_z",
    }
    needs_twap_context = (
        is_discrete_twap_signal
        or is_analytic_w30_signal
        or is_analytic_w60_signal
        or is_analytic_w60_stream_signal
        or is_spot_vs_twap60_open_signal
    )
    if needs_twap_context:
        twap_now, twap_now_staleness = _discrete_ffill_twap(rows_by_ts=btc_rows, target_utc=now_utc)
        twap_open, twap_open_staleness = _discrete_ffill_twap(rows_by_ts=btc_rows, target_utc=market_start_utc)
        twap_60_now, twap_60_now_staleness = _discrete_ffill_twap(
            rows_by_ts=btc_rows,
            target_utc=now_utc,
            sample_seconds=60,
        )
        twap_60_open, twap_60_open_staleness = _discrete_ffill_twap(
            rows_by_ts=btc_rows,
            target_utc=market_start_utc,
            sample_seconds=60,
        )
        prior_dt = now_utc - timedelta(seconds=MOVE_DZ_MOMENTUM_SECONDS)
        twap_prior, _ = _discrete_ffill_twap(rows_by_ts=btc_rows, target_utc=prior_dt)
        if is_discrete_twap_signal:
            normalized_signal_mode = "coinbase_twap_30s_discrete_1s"
            if twap_now is None or twap_open is None:
                return None
            signal_close = twap_now
            signal_anchor = twap_open
            signal_max_staleness_seconds = max(
                value for value in (twap_now_staleness, twap_open_staleness) if value is not None
            )
        elif is_spot_vs_twap60_open_signal:
            normalized_signal_mode = "coinbase_spot_vs_twap60_open_spot_z"
            if twap_60_open is None or twap_60_open <= 0.0:
                return None
            signal_close = latest_spot_close
            signal_anchor = float(twap_60_open)
            signal_max_staleness_seconds = twap_60_open_staleness
        else:
            normalized_signal_mode = (
                "coinbase_analytic_twap_stream_pre_w60s"
                if is_analytic_w60_stream_signal
                else "coinbase_analytic_twap_pre_w60s"
                if is_analytic_w60_signal
                else "coinbase_analytic_twap_pre_w30s"
            )
            if (is_analytic_w60_signal or is_analytic_w60_stream_signal) and twap_60_open is not None and twap_60_open > 0.0:
                signal_anchor = float(twap_60_open)
            elif is_analytic_w30_signal and twap_open is not None and twap_open > 0.0:
                signal_anchor = float(twap_open)
            if is_analytic_w60_stream_signal:
                if twap_60_now is None or twap_60_open is None or twap_60_open <= 0.0:
                    return None
                signal_close = float(twap_60_now)
                signal_anchor = float(twap_60_open)
            staleness_values = (
                (twap_60_now_staleness, twap_60_open_staleness)
                if is_analytic_w60_signal or is_analytic_w60_stream_signal
                else (twap_now_staleness, twap_open_staleness)
            )
            signal_max_staleness_seconds = max(
                (value for value in staleness_values if value is not None),
                default=None,
            )
    else:
        normalized_signal_mode = "spot"
    analytic_w30_anchor = float(twap_open) if twap_open is not None and twap_open > 0.0 else float(open_price)
    analytic_w60_anchor = (
        float(twap_60_open) if twap_60_open is not None and twap_60_open > 0.0 else float(open_price)
    )
    analytic_w30_distance_bps = 10000.0 * (latest_spot_close - analytic_w30_anchor) / analytic_w30_anchor
    analytic_w60_distance_bps = 10000.0 * (latest_spot_close - analytic_w60_anchor) / analytic_w60_anchor
    raw_w60_move_dz: float | None = None
    if analytic_w60_anchor > 0.0 and local_volatility_1h_bps is not None and local_volatility_1h_bps > 0.0:
        raw_w60_denom = max(0.05, float(local_volatility_1h_bps) * math.sqrt(max(1.0, float(tte_seconds)) / 3600.0))
        raw_w60_move_dz = float(analytic_w60_distance_bps / raw_w60_denom)
    analytic_w60_stream_distance_bps = (
        None
        if twap_60_now is None or twap_60_open is None or twap_60_open <= 0.0
        else 10000.0 * (float(twap_60_now) - float(twap_60_open)) / float(twap_60_open)
    )
    analytic_w30_z, analytic_w30_effective_tte = _analytic_twap_pre_z(
        spot_distance_bps=float(analytic_w30_distance_bps),
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=float(tte_seconds),
        window_seconds=30,
    )
    analytic_w30_fair_up = _normal_cdf(analytic_w30_z)
    analytic_w60_z, analytic_w60_effective_tte = _analytic_twap_pre_z(
        spot_distance_bps=float(analytic_w60_distance_bps),
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=float(tte_seconds),
        window_seconds=60,
    )
    analytic_w60_fair_up = _normal_cdf(analytic_w60_z)
    analytic_w60_stream_z, analytic_w60_stream_effective_tte = _analytic_twap_pre_z(
        spot_distance_bps=analytic_w60_stream_distance_bps,
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=float(tte_seconds),
        window_seconds=60,
    )
    analytic_w60_stream_fair_up = _normal_cdf(analytic_w60_stream_z)
    signed_bps_raw = 10000.0 * (signal_close - signal_anchor) / signal_anchor
    distance_z = _distance_to_beat_z(
        close=signal_close,
        anchor=signal_anchor,
        local_volatility_1h_bps=local_volatility_1h_bps,
        tte_seconds=tte_seconds,
    )
    distance_z_delta_2s: float | None
    if normalized_signal_mode == "coinbase_twap_30s_discrete_1s":
        prior_dt = now_utc - timedelta(seconds=MOVE_DZ_MOMENTUM_SECONDS)
        prior_tte = max(0.0, (market_end_utc - prior_dt).total_seconds())
        prior_z = _distance_to_beat_z(
            close=twap_prior,
            anchor=signal_anchor,
            local_volatility_1h_bps=local_volatility_1h_bps,
            tte_seconds=prior_tte,
        )
        distance_z_delta_2s = (
            float(distance_z - prior_z)
            if distance_z is not None and prior_z is not None
            else None
        )
    else:
        distance_z_delta_2s = spot_distance_z_delta_2s
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
        "cb_close_1s": latest_spot_close,
        "cb_signal_price": signal_close,
        "cb_signal_price_mode": normalized_signal_mode,
        "cb_spot_signed_bps_raw": float(spot_signed_bps_raw),
        "cb_spot_distance_to_beat_z": float("nan") if spot_distance_z is None else float(spot_distance_z),
        "raw_move_dz": float("nan") if raw_w60_move_dz is None else float(raw_w60_move_dz),
        "up_raw_move_dz": float("nan") if raw_w60_move_dz is None else float(raw_w60_move_dz),
        "down_raw_move_dz": float("nan") if raw_w60_move_dz is None else float(-raw_w60_move_dz),
        "cb_spot_distance_z_delta_2s": (
            float("nan") if spot_distance_z_delta_2s is None else float(spot_distance_z_delta_2s)
        ),
        "analytic_twap_pre_effective_tte_w30s": (
            float("nan") if analytic_w30_effective_tte is None else float(analytic_w30_effective_tte)
        ),
        "analytic_twap_pre_z_w30s": float("nan") if analytic_w30_z is None else float(analytic_w30_z),
        "analytic_twap_pre_fair_up_w30s": (
            float("nan") if analytic_w30_fair_up is None else float(analytic_w30_fair_up)
        ),
        "analytic_twap_pre_fair_down_w30s": (
            float("nan") if analytic_w30_fair_up is None else float(1.0 - analytic_w30_fair_up)
        ),
        "up_analytic_twap_pre_z_w30s": float("nan") if analytic_w30_z is None else float(analytic_w30_z),
        "down_analytic_twap_pre_z_w30s": float("nan") if analytic_w30_z is None else float(-analytic_w30_z),
        "up_analytic_twap_pre_fair_w30s": (
            float("nan") if analytic_w30_fair_up is None else float(analytic_w30_fair_up)
        ),
        "down_analytic_twap_pre_fair_w30s": (
            float("nan") if analytic_w30_fair_up is None else float(1.0 - analytic_w30_fair_up)
        ),
        "analytic_twap_pre_effective_tte_w60s": (
            float("nan") if analytic_w60_effective_tte is None else float(analytic_w60_effective_tte)
        ),
        "analytic_twap_pre_z_w60s": float("nan") if analytic_w60_z is None else float(analytic_w60_z),
        "analytic_twap_pre_fair_up_w60s": (
            float("nan") if analytic_w60_fair_up is None else float(analytic_w60_fair_up)
        ),
        "analytic_twap_pre_fair_down_w60s": (
            float("nan") if analytic_w60_fair_up is None else float(1.0 - analytic_w60_fair_up)
        ),
        "up_analytic_twap_pre_z_w60s": float("nan") if analytic_w60_z is None else float(analytic_w60_z),
        "down_analytic_twap_pre_z_w60s": float("nan") if analytic_w60_z is None else float(-analytic_w60_z),
        "up_analytic_twap_pre_fair_w60s": (
            float("nan") if analytic_w60_fair_up is None else float(analytic_w60_fair_up)
        ),
        "down_analytic_twap_pre_fair_w60s": (
            float("nan") if analytic_w60_fair_up is None else float(1.0 - analytic_w60_fair_up)
        ),
        "analytic_twap_stream_pre_effective_tte_w60s": (
            float("nan") if analytic_w60_stream_effective_tte is None else float(analytic_w60_stream_effective_tte)
        ),
        "analytic_twap_stream_pre_z_w60s": (
            float("nan") if analytic_w60_stream_z is None else float(analytic_w60_stream_z)
        ),
        "analytic_twap_stream_pre_fair_up_w60s": (
            float("nan") if analytic_w60_stream_fair_up is None else float(analytic_w60_stream_fair_up)
        ),
        "analytic_twap_stream_pre_fair_down_w60s": (
            float("nan") if analytic_w60_stream_fair_up is None else float(1.0 - analytic_w60_stream_fair_up)
        ),
        "up_analytic_twap_stream_pre_z_w60s": (
            float("nan") if analytic_w60_stream_z is None else float(analytic_w60_stream_z)
        ),
        "down_analytic_twap_stream_pre_z_w60s": (
            float("nan") if analytic_w60_stream_z is None else float(-analytic_w60_stream_z)
        ),
        "up_analytic_twap_stream_pre_fair_w60s": (
            float("nan") if analytic_w60_stream_fair_up is None else float(analytic_w60_stream_fair_up)
        ),
        "down_analytic_twap_stream_pre_fair_w60s": (
            float("nan") if analytic_w60_stream_fair_up is None else float(1.0 - analytic_w60_stream_fair_up)
        ),
        "analytic_twap_pre_method": "spot_bps_over_rv_sqrt_tte_minus_2w_over_3",
        "analytic_twap_stream_pre_method": "twap_stream_bps_over_rv_sqrt_tte_minus_2w_over_3",
        "cb_twap_30s_discrete_1s": float("nan") if twap_now is None else float(twap_now),
        "cb_twap_open_30s_discrete_1s": float("nan") if twap_open is None else float(twap_open),
        "cb_twap_60s_discrete_1s": float("nan") if twap_60_now is None else float(twap_60_now),
        "cb_twap_open_60s_discrete_1s": float("nan") if twap_60_open is None else float(twap_60_open),
        "cb_twap_signed_bps_raw": (
            float("nan") if twap_now is None or twap_open is None else 10000.0 * (twap_now - twap_open) / twap_open
        ),
        "cb_twap_60s_signed_bps_raw": (
            float("nan")
            if twap_60_now is None or twap_60_open is None
            else 10000.0 * (twap_60_now - twap_60_open) / twap_60_open
        ),
        "cb_twap_max_sample_staleness_seconds": (
            float("nan") if signal_max_staleness_seconds is None else float(signal_max_staleness_seconds)
        ),
        "btc_mom_20s_bps": float("nan") if btc_mom_20s_bps is None else float(btc_mom_20s_bps),
        "distance_to_beat_z": float("nan") if distance_z is None else float(distance_z),
        "distance_z_delta_2s": float("nan") if distance_z_delta_2s is None else float(distance_z_delta_2s),
        "up_side_dz_delta_2s": float("nan") if distance_z_delta_2s is None else float(distance_z_delta_2s),
        "down_side_dz_delta_2s": float("nan") if distance_z_delta_2s is None else float(-distance_z_delta_2s),
        "window_open_btc_price": float(open_price),
        "cb_price_to_beat_anchor": float(signal_anchor),
        "cb_spot_price_to_beat_anchor": float(open_price),
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
