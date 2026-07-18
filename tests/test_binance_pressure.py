from __future__ import annotations

from datetime import datetime, timezone

from stats_bot.binance import BinanceTickerFeed, adverse_signed_volume


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
