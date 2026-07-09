from __future__ import annotations

import collections
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

COINBASE_FEED = "wss://ws-feed.exchange.coinbase.com"
COINBASE_SPOT = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
_websocket_lib = None
WEBSOCKET_AVAILABLE = None
_requests_lib = None
_requests_session = None


def _parse_exchange_timestamp(ts_raw: str | None) -> datetime:
    if not ts_raw:
        return datetime.now(tz=timezone.utc)
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(tz=timezone.utc)


def _requests():
    global _requests_lib
    if _requests_lib is None:
        import requests

        _requests_lib = requests
    return _requests_lib


def _requests_session_obj():
    global _requests_session
    if _requests_session is None:
        session = _requests().Session()
        session.trust_env = False
        _requests_session = session
    return _requests_session


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


class CoinbaseFeed:
    def __init__(self, *, max_seconds: int = 1800, on_update=None):
        self.max_seconds = max_seconds
        self.price: float | None = None
        self._last_rest_poll = 0.0
        self._rest_poll_inflight = False
        self._lock = threading.Lock()
        self._bars: collections.OrderedDict[int, dict[str, float | datetime]] = collections.OrderedDict()
        self._minute_closes: collections.OrderedDict[int, float] = collections.OrderedDict()
        self._minute_bootstrap_attempted = False
        self._on_update = on_update

    def start(self) -> bool:
        websocket_lib, websocket_available = _load_websocket_lib()
        if not websocket_available:
            return False

        def on_open(ws):
            ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker"],
                    }
                )
            )

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get("type") != "ticker" or data.get("product_id") != "BTC-USD":
                    return
                price = float(data["price"])
                size = float(data.get("last_size") or 0.0)
                ts = _parse_exchange_timestamp(data.get("time"))
                self.upsert_second_bar(int(ts.timestamp()), price, size, source_timestamp=ts)
            except Exception:
                return

        def run_forever():
            while True:
                try:
                    ws = websocket_lib.WebSocketApp(COINBASE_FEED, on_open=on_open, on_message=on_message)
                    ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception:
                    pass
                time.sleep(2)

        threading.Thread(target=run_forever, daemon=True).start()
        return True

    def upsert_second_bar(
        self,
        second: int,
        price: float,
        size: float,
        *,
        source_timestamp: datetime | None = None,
    ) -> None:
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
                    "trade_count_1s": 1.0,
                }
            else:
                bar["high_1s"] = max(float(bar["high_1s"]), price)
                bar["low_1s"] = min(float(bar["low_1s"]), price)
                bar["close_1s"] = price
                bar["volume_1s"] = float(bar["volume_1s"]) + max(0.0, size)
                bar["trade_count_1s"] = float(bar["trade_count_1s"]) + 1.0
            while len(self._bars) > self.max_seconds:
                self._bars.popitem(last=False)
            minute = second - (second % 60)
            self._minute_closes[minute] = price
            while len(self._minute_closes) > 180:
                self._minute_closes.popitem(last=False)
        if self._on_update is not None:
            try:
                self._on_update(
                    "coinbase",
                    datetime.now(tz=timezone.utc),
                    {
                        "second": second,
                        "price": price,
                        "exchange_ts_utc": source_timestamp or datetime.fromtimestamp(second, tz=timezone.utc),
                    },
                )
            except Exception:
                pass

    def bootstrap_minute_history(self, now_utc: datetime | None = None) -> bool:
        """Load completed Coinbase minute closes so local volatility is ready at startup."""
        self._minute_bootstrap_attempted = True
        now_utc = (now_utc or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        end = now_utc.replace(second=0, microsecond=0)
        start = end - timedelta(minutes=100)
        try:
            response = _requests_session_obj().get(
                COINBASE_CANDLES,
                params={"granularity": 60, "start": start.isoformat(), "end": end.isoformat()},
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
            with self._lock:
                for row in sorted(rows, key=lambda item: int(item[0])):
                    minute = int(row[0])
                    if minute < int(end.timestamp()):
                        self._minute_closes[minute] = float(row[4])
                self._minute_closes = collections.OrderedDict(sorted(self._minute_closes.items()))
                while len(self._minute_closes) > 180:
                    self._minute_closes.popitem(last=False)
            return len(rows) >= 45
        except Exception:
            return False

    def local_volatility_1h_bps(self, now_utc: datetime) -> tuple[float | None, str, int | None]:
        """Return causal RV through the previous completed minute, matching research."""
        current_minute = int(now_utc.astimezone(timezone.utc).timestamp()) // 60 * 60
        with self._lock:
            closes = dict(self._minute_closes)
        eligible = {minute: close for minute, close in closes.items() if minute < current_minute}
        if not eligible:
            return None, "LOCAL_VOL_WARMUP", None
        latest = max(eligible)
        age_minutes = max(0, (current_minute - 60 - latest) // 60)
        if age_minutes > 1:
            return None, "LOCAL_VOL_STALE", int(age_minutes)
        start = latest - 60 * 60
        series: list[float] = []
        previous: float | None = None
        for minute in range(start, latest + 1, 60):
            value = eligible.get(minute, previous)
            if value is not None and value > 0.0:
                previous = value
                series.append(float(value))
        if len(series) < 46:
            return None, "LOCAL_VOL_WARMUP", int(age_minutes)
        returns = [math.log(current / prior) for prior, current in zip(series[:-1], series[1:])]
        returns = returns[-60:]
        if len(returns) < 45:
            return None, "LOCAL_VOL_WARMUP", int(age_minutes)
        return math.sqrt(sum(value * value for value in returns)) * 10000.0, "ready", int(age_minutes)

    def poll_rest_if_needed(self, now_ts: float) -> None:
        _, websocket_available = _load_websocket_lib()
        if websocket_available and self.price is not None:
            return
        if now_ts - self._last_rest_poll < 1.0:
            return
        self._last_rest_poll = now_ts

        def _rest_poll() -> None:
            try:
                resp = _requests_session_obj().get(COINBASE_SPOT, timeout=0.75)
                resp.raise_for_status()
                data = resp.json()
                price = float(data["price"])
                self.upsert_second_bar(int(time.time()), price, 0.0)
            except Exception:
                pass
            finally:
                self._rest_poll_inflight = False

        if websocket_available:
            if self._rest_poll_inflight:
                return
            self._rest_poll_inflight = True
            threading.Thread(target=_rest_poll, daemon=True).start()
            return

        _rest_poll()

    def history_rows(self) -> list[dict[str, float | datetime]]:
        with self._lock:
            return list(self._bars.values())

    def window_open_price(self, window_ts: int) -> float | None:
        with self._lock:
            exact = self._bars.get(window_ts)
            if exact is not None:
                return float(exact["open_1s"])
            for second, row in self._bars.items():
                if second >= window_ts:
                    return float(row["open_1s"])
            return None

    def resolution_price_at_or_after(self, end_ts: int) -> float | None:
        with self._lock:
            latest_before_or_at: float | None = None
            first_after_or_at: float | None = None
            for second, row in self._bars.items():
                if second <= end_ts:
                    latest_before_or_at = float(row["close_1s"])
                if first_after_or_at is None and second >= end_ts:
                    first_after_or_at = float(row["open_1s"])
            if latest_before_or_at is not None:
                return latest_before_or_at
            if first_after_or_at is not None:
                return first_after_or_at
            return self.price
