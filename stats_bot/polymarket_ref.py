from __future__ import annotations

import collections
import json
import math
import threading
import time
import bisect
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
POLYMARKET_TWAP_30_SUBSCRIBE = {
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_twap_thirty",
            "type": "update",
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
        self.source_timestamp_utc: datetime | None = None
        self.received_at_utc: datetime | None = None
        self.full_accuracy_value: str | None = None
        self._lock = threading.Lock()
        self._bars: collections.OrderedDict[int, dict[str, Any]] = collections.OrderedDict()
        self._on_update = on_update

    def start(self) -> bool:
        websocket_lib, websocket_available = _load_websocket_lib()
        if not websocket_available:
            return False

        def on_open(ws):
            ws.send(json.dumps(self.subscribe_message, sort_keys=True))

        def on_message(_ws, message):
            try:
                update = _extract_btc_reference_update(message)
                if update is None:
                    return
                event_ts = update["timestamp_utc"] or datetime.now(tz=timezone.utc)
                self.upsert_second_bar(
                    int(event_ts.timestamp()),
                    float(update["price"]),
                    source_timestamp=event_ts,
                    full_accuracy_value=update.get("full_accuracy_value"),
                    received_at=datetime.now(tz=timezone.utc),
                )
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
        full_accuracy_value: str | None = None,
        received_at: datetime | None = None,
    ) -> None:
        received_at = received_at or datetime.now(tz=timezone.utc)
        source_timestamp = source_timestamp or datetime.fromtimestamp(second, tz=timezone.utc)
        with self._lock:
            self.price = price
            self.source_timestamp_utc = source_timestamp
            self.received_at_utc = received_at
            self.full_accuracy_value = full_accuracy_value
            bar = self._bars.get(second)
            if bar is None:
                self._bars[second] = {
                    "timestamp_utc": datetime.fromtimestamp(second, tz=timezone.utc),
                    "source_timestamp_utc": source_timestamp,
                    "received_at_utc": received_at,
                    "open_1s": price,
                    "high_1s": price,
                    "low_1s": price,
                    "close_1s": price,
                    "volume_1s": 0.0,
                    "trade_count_1s": 1.0,
                    "full_accuracy_value": full_accuracy_value or "",
                }
            else:
                bar["high_1s"] = max(float(bar["high_1s"]), price)
                bar["low_1s"] = min(float(bar["low_1s"]), price)
                bar["close_1s"] = price
                bar["trade_count_1s"] = float(bar["trade_count_1s"]) + 1.0
                bar["source_timestamp_utc"] = source_timestamp
                bar["received_at_utc"] = received_at
                if full_accuracy_value:
                    bar["full_accuracy_value"] = full_accuracy_value
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
                        "exchange_ts_utc": source_timestamp,
                    },
                )
            except Exception:
                pass

    def history_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._bars.values())

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._bars:
                return None
            return dict(next(reversed(self._bars.values())))

    def latest_at_or_before(self, target_utc: datetime) -> dict[str, Any] | None:
        target_second = int(_ensure_utc(target_utc).timestamp())
        with self._lock:
            if not self._bars:
                return None
            seconds = list(self._bars.keys())
            idx = bisect.bisect_right(seconds, target_second) - 1
            if idx < 0:
                return None
            return dict(self._bars[seconds[idx]])

    def discrete_ffill_twap(
        self,
        *,
        target_utc: datetime,
        sample_seconds: int = 30,
    ) -> tuple[float | None, int | None]:
        target_second = int(_ensure_utc(target_utc).timestamp())
        with self._lock:
            if not self._bars:
                return None, None
            seconds = list(self._bars.keys())
            close_by_second = {
                second: float(row["close_1s"])
                for second, row in self._bars.items()
                if row.get("close_1s") is not None and float(row["close_1s"]) > 0.0
            }
        samples: list[float] = []
        max_staleness = 0
        for sample_second in range(target_second - int(sample_seconds), target_second):
            idx = bisect.bisect_right(seconds, sample_second) - 1
            if idx < 0:
                return None, None
            source_second = seconds[idx]
            value = close_by_second.get(source_second)
            if value is None:
                return None, None
            samples.append(value)
            max_staleness = max(max_staleness, sample_second - source_second)
        if len(samples) != int(sample_seconds):
            return None, None
        return float(sum(samples) / len(samples)), int(max_staleness)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _extract_btc_reference_price(raw_message: Any) -> tuple[float | None, datetime | None]:
    update = _extract_btc_reference_update(raw_message)
    if update is None:
        return None, None
    return float(update["price"]), update["timestamp_utc"]


def _extract_btc_reference_update(raw_message: Any) -> dict[str, Any] | None:
    raw_text = str(raw_message or "").strip()
    if raw_text == "":
        return None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None

    best_price = None
    best_ts = None
    best_full_accuracy_value = None
    best_topic = payload.get("topic") if isinstance(payload, dict) else None
    best_window_s = None
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
            raw_full_accuracy = row.get("full_accuracy_value")
            best_full_accuracy_value = str(raw_full_accuracy) if raw_full_accuracy is not None else None
            if row.get("topic") is not None:
                best_topic = row.get("topic")
            best_window_s = row.get("window_s") or row.get("windowSeconds") or row.get("window_seconds")
    if best_price is None:
        return None
    return {
        "price": float(best_price),
        "timestamp_utc": best_ts,
        "full_accuracy_value": best_full_accuracy_value,
        "topic": best_topic,
        "window_s": best_window_s,
    }


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
