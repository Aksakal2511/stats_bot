from __future__ import annotations

import queue
import threading
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from stats_bot.bot import EntrySubmission, FastEntryQuote, StatsBot
from stats_bot.polymarket import BookSnapshot, MarketInfo, OrderResult, PolymarketClient, SignedBuyOrder


def _market() -> MarketInfo:
    start = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
    return MarketInfo(
        slug="btc-updown-5m-test",
        market_id="market",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=start,
        market_end_utc=start + timedelta(seconds=300),
        tick_size=0.01,
        min_order_size=5.0,
    )


def test_stream_partial_price_update_preserves_other_book_side() -> None:
    client = object.__new__(PolymarketClient)
    client._stream_lock = threading.Lock()
    client._stream_books = {}
    now = datetime.now(tz=timezone.utc)

    client._store_stream_book("up", bid=0.40, ask=0.42, captured_at=now)
    client._store_stream_book("up", bid=None, ask=0.43, captured_at=now)

    snapshot = client.stream_book_snapshot("up")
    assert snapshot is not None
    assert snapshot.bid == 0.40
    assert snapshot.ask == 0.43
    assert math.isclose(snapshot.mid, 0.415)


def test_signed_order_post_uses_dedicated_entry_client() -> None:
    calls: list[tuple[object, object]] = []

    class EntryClient:
        def post_order(self, order, order_type):
            calls.append((order, order_type))
            return {"orderID": "entry-order", "status": "live"}

    client = object.__new__(PolymarketClient)
    client.client = object()
    client._entry_client = EntryClient()
    client._OrderType = SimpleNamespace(GTC="GTC")
    client._ensure_private_client = lambda: True

    result = client.post_signed_buy_order(SignedBuyOrder("up", 0.42, 5.0, "signed-order"))

    assert result.ok is True
    assert result.order_id == "entry-order"
    assert calls == [("signed-order", "GTC")]


def test_event_submission_is_single_flight_for_market() -> None:
    started = threading.Event()
    release = threading.Event()
    post_calls: list[SignedBuyOrder] = []
    pop_calls: list[tuple] = []
    log_events: list[tuple[str, str, dict]] = []

    class FakePolymarket:
        def post_signed_buy_order(self, signed_order: SignedBuyOrder) -> OrderResult:
            post_calls.append(signed_order)
            started.set()
            assert release.wait(timeout=2.0)
            return OrderResult(True, "order-1", "LIVE")

    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(
        paper_trading=False,
        paper_fill_model="fast_limit",
        execution_mode="event_driven_ws",
        observe_first_window=False,
        live_max_entry_attempts_per_window=3,
    )
    bot.current_market = _market()
    bot.current_window_ts = int(bot.current_market.market_start_utc.timestamp())
    bot.startup_window_ts = None
    bot.pending_entry = None
    bot.active_position = None
    bot.entry_fills_by_window = {bot.current_market.slug: 0}
    bot.entry_attempts_by_window = {bot.current_market.slug: 0}
    bot.entry_attempt_orders_by_window = {bot.current_market.slug: []}
    bot.last_entry_attempt_time_by_window = {}
    bot.wallet_balance_live = 50.0
    bot._entry_submission_lock = threading.Lock()
    bot._entry_submission = None
    bot._entry_submission_state = "IDLE"
    bot._entry_submission_late_logged = False
    bot._entry_submission_results = queue.SimpleQueue()
    bot._entry_submission_queue = queue.Queue(maxsize=1)
    bot._execution_worker_stop = threading.Event()
    bot._execution_worker_ready = threading.Event()
    bot._execution_worker_thread = None
    bot._execution_wakeup = threading.Event()
    bot.pm = FakePolymarket()
    bot._can_enter_after_cooldown = lambda *_args: True
    bot._reconcile_live_wallet_fills_for_window = lambda *_args: False
    bot._live_retry_cooldown_remaining = lambda *_args: 0.0
    bot._presigned_entries_available = lambda: True
    book = BookSnapshot(0.40, 0.41, 0.405, 0.01, None, None, None, None, datetime.now(tz=timezone.utc))
    quote = FastEntryQuote(
        price=0.42,
        shares=5.0,
        target_notional=2.1,
        min_order_size=5.0,
        spread_ticks=1,
        book=book,
        edge_at_ask=0.13,
        edge_at_limit=0.12,
        required_edge=0.05,
    )
    bot._fast_entry_quote = lambda *_args: quote
    signed = SignedBuyOrder("up", 0.42, 5.0, object())

    def pop(*args):
        pop_calls.append(args)
        return signed

    bot._presign_pop = pop
    bot._selected_token_id = lambda _side: "up"
    bot._entry_snapshot_with_meta = lambda *_args, **_kwargs: {}
    bot._log_latency = lambda *_args, **_kwargs: None
    bot._log_event = lambda event_type, message, fields: log_events.append((event_type, message, fields))
    bot._remember_error = lambda *_args, **_kwargs: None
    decision = SimpleNamespace(
        viable=True,
        selected_side="UP",
        policy_key_text="policy",
        selected_trade_value=0.54,
    )
    now = datetime.now(tz=timezone.utc)
    bot._start_execution_worker()

    bot._place_entry_event_driven(now, {}, decision)
    assert started.wait(timeout=1.0)
    bot._update_late_submission_state(now + timedelta(seconds=2))
    bot._update_late_submission_state(now + timedelta(seconds=3))
    bot._place_entry_event_driven(now, {}, decision)

    assert len(post_calls) == 1
    assert len(pop_calls) == 1
    assert bot.entry_attempts_by_window[bot.current_market.slug] == 1
    assert bot._entry_submission_inflight() is True
    assert bot._entry_submission_state == "LATE"
    assert len([event for event in log_events if event[0] == "entry_submission_late"]) == 1

    release.set()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=1)
    while bot._entry_submission_results.empty() and datetime.now(tz=timezone.utc) < deadline:
        threading.Event().wait(0.01)
    bot._drain_entry_submission_results(datetime.now(tz=timezone.utc))
    assert bot.pending_entry is not None
    assert bot.pending_entry.order_id == "order-1"
    assert bot._entry_submission_inflight() is False

    bot._place_entry_event_driven(datetime.now(tz=timezone.utc), {}, decision)
    assert len(post_calls) == 1
    assert len(pop_calls) == 1
    bot._execution_worker_stop.set()
    bot._entry_submission_queue.put_nowait(None)
    bot._execution_worker_thread.join(timeout=1.0)


def test_wallet_refresh_is_deferred_while_entry_submission_is_inflight() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(paper_trading=False, balance_refresh_seconds=15)
    bot.wallet_balance_live = 42.5
    bot._last_balance_refresh_at = 0.0
    bot._entry_submission_lock = threading.Lock()
    bot._entry_submission = object()
    bot.pm = SimpleNamespace(fetch_wallet_balance=lambda: (_ for _ in ()).throw(AssertionError("wallet REST competed with submit")))

    assert bot._refresh_wallet_balance(force=True) == 42.5


def test_peak_drawdown_kill_latches_at_fifteen_dollars() -> None:
    events: list[tuple[str, str, dict]] = []
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(peak_drawdown_kill_usd=15.0)
    bot.realized_pnl = 5.0
    bot.peak_realized_pnl = 0.0
    bot.peak_drawdown_usd = 0.0
    bot.peak_drawdown_kill_active = False
    bot.peak_drawdown_kill_triggered_at = None
    bot._log_event = lambda event_type, message, fields: events.append((event_type, message, fields))
    bot._log_latency = lambda *_args, **_kwargs: None
    now = datetime.now(tz=timezone.utc)

    bot._update_peak_drawdown_kill(now)
    assert bot.peak_realized_pnl == 5.0
    assert bot.peak_drawdown_kill_active is False

    bot.realized_pnl = -10.0
    bot._update_peak_drawdown_kill(now + timedelta(seconds=1))
    assert bot.peak_drawdown_usd == 15.0
    assert bot.peak_drawdown_kill_active is True
    assert len([event for event in events if event[0] == "peak_drawdown_kill_activated"]) == 1

    bot.realized_pnl = 20.0
    bot._update_peak_drawdown_kill(now + timedelta(seconds=2))
    assert bot.peak_drawdown_kill_active is True
    assert len([event for event in events if event[0] == "peak_drawdown_kill_activated"]) == 1


def test_paper_fast_limit_uses_execution_worker() -> None:
    bot = object.__new__(StatsBot)
    bot.config = SimpleNamespace(paper_trading=True, execution_mode="event_driven_ws")
    bot._entry_submission_queue = queue.Queue(maxsize=1)
    bot._entry_submission_results = queue.SimpleQueue()
    bot._entry_submission_lock = threading.Lock()
    bot._entry_submission = None
    bot._entry_submission_state = "IDLE"
    bot._execution_worker_stop = threading.Event()
    bot._execution_worker_ready = threading.Event()
    bot._execution_worker_thread = None
    bot._execution_wakeup = threading.Event()
    bot.pm = SimpleNamespace()
    market = _market()
    book = BookSnapshot(0.40, 0.41, 0.405, 0.01, None, None, None, None, datetime.now(tz=timezone.utc))
    quote = FastEntryQuote(
        price=0.42,
        shares=5.0,
        target_notional=2.1,
        min_order_size=5.0,
        spread_ticks=1,
        book=book,
        edge_at_ask=0.13,
        edge_at_limit=0.12,
        required_edge=0.05,
    )
    now = datetime.now(tz=timezone.utc)
    submission = EntrySubmission(
        submission_id="paper-test",
        market_slug=market.slug,
        token_id="up",
        policy_side="UP",
        market=market,
        signed_order=None,
        execution_mode="paper_fast_limit_async",
        quote=quote,
        snapshot={},
        policy_key_text="policy",
        wallet_balance_before=None,
        attempt_count=1,
        decision_at_utc=now,
        submitted_at_utc=now,
    )
    bot._entry_submission = submission
    bot._start_execution_worker()
    bot._entry_submission_queue.put_nowait(submission)

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=1)
    while bot._entry_submission_results.empty() and datetime.now(tz=timezone.utc) < deadline:
        threading.Event().wait(0.01)
    result = bot._entry_submission_results.get_nowait()
    assert result.submission.submission_id == "paper-test"
    assert result.result.matched_shares == 5.0
    assert result.result.price == 0.41

    bot._execution_worker_stop.set()
    bot._entry_submission_queue.put_nowait(None)
    bot._execution_worker_thread.join(timeout=1.0)
