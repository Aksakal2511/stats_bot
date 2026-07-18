from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from stats_bot.bot import StatsBot
from stats_bot.binance import BinanceTickerFeed, adverse_signed_volume
from stats_bot.polymarket import MarketInfo


def test_adverse_signed_volume_uses_binance_taker_side() -> None:
    feed = BinanceTickerFeed(name="binance_spot", url="wss://example.invalid")
    base = int(datetime(2026, 7, 15, 18, 5, tzinfo=timezone.utc).timestamp())
    feed.upsert_second_bar(base, 100.0, 3.0, signed_size=3.0)
    feed.upsert_second_bar(base + 1, 99.9, 11.0, signed_size=-11.0)
    feed.upsert_second_bar(base + 2, 100.1, 4.0, signed_size=4.0)

    up_adverse, signed_total, buy_total, sell_total, seconds = adverse_signed_volume(
        feed.history_rows(),
        held_side="UP",
        lookback_seconds=3,
    )
    down_adverse, *_ = adverse_signed_volume(feed.history_rows(), held_side="DOWN", lookback_seconds=3)

    assert signed_total == -4.0
    assert buy_total == 7.0
    assert sell_total == 11.0
    assert seconds == 3
    assert up_adverse == 4.0
    assert down_adverse == 0.0


def test_dashboard_signed_bps_and_net_flow_diagnostics() -> None:
    bot = object.__new__(StatsBot)
    start = datetime(2026, 7, 15, 18, 5, tzinfo=timezone.utc)
    base = int(start.timestamp())
    bot.config = SimpleNamespace(pressure_signed_volume_lookback_seconds=30)
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=start,
        market_end_utc=start,
    )
    bot.binance_spot = BinanceTickerFeed(name="binance_spot", url="wss://example.invalid")
    bot.binance_perp = BinanceTickerFeed(name="binance_perp", url="wss://example.invalid")
    bot.binance_spot.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_spot.upsert_second_bar(base + 1, 100.0102, 9.0, signed_size=9.0)
    bot.binance_spot.upsert_second_bar(base + 2, 100.0102, 3.0, signed_size=-3.0)
    bot.binance_perp.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_perp.upsert_second_bar(base + 2, 100.0135, 0.0, signed_size=0.0)

    text = bot._dashboard_signed_bps_text({"cb_signed_bps_raw": 1.03})

    assert text == "coinbase 1.03 | binance spot 1.02 | binance perp 1.35 | avg 1.13"
    assert bot._dashboard_net_signed_flow_text() == "+6.00 BTC (30s)"


def test_dashboard_signed_bps_avg_ignores_display_zero_sources() -> None:
    bot = object.__new__(StatsBot)
    start = datetime(2026, 7, 15, 18, 5, tzinfo=timezone.utc)
    base = int(start.timestamp())
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=start,
        market_end_utc=start,
    )
    bot.binance_spot = BinanceTickerFeed(name="binance_spot", url="wss://example.invalid")
    bot.binance_perp = BinanceTickerFeed(name="binance_perp", url="wss://example.invalid")
    bot.binance_spot.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_spot.upsert_second_bar(base + 1, 99.99999, 0.0, signed_size=0.0)
    bot.binance_perp.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_perp.upsert_second_bar(base + 1, 100.02, 0.0, signed_size=0.0)

    text = bot._dashboard_signed_bps_text({"cb_signed_bps_raw": 2.0})

    assert text == "coinbase 2.00 | binance spot -0.00 | binance perp 2.00 | avg 2.00"
