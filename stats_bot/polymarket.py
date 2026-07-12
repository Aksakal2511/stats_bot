from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .strategy import finite_float


POLYMARKET_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
WINDOW_SECONDS = 300
GAMMA_HTTP_TIMEOUT_SECONDS = 2.0
BOOK_HTTP_TIMEOUT_SECONDS = 1.0
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
ORDER_TRANSPORT_KEEPALIVE_SECONDS = 600.0
ORDER_TRANSPORT_WARM_INTERVAL_SECONDS = 3.0
ORDER_TRANSPORT_TIMEOUT_SECONDS = 5.0
ORDER_TRANSPORT_WARM_TIMEOUT_SECONDS = 0.75


@dataclass(frozen=True)
class MarketInfo:
    slug: str
    market_id: str
    condition_id: str
    question: str
    token_up: str
    token_down: str
    market_start_utc: datetime
    market_end_utc: datetime
    tick_size: float = 0.01
    min_order_size: float | None = None
    neg_risk: bool = False
    fees_enabled: bool | None = None
    fee_rate: float | None = None


@dataclass(frozen=True)
class BookSnapshot:
    bid: float | None
    ask: float | None
    mid: float | None
    spread: float | None
    bid_depth_total: float | None
    ask_depth_total: float | None
    best_bid_depth: float | None
    best_ask_depth: float | None
    captured_at_utc: datetime


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    order_id: str | None
    status: str
    message: str = ""
    matched_shares: float = 0.0
    price: float | None = None
    transport_metrics: dict[str, Any] | None = None


@dataclass(frozen=True)
class SignedBuyOrder:
    token_id: str
    price: float
    shares: float
    order: Any


@dataclass(frozen=True)
class MarketResolution:
    outcome: str | None
    source: str
    resolved: bool


class _DedicatedOrderTransport:
    """A private, warm HTTP/2 transport used only for authenticated order POSTs."""

    def __init__(self) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(
            http2=True,
            trust_env=False,
            timeout=httpx.Timeout(ORDER_TRANSPORT_TIMEOUT_SECONDS, connect=1.0),
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=ORDER_TRANSPORT_KEEPALIVE_SECONDS,
            ),
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._metrics_lock = threading.Lock()
        self._last_io_completed_at: float | None = None
        self._last_warm_completed_at: float | None = None
        self._last_warm_ok: bool | None = None
        self._last_post_metrics: dict[str, Any] = {}

    @staticmethod
    def _headers(headers: dict[str, str] | None) -> dict[str, str]:
        result = dict(headers or {})
        result["User-Agent"] = "stats-bot-entry-transport"
        result["Accept"] = "*/*"
        result["Content-Type"] = "application/json"
        return result

    def post(self, endpoint: str, headers=None, data=None, params=None):
        """Match the SDK's `_post` contract without using its shared global client."""
        from py_clob_client_v2.exceptions import PolyApiException

        started = time.perf_counter()
        with self._metrics_lock:
            idle_seconds = (
                max(0.0, started - self._last_io_completed_at)
                if self._last_io_completed_at is not None
                else None
            )
            warm_age_seconds = (
                max(0.0, started - self._last_warm_completed_at)
                if self._last_warm_completed_at is not None
                else None
            )
            warm_ok = self._last_warm_ok
        response = None
        error_type = ""
        try:
            response = self._client.request(
                "POST",
                endpoint,
                headers=self._headers(headers),
                content=data.encode("utf-8") if isinstance(data, str) else data,
                params=params,
            )
        except self._httpx.RequestError as exc:
            error_type = type(exc).__name__
            raise PolyApiException(error_msg=f"Dedicated order transport: {exc}") from exc
        finally:
            completed = time.perf_counter()
            metrics = {
                "wire_elapsed_ms": round((completed - started) * 1000.0, 3),
                "connection_idle_seconds": round(idle_seconds, 3) if idle_seconds is not None else "",
                "warm_probe_age_seconds": round(warm_age_seconds, 3) if warm_age_seconds is not None else "",
                "warm_probe_ok": "" if warm_ok is None else int(warm_ok),
                "http_version": response.http_version if response is not None else "",
                "http_status": response.status_code if response is not None else "",
                "cf_ray": response.headers.get("cf-ray", "") if response is not None else "",
                "server_timing": response.headers.get("server-timing", "") if response is not None else "",
                "transport_error_type": error_type,
            }
            with self._metrics_lock:
                self._last_io_completed_at = completed
                self._last_post_metrics = metrics
        if response.status_code != 200:
            raise PolyApiException(response)
        try:
            return response.json()
        except ValueError:
            return response.text

    def warm(self) -> bool:
        """Keep the actual order connection warm; failures never affect trading state."""
        ok = False
        try:
            response = self._client.get(
                f"{POLYMARKET_HOST}/ok",
                headers={"User-Agent": "stats-bot-entry-transport", "Accept": "*/*"},
                timeout=self._httpx.Timeout(ORDER_TRANSPORT_WARM_TIMEOUT_SECONDS),
            )
            ok = response.status_code == 200
        except self._httpx.RequestError:
            pass
        finally:
            completed = time.perf_counter()
            with self._metrics_lock:
                self._last_io_completed_at = completed
                self._last_warm_completed_at = completed
                self._last_warm_ok = ok
        return ok

    def last_post_metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            return dict(self._last_post_metrics)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        def run() -> None:
            self.warm()
            while not self._stop.wait(ORDER_TRANSPORT_WARM_INTERVAL_SECONDS):
                self.warm()

        self._thread = threading.Thread(target=run, name="stats-bot-order-transport-warm", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._client.close()


def _lazy_import_clob():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.constants import POLYGON
    from py_clob_client_v2.clob_types import (
        ApiCreds,
        MarketOrderArgsV2 as MarketOrderArgs,
        OpenOrderParams,
        OrderArgsV2 as OrderArgs,
        OrderPayload,
        OrderType,
        PartialCreateOrderOptions,
    )

    return ClobClient, POLYGON, ApiCreds, MarketOrderArgs, OpenOrderParams, OrderArgs, OrderPayload, OrderType, PartialCreateOrderOptions


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _boolish(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _extract_order_id(resp: object) -> str | None:
    if isinstance(resp, dict):
        raw = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        return str(raw) if raw else None
    raw = getattr(resp, "orderID", None) or getattr(resp, "orderId", None) or getattr(resp, "id", None)
    return str(raw) if raw else None


def _extract_status(resp: object, default: str = "POSTED") -> str:
    if isinstance(resp, dict):
        raw = resp.get("status") or resp.get("type") or default
        return str(raw).upper()
    raw = getattr(resp, "status", None) or getattr(resp, "type", None) or default
    return str(raw).upper()


def _extract_matched(resp: object) -> float:
    keys = (
        "size_matched",
        "sizeMatched",
        "matched_size",
        "matchedSize",
        "filled_size",
        "filledSize",
        "matched",
    )
    if isinstance(resp, dict):
        for key in keys:
            value = finite_float(resp.get(key))
            if value is not None:
                return max(0.0, value)
        return 0.0
    for key in keys:
        value = finite_float(getattr(resp, key, None))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _extract_avg_price(resp: object, fallback: float) -> float:
    keys = ("avgPrice", "avg_price", "price")
    if isinstance(resp, dict):
        for key in keys:
            value = finite_float(resp.get(key))
            if value is not None:
                return value
        return fallback
    for key in keys:
        value = finite_float(getattr(resp, key, None))
        if value is not None:
            return value
    return fallback


def _tick_text(tick_size: float) -> str:
    if tick_size >= 0.1:
        return "0.1"
    if tick_size >= 0.01:
        return "0.01"
    if tick_size >= 0.001:
        return "0.001"
    return "0.0001"


def _normalise_outcome(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"UP", "YES"}:
        return "UP"
    if text in {"DOWN", "NO"}:
        return "DOWN"
    return None


def round_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        tick_size = 0.01
    ticks = round(price / tick_size)
    decimals = max(0, min(4, len(str(tick_size).split(".")[-1].rstrip("0")) if "." in str(tick_size) else 0))
    return round(max(0.001, min(0.999, ticks * tick_size)), decimals)


class PolymarketClient:
    def __init__(self, *, env_path: Path, live: bool):
        self.live = live
        self.client = None
        self._entry_client = None
        self._entry_transport: _DedicatedOrderTransport | None = None
        self.wallet_address: str | None = None
        self._http = requests.Session()
        self._http.trust_env = False
        self._MarketOrderArgs = None
        self._OpenOrderParams = None
        self._OrderArgs = None
        self._OrderPayload = None
        self._OrderType = None
        self._PartialCreateOrderOptions = None

        self._pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
        self._funder = os.getenv("POLY_FUNDER", "").strip() or None
        self._sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1" if self._funder else "0"))
        self._api_key = os.getenv("POLY_API_KEY", "").strip()
        self._api_secret = os.getenv("POLY_API_SECRET", "").strip()
        self._api_pass = os.getenv("POLY_API_PASSPHRASE", "").strip()
        self._client_init_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stream_generation = 0
        self._stream_books: dict[str, BookSnapshot] = {}
        self._market_ws = None
        self._user_ws = None
        self._stream_callback = None
        self._market_stream_connected = False
        self._user_stream_connected = False
        if live and not self._pk:
            raise SystemExit("POLY_PRIVATE_KEY is required for live mode.")
        if self._pk and not self._pk.startswith("0x"):
            self._pk = "0x" + self._pk

    def _emit_stream_event(self, event_type: str, received_at: datetime, fields: dict[str, Any]) -> None:
        callback = self._stream_callback
        if callback is None:
            return
        try:
            callback(event_type, received_at, fields)
        except Exception:
            pass

    @staticmethod
    def _stream_timestamp(value: object, fallback: datetime) -> datetime:
        try:
            raw = float(value)
            if raw > 10_000_000_000:
                raw /= 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except Exception:
            return fallback

    @staticmethod
    def _stream_level(entry: object) -> tuple[float, float] | None:
        try:
            if isinstance(entry, dict):
                return float(entry["price"]), max(0.0, float(entry.get("size") or 0.0))
            return float(entry.price), max(0.0, float(entry.size))
        except Exception:
            return None

    def _store_stream_book(
        self,
        token_id: str,
        *,
        bid: float | None,
        ask: float | None,
        captured_at: datetime,
        bids: list[object] | None = None,
        asks: list[object] | None = None,
    ) -> bool:
        bid_levels = [item for raw in (bids or []) if (item := self._stream_level(raw)) is not None]
        ask_levels = [item for raw in (asks or []) if (item := self._stream_level(raw)) is not None]
        if bid is None and bid_levels:
            bid = max(price for price, _ in bid_levels)
        if ask is None and ask_levels:
            ask = min(price for price, _ in ask_levels)
        with self._stream_lock:
            previous = self._stream_books.get(token_id)
            if bid is None:
                bid = previous.bid if previous else None
            if ask is None:
                ask = previous.ask if previous else None
            if bid is None and ask is None:
                return False
            bid_total = sum(size for _, size in bid_levels) if bid_levels else None
            ask_total = sum(size for _, size in ask_levels) if ask_levels else None
            best_bid_depth = sum(size for price, size in bid_levels if bid is not None and math.isclose(price, bid, abs_tol=1e-9)) if bid_levels else None
            best_ask_depth = sum(size for price, size in ask_levels if ask is not None and math.isclose(price, ask, abs_tol=1e-9)) if ask_levels else None
            mid = (bid + ask) / 2.0 if bid is not None and ask is not None else (bid if bid is not None else ask)
            spread = max(0.0, ask - bid) if bid is not None and ask is not None else None
            self._stream_books[token_id] = BookSnapshot(
                bid=bid,
                ask=ask,
                mid=mid,
                spread=spread,
                bid_depth_total=bid_total if bid_total is not None else (previous.bid_depth_total if previous else None),
                ask_depth_total=ask_total if ask_total is not None else (previous.ask_depth_total if previous else None),
                best_bid_depth=best_bid_depth if best_bid_depth is not None else (previous.best_bid_depth if previous else None),
                best_ask_depth=best_ask_depth if best_ask_depth is not None else (previous.best_ask_depth if previous else None),
                captured_at_utc=captured_at,
            )
            return previous is None or previous.bid != bid or previous.ask != ask

    def stream_book_snapshot(self, token_id: str, *, max_age_seconds: float = 2.0) -> BookSnapshot | None:
        with self._stream_lock:
            snapshot = self._stream_books.get(str(token_id))
        if snapshot is None:
            return None
        if (datetime.now(tz=timezone.utc) - snapshot.captured_at_utc).total_seconds() > max_age_seconds:
            return None
        return snapshot

    def stream_status(self) -> tuple[bool, bool]:
        with self._stream_lock:
            return self._market_stream_connected, self._user_stream_connected

    def stop_streams(self) -> None:
        with self._stream_lock:
            self._stream_generation += 1
            sockets = (self._market_ws, self._user_ws)
            self._market_ws = None
            self._user_ws = None
            self._market_stream_connected = False
            self._user_stream_connected = False
        for socket in sockets:
            try:
                if socket is not None:
                    socket.close()
            except Exception:
                pass
        if self._entry_transport is not None:
            self._entry_transport.close()
            self._entry_transport = None

    def start_streams(self, market: MarketInfo, callback) -> None:
        """Start fresh market/user subscriptions for the active five-minute market."""
        try:
            import websocket
        except Exception:
            return
        with self._stream_lock:
            self._stream_generation += 1
            generation = self._stream_generation
            self._stream_callback = callback
            self._stream_books = {}
            previous_market = self._market_ws
            previous_user = self._user_ws
            self._market_stream_connected = False
            self._user_stream_connected = False
        for previous in (previous_market, previous_user):
            try:
                if previous is not None:
                    previous.close()
            except Exception:
                pass

        token_ids = [str(market.token_up), str(market.token_down)]

        def market_open(ws):
            with self._stream_lock:
                if generation != self._stream_generation:
                    return
                self._market_stream_connected = True
            ws.send(json.dumps({"assets_ids": token_ids, "type": "market", "custom_feature_enabled": True}))
            self._emit_stream_event("market_ws_open", datetime.now(tz=timezone.utc), {"market_slug": market.slug})

        def market_message(_ws, message):
            received = datetime.now(tz=timezone.utc)
            try:
                payload = json.loads(message)
            except Exception:
                return
            messages = payload if isinstance(payload, list) else [payload]
            for data in messages:
                if not isinstance(data, dict):
                    continue
                event_type = str(data.get("event_type") or data.get("type") or "market_ws")
                exchange_at = self._stream_timestamp(data.get("timestamp"), received)
                if event_type == "book":
                    token = str(data.get("asset_id") or "")
                    changed = self._store_stream_book(
                        token, bid=None, ask=None, captured_at=received, bids=data.get("bids"), asks=data.get("asks")
                    )
                    if changed:
                        self._emit_stream_event("market_book", received, {"market_slug": market.slug, "token_id": token, "exchange_ts_utc": exchange_at})
                elif event_type in {"price_change", "best_bid_ask"}:
                    changes = data.get("price_changes") if event_type == "price_change" else [data]
                    for change in changes or []:
                        if not isinstance(change, dict):
                            continue
                        token = str(change.get("asset_id") or "")
                        bid = finite_float(change.get("best_bid"))
                        ask = finite_float(change.get("best_ask"))
                        changed = self._store_stream_book(token, bid=bid, ask=ask, captured_at=received)
                        if changed:
                            self._emit_stream_event("market_price", received, {"market_slug": market.slug, "token_id": token, "exchange_ts_utc": exchange_at})

        def market_close(_ws, _status, _message):
            with self._stream_lock:
                if generation == self._stream_generation:
                    self._market_stream_connected = False
            self._emit_stream_event("market_ws_close", datetime.now(tz=timezone.utc), {"market_slug": market.slug})

        market_ws = websocket.WebSocketApp(MARKET_WS_URL, on_open=market_open, on_message=market_message, on_close=market_close)
        with self._stream_lock:
            self._market_ws = market_ws
        threading.Thread(
            target=lambda: market_ws.run_forever(ping_interval=10, ping_timeout=5, reconnect=2),
            name=f"stats-pm-market-ws-{market.slug}", daemon=True,
        ).start()

        if not (self.live and self._api_key and self._api_secret and self._api_pass):
            return

        def user_open(ws):
            with self._stream_lock:
                if generation != self._stream_generation:
                    return
                self._user_stream_connected = True
            ws.send(json.dumps({
                "auth": {"apiKey": self._api_key, "secret": self._api_secret, "passphrase": self._api_pass},
                "markets": [market.condition_id],
                "type": "user",
            }))
            self._emit_stream_event("user_ws_open", datetime.now(tz=timezone.utc), {"market_slug": market.slug})

        def user_message(_ws, message):
            received = datetime.now(tz=timezone.utc)
            try:
                payload = json.loads(message)
            except Exception:
                return
            messages = payload if isinstance(payload, list) else [payload]
            for data in messages:
                if not isinstance(data, dict):
                    continue
                order_id = data.get("order_id") or data.get("orderID") or data.get("id") or data.get("maker_order_id")
                self._emit_stream_event(
                    "user_order",
                    received,
                    {
                        "market_slug": market.slug,
                        "order_id": str(order_id or ""),
                        "user_event_type": str(data.get("event_type") or data.get("type") or ""),
                        "status": str(data.get("status") or ""),
                        "exchange_ts_utc": self._stream_timestamp(data.get("timestamp"), received),
                    },
                )

        def user_close(_ws, _status, _message):
            with self._stream_lock:
                if generation == self._stream_generation:
                    self._user_stream_connected = False
            self._emit_stream_event("user_ws_close", datetime.now(tz=timezone.utc), {"market_slug": market.slug})

        user_ws = websocket.WebSocketApp(USER_WS_URL, on_open=user_open, on_message=user_message, on_close=user_close)
        with self._stream_lock:
            self._user_ws = user_ws
        threading.Thread(
            target=lambda: user_ws.run_forever(ping_interval=10, ping_timeout=5, reconnect=2),
            name=f"stats-pm-user-ws-{market.slug}", daemon=True,
        ).start()

    def _ensure_private_client(self) -> bool:
        if not self.live:
            return False
        if self.client is not None:
            return True
        with self._client_init_lock:
            if self.client is not None:
                return True

            ClobClient, POLYGON, ApiCreds, MarketOrderArgs, OpenOrderParams, OrderArgs, OrderPayload, OrderType, PartialCreateOrderOptions = _lazy_import_clob()
            self._MarketOrderArgs = MarketOrderArgs
            self._OpenOrderParams = OpenOrderParams
            self._OrderArgs = OrderArgs
            self._OrderPayload = OrderPayload
            self._OrderType = OrderType
            self._PartialCreateOrderOptions = PartialCreateOrderOptions

            if not self._pk:
                self.client = ClobClient(host=POLYMARKET_HOST, chain_id=POLYGON)
                return True

            base = ClobClient(
                host=POLYMARKET_HOST,
                chain_id=POLYGON,
                key=self._pk,
                signature_type=self._sig_type,
                funder=self._funder,
            )
            creds = (
                ApiCreds(api_key=self._api_key, api_secret=self._api_secret, api_passphrase=self._api_pass)
                if self._api_key and self._api_secret and self._api_pass
                else base.create_or_derive_api_key()
            )
            self.client = ClobClient(
                host=POLYMARKET_HOST,
                chain_id=POLYGON,
                key=self._pk,
                creds=creds,
                signature_type=self._sig_type,
                funder=self._funder,
            )
            # The SDK's default HTTP/2 client is module-global. Keep the order-posting
            # path on an independent client so balance/order polling cannot occupy it.
            self._entry_transport = _DedicatedOrderTransport()
            self._entry_client = ClobClient(
                host=POLYMARKET_HOST,
                chain_id=POLYGON,
                key=self._pk,
                creds=creds,
                signature_type=self._sig_type,
                funder=self._funder,
            )
            self._entry_client._post = self._entry_transport.post
            self._entry_transport.start()
            try:
                self.wallet_address = str(base.get_address())
            except Exception:
                self.wallet_address = None
            return True

    def find_market(self, window_ts: int) -> MarketInfo | None:
        for ts in (window_ts, window_ts - WINDOW_SECONDS):
            slug = f"btc-updown-5m-{ts}"
            try:
                resp = self._http.get(
                    f"{GAMMA_API}/events",
                    params={"slug": slug},
                    timeout=GAMMA_HTTP_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                events = resp.json()
            except Exception:
                continue
            if not events:
                continue
            event = events[0] if isinstance(events, list) else events
            for market in event.get("markets", []):
                raw_ids = market.get("clobTokenIds", "[]")
                raw_outcomes = market.get("outcomes", '["Up","Down"]')
                ids = _json_list(raw_ids)
                outcomes = _json_list(raw_outcomes)
                tokens = {str(outcome).strip().upper(): str(token_id).strip() for outcome, token_id in zip(outcomes, ids)}
                if not (tokens.get("UP") and tokens.get("DOWN")):
                    continue
                start = datetime.fromtimestamp(ts, tz=timezone.utc)
                tick = finite_float(
                    market.get("minimum_tick_size")
                    or market.get("minimumTickSize")
                    or market.get("tick_size")
                    or market.get("tickSize")
                )
                min_order_size = finite_float(
                    market.get("minimum_order_size")
                    or market.get("minimumOrderSize")
                    or market.get("min_order_size")
                    or market.get("minOrderSize")
                    or market.get("order_min_size")
                    or market.get("orderMinSize")
                )
                condition_id = str(market.get("conditionId", market.get("condition_id", market.get("id", ""))))
                market_info = MarketInfo(
                    slug=slug,
                    market_id=str(market.get("id", condition_id)),
                    condition_id=condition_id,
                    question=str(market.get("question", slug)),
                    token_up=tokens["UP"],
                    token_down=tokens["DOWN"],
                    market_start_utc=start,
                    market_end_utc=start + timedelta(seconds=WINDOW_SECONDS),
                    tick_size=float(tick if tick is not None else 0.01),
                    min_order_size=min_order_size,
                    neg_risk=_boolish(market.get("negRisk") or market.get("neg_risk"), False),
                    fees_enabled=_boolish(market.get("feesEnabled"), False) if "feesEnabled" in market else None,
                    fee_rate=finite_float(market.get("feeRate") or market.get("fee_rate")),
                )
                return self.enrich_market_info(market_info)
        return None

    def enrich_market_info(self, market: MarketInfo) -> MarketInfo:
        if not self.live or self.client is None or not market.condition_id:
            return market
        try:
            data = self.client.get_market(market.condition_id)
        except Exception:
            return market
        getter = data.get if isinstance(data, dict) else lambda key, default=None: getattr(data, key, default)
        tick = finite_float(
            getter("minimum_tick_size")
            or getter("minimumTickSize")
            or getter("tick_size")
            or getter("tickSize")
            or market.tick_size
        )
        min_order_size = finite_float(
            getter("minimum_order_size")
            or getter("minimumOrderSize")
            or getter("min_order_size")
            or getter("minOrderSize")
            or getter("order_min_size")
            or getter("orderMinSize")
            or market.min_order_size
        )
        neg_risk = _boolish(getter("neg_risk", getter("negRisk", market.neg_risk)), market.neg_risk)
        fees_enabled_raw = getter("feesEnabled", getter("fees_enabled", market.fees_enabled))
        fee_rate = finite_float(getter("feeRate", getter("fee_rate", market.fee_rate)))
        return MarketInfo(
            slug=market.slug,
            market_id=market.market_id,
            condition_id=market.condition_id,
            question=market.question,
            token_up=market.token_up,
            token_down=market.token_down,
            market_start_utc=market.market_start_utc,
            market_end_utc=market.market_end_utc,
            tick_size=float(tick if tick is not None else market.tick_size),
            min_order_size=min_order_size,
            neg_risk=neg_risk,
            fees_enabled=_boolish(fees_enabled_raw, False) if fees_enabled_raw is not None else market.fees_enabled,
            fee_rate=fee_rate,
        )

    def resolved_market_outcome(
        self,
        *,
        slug: str,
        market_id: str | None = None,
        condition_id: str | None = None,
    ) -> MarketResolution:
        try:
            resp = self._http.get(
                f"{GAMMA_API}/events",
                params={"slug": slug},
                timeout=GAMMA_HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            return MarketResolution(None, f"gamma_error:{type(exc).__name__}", False)
        if not events:
            return MarketResolution(None, "gamma_no_event", False)

        event = events[0] if isinstance(events, list) else events
        markets = event.get("markets", []) if isinstance(event, dict) else []
        if not isinstance(markets, list) or not markets:
            return MarketResolution(None, "gamma_no_market", False)

        selected = None
        market_id_text = str(market_id or "")
        condition_id_text = str(condition_id or "")
        for market in markets:
            if not isinstance(market, dict):
                continue
            raw_condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
            raw_market_id = str(market.get("id") or "")
            if condition_id_text and raw_condition_id == condition_id_text:
                selected = market
                break
            if market_id_text and raw_market_id == market_id_text:
                selected = market
                break
        if selected is None:
            selected = next((market for market in markets if isinstance(market, dict)), None)
        if selected is None:
            return MarketResolution(None, "gamma_no_matching_market", False)

        status = str(selected.get("umaResolutionStatus") or selected.get("resolutionStatus") or "").strip().lower()
        closed = _boolish(selected.get("closed"), False)
        if status and status not in {"resolved", "finalized"}:
            return MarketResolution(None, f"gamma_unresolved:{status}", False)

        outcomes = [_normalise_outcome(outcome) for outcome in _json_list(selected.get("outcomes", '["Up","Down"]'))]
        prices = [finite_float(price) for price in _json_list(selected.get("outcomePrices"))]
        candidates = [(outcome, price) for outcome, price in zip(outcomes, prices) if outcome in {"UP", "DOWN"} and price is not None]
        if len(candidates) < 2:
            return MarketResolution(None, "gamma_missing_outcome_prices", False)

        candidates.sort(key=lambda item: item[1], reverse=True)
        winner, win_price = candidates[0]
        runner_up_price = candidates[1][1]
        resolved = closed or status in {"resolved", "finalized"} or win_price >= 0.99
        if not resolved:
            return MarketResolution(None, "gamma_not_closed", False)
        if win_price <= runner_up_price or win_price < 0.5:
            return MarketResolution(None, "gamma_ambiguous_outcome_prices", False)

        resolution_source = str(selected.get("resolutionSource") or event.get("resolutionSource") or "gamma_outcome_prices").strip()
        source = f"gamma_outcome_prices:{resolution_source}" if resolution_source else "gamma_outcome_prices"
        return MarketResolution(winner, source, True)

    def get_order_book_snapshot(self, token_id: str) -> BookSnapshot:
        now = datetime.now(tz=timezone.utc)
        streamed = self.stream_book_snapshot(token_id)
        if streamed is not None:
            return streamed
        if not self.live:
            try:
                resp = self._http.get(
                    f"{POLYMARKET_HOST}/book",
                    params={"token_id": token_id},
                    timeout=BOOK_HTTP_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                book = resp.json()
                bids = book.get("bids", []) if isinstance(book, dict) else []
                asks = book.get("asks", []) if isinstance(book, dict) else []

                def price(entry: object) -> float:
                    return float(entry["price"])

                def size(entry: object) -> float:
                    return float(entry["size"])

                bid_prices = [(price(b), max(0.0, size(b))) for b in bids]
                ask_prices = [(price(a), max(0.0, size(a))) for a in asks]
                best_bid = max((p for p, _ in bid_prices), default=None)
                best_ask = min((p for p, _ in ask_prices), default=None)
                bid_depth_total = sum(s for _, s in bid_prices)
                ask_depth_total = sum(s for _, s in ask_prices)
                best_bid_depth = sum(s for p, s in bid_prices if best_bid is not None and math.isclose(p, best_bid, abs_tol=1e-9))
                best_ask_depth = sum(s for p, s in ask_prices if best_ask is not None and math.isclose(p, best_ask, abs_tol=1e-9))
                if best_bid is not None and best_ask is not None:
                    mid = (best_bid + best_ask) / 2.0
                    spread = max(0.0, best_ask - best_bid)
                else:
                    mid = best_bid if best_bid is not None else best_ask
                    spread = None
                return BookSnapshot(
                    bid=best_bid,
                    ask=best_ask,
                    mid=mid,
                    spread=spread,
                    bid_depth_total=bid_depth_total,
                    ask_depth_total=ask_depth_total,
                    best_bid_depth=best_bid_depth,
                    best_ask_depth=best_ask_depth,
                    captured_at_utc=now,
                )
            except Exception:
                return BookSnapshot(None, None, None, None, None, None, None, None, now)
        if self.client is None:
            return BookSnapshot(None, None, None, None, None, None, None, None, now)
        try:
            book = self.client.get_order_book(token_id)
            bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
            asks = book.asks if hasattr(book, "asks") else book.get("asks", [])

            def price(entry: object) -> float:
                return float(entry.price if hasattr(entry, "price") else entry["price"])

            def size(entry: object) -> float:
                return float(entry.size if hasattr(entry, "size") else entry["size"])

            bid_prices = [(price(b), max(0.0, size(b))) for b in bids]
            ask_prices = [(price(a), max(0.0, size(a))) for a in asks]
            best_bid = max((p for p, _ in bid_prices), default=None)
            best_ask = min((p for p, _ in ask_prices), default=None)
            bid_depth_total = sum(s for _, s in bid_prices)
            ask_depth_total = sum(s for _, s in ask_prices)
            best_bid_depth = sum(s for p, s in bid_prices if best_bid is not None and math.isclose(p, best_bid, abs_tol=1e-9))
            best_ask_depth = sum(s for p, s in ask_prices if best_ask is not None and math.isclose(p, best_ask, abs_tol=1e-9))
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2.0
                spread = max(0.0, best_ask - best_bid)
            else:
                mid = best_bid if best_bid is not None else best_ask
                spread = None
            return BookSnapshot(
                bid=best_bid,
                ask=best_ask,
                mid=mid,
                spread=spread,
                bid_depth_total=bid_depth_total,
                ask_depth_total=ask_depth_total,
                best_bid_depth=best_bid_depth,
                best_ask_depth=best_ask_depth,
                captured_at_utc=now,
            )
        except Exception:
            return BookSnapshot(None, None, None, None, None, None, None, None, now)

    def place_maker_buy(
        self,
        *,
        token_id: str,
        price: float,
        shares: float,
        market: MarketInfo,
    ) -> OrderResult:
        if not self._ensure_private_client() or self.client is None:
            return OrderResult(False, None, "NO_CLIENT", "Polymarket client unavailable")
        try:
            options = self._PartialCreateOrderOptions(tick_size=_tick_text(market.tick_size), neg_risk=market.neg_risk)
            order_args = self._OrderArgs(token_id=token_id, price=price, size=shares, side="BUY")
            order = self.client.create_order(order_args, options=options)
            resp = self.client.post_order(order, self._OrderType.GTC, post_only=True)
            order_id = _extract_order_id(resp)
            if not order_id:
                return OrderResult(False, None, "NO_ORDER_ID", str(resp), price=price)
            return OrderResult(True, order_id, "PLACED", price=price)
        except Exception as exc:
            return OrderResult(False, None, "ERROR", str(exc), price=price)

    def create_signed_buy_order(
        self,
        *,
        token_id: str,
        price: float,
        shares: float,
        market: MarketInfo,
    ) -> SignedBuyOrder:
        if not self._ensure_private_client() or self.client is None:
            raise RuntimeError("Polymarket client unavailable")
        options = self._PartialCreateOrderOptions(tick_size=_tick_text(market.tick_size), neg_risk=market.neg_risk)
        order_args = self._OrderArgs(token_id=token_id, price=price, size=shares, side="BUY")
        order = self.client.create_order(order_args, options=options)
        return SignedBuyOrder(token_id=token_id, price=price, shares=shares, order=order)

    def post_signed_buy_order(self, signed_order: SignedBuyOrder) -> OrderResult:
        if not self._ensure_private_client() or self.client is None:
            return OrderResult(False, None, "NO_CLIENT", "Polymarket client unavailable", price=signed_order.price)
        try:
            entry_client = self._entry_client or self.client
            resp = entry_client.post_order(signed_order.order, self._OrderType.GTC)
            transport = getattr(self, "_entry_transport", None)
            transport_metrics = transport.last_post_metrics() if transport else None
            order_id = _extract_order_id(resp)
            if not order_id:
                return OrderResult(False, None, "NO_ORDER_ID", str(resp), price=signed_order.price, transport_metrics=transport_metrics)
            matched = _extract_matched(resp)
            avg_price = _extract_avg_price(resp, signed_order.price)
            return OrderResult(
                True,
                order_id,
                _extract_status(resp),
                str(resp),
                matched_shares=matched,
                price=avg_price,
                transport_metrics=transport_metrics,
            )
        except Exception as exc:
            transport = getattr(self, "_entry_transport", None)
            metrics = transport.last_post_metrics() if transport else None
            return OrderResult(False, None, "ERROR", str(exc), price=signed_order.price, transport_metrics=metrics)

    def place_immediate_sell(
        self,
        *,
        token_id: str,
        price: float,
        shares: float,
        order_type: str,
        market: MarketInfo,
    ) -> OrderResult:
        if not self._ensure_private_client() or self.client is None:
            return OrderResult(False, None, "NO_CLIENT", "Polymarket client unavailable")
        normalized_type = str(order_type).upper()
        clob_type = self._OrderType.FOK if normalized_type == "FOK" else self._OrderType.FAK
        try:
            options = self._PartialCreateOrderOptions(tick_size=_tick_text(market.tick_size), neg_risk=market.neg_risk)
            args = self._MarketOrderArgs(
                token_id=token_id,
                amount=round(max(0.0, shares), 4),
                side="SELL",
                price=price,
                order_type=clob_type,
            )
            resp = self.client.create_and_post_market_order(args, options=options, order_type=clob_type)
            order_id = _extract_order_id(resp)
            matched = 0.0
            if isinstance(resp, dict):
                matched = float(resp.get("size_matched") or resp.get("matched") or 0.0)
            return OrderResult(True, order_id, normalized_type, str(resp), matched_shares=matched, price=price)
        except Exception as exc:
            return OrderResult(False, None, normalized_type, str(exc), matched_shares=0.0, price=price)

    def get_order(self, order_id: str) -> object | None:
        if not self._ensure_private_client() or self.client is None:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception:
            return None

    def cancel_order(self, order_id: str) -> None:
        if not self._ensure_private_client() or self.client is None:
            return
        self.client.cancel_order(self._OrderPayload(orderID=order_id))

    def get_open_orders(self) -> list[object]:
        if not self._ensure_private_client() or self.client is None:
            return []
        try:
            return list(self.client.get_orders(self._OpenOrderParams()) or [])
        except Exception:
            return []

    def cancel_all_open_orders(self) -> int:
        cancelled = 0
        for order in self.get_open_orders():
            order_id = None
            if isinstance(order, dict):
                order_id = order.get("id") or order.get("orderID") or order.get("orderId")
            else:
                order_id = getattr(order, "id", None) or getattr(order, "orderID", None) or getattr(order, "orderId", None)
            if not order_id:
                continue
            try:
                self.cancel_order(str(order_id))
                cancelled += 1
            except Exception:
                pass
        return cancelled

    def fetch_wallet_balance(self) -> float | None:
        if not self._ensure_private_client() or self.client is None:
            return None
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            resp = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            raw = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", None)
            if raw is not None:
                return int(raw) / 1_000_000
        except Exception:
            return None
        return None
