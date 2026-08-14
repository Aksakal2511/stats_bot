from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from stats_bot.bot import StatsBot
from stats_bot.binance import BinanceTickerFeed, adverse_signed_volume
from stats_bot.polymarket import MarketInfo
from stats_bot.polymarket_ref import (
    POLYMARKET_TWAP_30_SUBSCRIBE,
    PolymarketReferenceFeed,
    _extract_btc_reference_price,
)


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
    bot.pm_chainlink = PolymarketReferenceFeed(url="wss://example.invalid")
    bot.binance_spot.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_spot.upsert_second_bar(base + 1, 100.0102, 9.0, signed_size=9.0)
    bot.binance_spot.upsert_second_bar(base + 2, 100.0102, 3.0, signed_size=-3.0)
    bot.binance_perp.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_perp.upsert_second_bar(base + 2, 100.0135, 0.0, signed_size=0.0)
    bot.pm_chainlink.upsert_second_bar(base, 100.0)
    bot.pm_chainlink.upsert_second_bar(base + 2, 100.0101)

    text = bot._dashboard_signed_bps_text({"cb_signed_bps_raw": 1.03})

    assert text == "1.03000"
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
    bot.pm_chainlink = PolymarketReferenceFeed(url="wss://example.invalid")
    bot.binance_spot.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_spot.upsert_second_bar(base + 1, 99.99999, 0.0, signed_size=0.0)
    bot.binance_perp.upsert_second_bar(base, 100.0, 0.0, signed_size=0.0)
    bot.binance_perp.upsert_second_bar(base + 1, 100.02, 0.0, signed_size=0.0)

    text = bot._dashboard_signed_bps_text({"cb_signed_bps_raw": 2.0})

    assert text == "2.00000"


def test_polymarket_reference_feed_extracts_chainlink_btc_price() -> None:
    raw = (
        '{"topic":"crypto_prices_chainlink","payload":'
        '{"symbol":"btc/usd","value":"100020000000000000000000","timestamp":1784372700123}}'
    )

    price, ts = _extract_btc_reference_price(raw)

    assert price == 100020.0
    assert ts is not None
    assert ts.isoformat() == "2026-07-18T11:05:00.123000+00:00"


def test_polymarket_reference_feed_extracts_chainlink_twap_price() -> None:
    raw = (
        '{"topic":"crypto_prices_twap_thirty","type":"update","payload":'
        '{"symbol":"btc/usd","value":65000.5,'
        '"full_accuracy_value":"65000500000000000000000",'
        '"timestamp":1785178800000,"window_s":30}}'
    )

    price, ts = _extract_btc_reference_price(raw)

    assert POLYMARKET_TWAP_30_SUBSCRIBE["subscriptions"][0]["topic"] == "crypto_prices_twap_thirty"
    assert price == 65000.5
    assert ts is not None
    assert ts.isoformat() == "2026-07-27T19:00:00+00:00"


def test_chainlink_twap_diagnostics_compare_spot_rebuild_and_official_twap() -> None:
    bot = object.__new__(StatsBot)
    start = datetime(2026, 7, 27, 19, 0, tzinfo=timezone.utc)
    now = start.replace(minute=2)
    bot.config = SimpleNamespace(
        pm_chainlink_feed_enabled=True,
        pm_chainlink_twap_diag_enabled=True,
        pm_chainlink_twap_diag_window_seconds=30,
    )
    bot.current_market = MarketInfo(
        slug="btc-updown-5m-test",
        market_id="1",
        condition_id="condition",
        question="test",
        token_up="up",
        token_down="down",
        market_start_utc=start,
        market_end_utc=start.replace(minute=5),
    )
    bot.pm_chainlink = PolymarketReferenceFeed(name="spot", url="wss://example.invalid")
    bot.pm_chainlink_twap = PolymarketReferenceFeed(
        name="twap",
        url="wss://example.invalid",
        subscribe_message=POLYMARKET_TWAP_30_SUBSCRIBE,
    )
    for offset in range(-40, 121):
        second = int((start.timestamp()) + offset)
        price = 65_000.0 + offset
        bot.pm_chainlink.upsert_second_bar(second, price)
    bot.pm_chainlink_twap.upsert_second_bar(int(start.timestamp()), 64_985.5, full_accuracy_value="64985500000000000000000")
    bot.pm_chainlink_twap.upsert_second_bar(int(now.timestamp()), 65_105.5, full_accuracy_value="65105500000000000000000")
    snapshot = {
        "cb_twap_30s_discrete_1s": 65_105.25,
        "cb_twap_open_30s_discrete_1s": 64_985.25,
        "cb_twap_signed_bps_raw": 10_000.0 * (65_105.25 - 64_985.25) / 64_985.25,
    }

    diag = bot._chainlink_twap_diagnostics(now, snapshot)

    assert diag["pm_cl_spot_twap_open_30s_discrete_1s"] == 64984.5
    assert diag["pm_cl_spot_twap_30s_discrete_1s"] == 65104.5
    assert diag["pm_cl_twap_open_30s"] == 64985.5
    assert diag["pm_cl_twap_30s"] == 65105.5
    assert diag["pm_cl_twap_full_accuracy_value"] == "65105500000000000000000"
    assert diag["pm_cl_spot_rebuild_minus_official_twap"] == -1.0
