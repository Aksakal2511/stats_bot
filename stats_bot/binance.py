from __future__ import annotations

import collections
import json
import math
import threading
import time
from datetime import datetime, timezone


_websocket_lib = None
WEBSOCKET_AVAILABLE = None


def _load_websocket_lib():
    global _websocket_lib, WEBSOCKET_AVAILABLE
    if WEBSOCKET_AVAILABLE is not None:
        return _websocket_lib, bool(WEBSOCKET_AVAILABLE)
    try:
        import websocket as websocket_lib

        _websocket_lib = websocket_lib
        WEBSOCKET_AVAILABLE = True
    except Exception:
        _websocket_lib = None
        WEBSOCKET_AVAILABLE = False
    return _websocket_lib, bool(WEBSOCKET_AVAILABLE)


class BinanceTickerFeed:
    def __init__(self, *, name: str, url: str, max_seconds: int = 7200, on_update=None):
        self.name = name
        self.url = url
        self.max_seconds = max_seconds
        self.price: float | None = None
        self._lock = threading.Lock()
        self._bars: collections.OrderedDict[int, dict[str, float | datetime]] = collections.OrderedDict()
        self._on_update = on_update

    def start(self) -> bool:
        websocket_lib, websocket_available = _load_websocket_lib()
        if not websocket_available:
            return False

        def on_message(_ws, message):
            try:
                data = json.loads(message)
                raw_price = data.get("p") or data.get("c")
                if raw_price is None:
                    return
                price = float(raw_price)
                qty = float(data.get("q") or data.get("Q") or 0.0)
                buyer_is_maker = data.get("m")
                if isinstance(buyer_is_maker, str):
                    buyer_is_maker = buyer_is_maker.strip().lower() == "true"
                signed_qty = None
                if isinstance(buyer_is_maker, bool):
                    signed_qty = -qty if buyer_is_maker else qty
                event_ms = data.get("E") or data.get("T")
                ts = (
                    datetime.fromtimestamp(float(event_ms) / 1000.0, tz=timezone.utc)
                    if event_ms is not None
                    else datetime.now(tz=timezone.utc)
                )
                self.upsert_second_bar(int(ts.timestamp()), price, qty, signed_size=signed_qty, source_timestamp=ts)
            except Exception:
                return

        def run_forever():
            while True:
                try:
                    ws = websocket_lib.WebSocketApp(self.url, on_message=on_message)
                    ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception:
                    pass
                time.sleep(2)

        threading.Thread(target=run_forever, name=f"stats-bot-{self.name}", daemon=True).start()
        return True

    def upsert_second_bar(
        self,
        second: int,
        price: float,
        size: float,
        *,
        signed_size: float | None = None,
        source_timestamp: datetime | None = None,
    ) -> None:
        signed = 0.0
        buy_size = 0.0
        sell_size = 0.0
        if signed_size is not None and math.isfinite(float(signed_size)):
            signed = float(signed_size)
            if signed > 0:
                buy_size = signed
            elif signed < 0:
                sell_size = -signed
        with self._lock:
            self.price = price
            bar = self._bars.get(second)
            if bar is None:
                self._bars[second] = {
                    "timestamp_utc": datetime.fromtimestamp(second, tz=timezone.utc),
                    "open_1s": price,
                    "high_1s": price,
                    "low_1s": price,
                    "close_1s": price,
                    "volume_1s": max(0.0, size),
                    "buy_volume_1s": buy_size,
                    "sell_volume_1s": sell_size,
                    "signed_volume_1s": signed,
                    "trade_count_1s": 1.0,
                }
            else:
                bar["high_1s"] = max(float(bar["high_1s"]), price)
                bar["low_1s"] = min(float(bar["low_1s"]), price)
                bar["close_1s"] = price
                bar["volume_1s"] = float(bar["volume_1s"]) + max(0.0, size)
                bar["buy_volume_1s"] = float(bar.get("buy_volume_1s") or 0.0) + buy_size
                bar["sell_volume_1s"] = float(bar.get("sell_volume_1s") or 0.0) + sell_size
                bar["signed_volume_1s"] = float(bar.get("signed_volume_1s") or 0.0) + signed
                bar["trade_count_1s"] = float(bar["trade_count_1s"]) + 1.0
            while len(self._bars) > self.max_seconds:
                self._bars.popitem(last=False)
        if self._on_update is not None:
            try:
                self._on_update(
                    self.name,
                    datetime.now(tz=timezone.utc),
                    {
                        "second": second,
                        "price": price,
                        "exchange_ts_utc": source_timestamp or datetime.fromtimestamp(second, tz=timezone.utc),
                    },
                )
            except Exception:
                pass

    def history_rows(self) -> list[dict[str, float | datetime]]:
        with self._lock:
            return list(self._bars.values())


def adverse_z(
    rows: list[dict[str, float | datetime]],
    *,
    held_side: str,
    lookback_seconds: int,
    rv_seconds: int = 3600,
    min_returns: int = 600,
) -> tuple[float | None, float | None, float | None]:
    clean: list[tuple[int, float]] = []
    for row in rows:
        ts = row.get("timestamp_utc")
        price = row.get("close_1s")
        if not isinstance(ts, datetime):
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        if price_f > 0 and math.isfinite(price_f):
            clean.append((int(ts.timestamp()), price_f))
    if len(clean) <= lookback_seconds + 1:
        return None, None, None
    clean.sort(key=lambda item: item[0])
    latest_ts, latest_price = clean[-1]
    target_ts = latest_ts - int(lookback_seconds)
    prior_price = None
    for ts, price in clean:
        if ts <= target_ts:
            prior_price = price
        else:
            break
    if prior_price is None or prior_price <= 0:
        return None, None, None
    ret_bps = (latest_price / prior_price - 1.0) * 10_000.0
    recent = [(ts, price) for ts, price in clean if ts >= latest_ts - int(rv_seconds)]
    if len(recent) < min_returns:
        return ret_bps, None, None
    sumsq = 0.0
    count = 0
    for (_, prev), (_, curr) in zip(recent[:-1], recent[1:]):
        if prev > 0 and curr > 0:
            value = math.log(curr / prev)
            sumsq += value * value
            count += 1
    if count < min_returns:
        return ret_bps, None, None
    rv_bps = math.sqrt(sumsq) * 10_000.0
    expected = max(0.05, rv_bps * math.sqrt(max(1, lookback_seconds) / float(rv_seconds)))
    direction = -1.0 if held_side.upper() == "UP" else 1.0
    return ret_bps, direction * ret_bps / expected, rv_bps


def adverse_signed_volume(
    rows: list[dict[str, float | datetime]],
    *,
    held_side: str,
    lookback_seconds: int,
) -> tuple[float | None, float | None, float | None, float | None, int]:
    clean: list[tuple[int, float, float, float]] = []
    for row in rows:
        ts = row.get("timestamp_utc")
        if not isinstance(ts, datetime):
            continue
        signed = row.get("signed_volume_1s")
        buy = row.get("buy_volume_1s")
        sell = row.get("sell_volume_1s")
        try:
            signed_f = float(signed)
            buy_f = float(buy or 0.0)
            sell_f = float(sell or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(signed_f):
            clean.append((int(ts.timestamp()), signed_f, buy_f, sell_f))
    if not clean:
        return None, None, None, None, 0
    clean.sort(key=lambda item: item[0])
    latest_ts = clean[-1][0]
    start_ts = latest_ts - max(1, int(lookback_seconds)) + 1
    window = [item for item in clean if item[0] >= start_ts]
    if not window:
        return None, None, None, None, 0
    signed_total = sum(item[1] for item in window)
    buy_total = sum(item[2] for item in window)
    sell_total = sum(item[3] for item in window)
    adverse = -signed_total if held_side.upper() == "UP" else signed_total
    return max(0.0, adverse), signed_total, buy_total, sell_total, len(window)
