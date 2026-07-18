from __future__ import annotations

import collections
import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any


POLYMARKET_REF_WS = "wss://ws-live-data.polymarket.com/"
POLYMARKET_REF_SUBSCRIBE = {
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": "{\"symbol\":\"btc/usd\"}",
        }
    ],
}

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


class PolymarketReferenceFeed:
    def __init__(
        self,
        *,
        name: str = "pm_chainlink",
        url: str = POLYMARKET_REF_WS,
        subscribe_message: dict[str, Any] | None = None,
        max_seconds: int = 1800,
        on_update=None,
    ):
        self.name = name
        self.url = url
        self.subscribe_message = dict(subscribe_message or POLYMARKET_REF_SUBSCRIBE)
        self.max_seconds = max_seconds
        self.price: float | None = None
        self._lock = threading.Lock()
        self._bars: collections.OrderedDict[int, dict[str, float | datetime]] = collections.OrderedDict()
        self._on_update = on_update

    def start(self) -> bool:
        websocket_lib, websocket_available = _load_websocket_lib()
        if not websocket_available:
            return False

        def on_open(ws):
            ws.send(json.dumps(self.subscribe_message, sort_keys=True))

        def on_message(_ws, message):
            try:
                price, ts = _extract_btc_reference_price(message)
                if price is None:
                    return
                event_ts = ts or datetime.now(tz=timezone.utc)
                self.upsert_second_bar(int(event_ts.timestamp()), price, source_timestamp=event_ts)
            except Exception:
                return

        def run_forever():
            while True:
                try:
                    ws = websocket_lib.WebSocketApp(self.url, on_open=on_open, on_message=on_message)
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
                    "volume_1s": 0.0,
                    "trade_count_1s": 1.0,
                }
            else:
                bar["high_1s"] = max(float(bar["high_1s"]), price)
                bar["low_1s"] = min(float(bar["low_1s"]), price)
                bar["close_1s"] = price
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


def _extract_btc_reference_price(raw_message: Any) -> tuple[float | None, datetime | None]:
    raw_text = str(raw_message or "").strip()
    if raw_text == "":
        return None, None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, None

    best_price = None
    best_ts = None
    for row in _iter_candidate_rows(payload):
        if not _is_row_for_btc(row):
            continue
        price = _decode_price(
            row.get("value")
            or row.get("price")
            or row.get("last_price")
            or row.get("lastTradePrice")
            or row.get("mark_price")
            or row.get("close")
            or row.get("p")
            or row.get("v")
            or row.get("full_accuracy_value")
        )
        if price is None:
            continue
        ts = _parse_exchange_ts(
            row.get("timestamp")
            or row.get("ts")
            or row.get("t")
            or row.get("time")
            or row.get("updated_at")
            or row.get("validFromTimestamp")
        )
        if best_price is None or (ts is not None and (best_ts is None or ts >= best_ts)):
            best_price = float(price)
            best_ts = ts
    return best_price, best_ts


def _normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    for token in (" ", "-", "_", "/"):
        raw = raw.replace(token, "")
    return raw


def _is_row_for_btc(row: dict[str, Any]) -> bool:
    symbols = []
    for key in ("symbol", "s", "pair", "ticker", "asset", "product_id", "productId", "market", "instrument"):
        value = row.get(key)
        if value is not None:
            symbols.append(_normalize_symbol(value))
    if not symbols:
        return True
    return any(symbol in {"BTC", "BTCUSD", "BTCUSDT"} for symbol in symbols)


def _decode_price(value: Any) -> float | None:
    try:
        if value is None:
            return None
        price = float(value)
        if price > 1e12:
            price /= 1e18
        if price <= 0.0 or not math.isfinite(price):
            return None
        return float(price)
    except Exception:
        return None


def _parse_exchange_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 1e16:
            raw = raw / 1e9
        elif raw > 1e12:
            raw = raw / 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _iter_candidate_rows(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    queue: deque[Any] = deque([payload])
    visited = 0
    while queue and visited < 128:
        current = queue.popleft()
        visited += 1
        if isinstance(current, dict):
            rows.append(current)
            for key in ("payload", "data", "result", "message"):
                nested = current.get(key)
                if isinstance(nested, dict):
                    queue.append(nested)
                elif isinstance(nested, list):
                    queue.extend(v for v in nested if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            queue.extend(v for v in current if isinstance(v, (dict, list)))
    return rows
