#!/usr/bin/env python3
"""Download raw Coinbase BTC-USD trades without aggregating them to 1s candles.

The existing Coinbase backfill path intentionally aggregates trade pages into
canonical 1-second rows.  This script keeps the pre-aggregation trade rows so
they can later be joined causally to Polymarket quote-event timestamps.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.ingest import (  # noqa: E402
    COINBASE_BASE_URL_DEFAULT,
    COINBASE_TRADES_ENDPOINT_DEFAULT,
    BackfillRuntimeConfig,
    CoinbaseTradesClient,
    _seek_cursor_for_timestamp,
)


DEFAULT_CONFIG = REPO_ROOT / "config/market_data/coinbase_spot.yaml"
DEFAULT_OUTPUT = REPO_ROOT / "data/raw/coinbase_spot_btc_usd/trades"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", required=True, help="Inclusive UTC date, YYYY-MM-DD.")
    parser.add_argument("--to-date", required=True, help="Inclusive UTC date, YYYY-MM-DD.")
    parser.add_argument("--product-id", default="BTC-USD")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--pages-per-flush", type=int, default=250)
    parser.add_argument("--max-pages", type=int, default=0, help="Smoke-test cap; 0 means no cap.")
    parser.add_argument("--rate-limit-sleep-sec", type=float, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.execute and not args.dry_run:
        args.dry_run = True
    return args


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}h:{seconds % 3600 // 60:02d}m:{seconds % 60:02d}s"


def load_runtime(config_path: Path, args: argparse.Namespace) -> BackfillRuntimeConfig:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    coin_cfg = payload.get("coinbase", payload)
    request = coin_cfg.get("request", {})
    retry = coin_cfg.get("retry", {})
    pagination = coin_cfg.get("pagination", {})
    rate_limit = coin_cfg.get("rate_limit", {})
    sleep = (
        float(args.rate_limit_sleep_sec)
        if args.rate_limit_sleep_sec is not None
        else float(rate_limit.get("sleep_sec_between_requests", 0.25))
    )
    return BackfillRuntimeConfig(
        base_url=coin_cfg.get("base_url", COINBASE_BASE_URL_DEFAULT),
        trades_endpoint=coin_cfg.get("trades_endpoint", COINBASE_TRADES_ENDPOINT_DEFAULT),
        api_limit=int(request.get("limit", 1000)),
        request_timeout_sec=float(request.get("timeout_sec", 20.0)),
        max_retries=int(retry.get("max_retries", 8)),
        backoff_base_sec=float(retry.get("backoff_base_sec", 0.5)),
        backoff_cap_sec=float(retry.get("backoff_cap_sec", 20.0)),
        rate_limit_sleep_sec=sleep,
        pagination_param=pagination.get("param", "after"),
        pagination_header_after=pagination.get("response_header_after", "cb-after"),
        retryable_status_codes=set(retry.get("retryable_status_codes", [429, 500, 502, 503, 504])),
    )


def day_path(output_dir: Path, day: str) -> Path:
    return output_dir / f"date={day}" / "part.parquet"


def write_day(day: str, frames: list[pd.DataFrame], output_dir: Path, *, overwrite: bool) -> int:
    if not frames:
        return 0
    target = day_path(output_dir, day)
    if target.exists() and not overwrite:
        return int(pd.read_parquet(target, columns=["trade_id"]).shape[0])
    out = pd.concat(frames, ignore_index=True, sort=False)
    if out.empty:
        return 0
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp_utc", "trade_id", "price"])
    out["trade_id"] = pd.to_numeric(out["trade_id"], errors="coerce").astype("Int64")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["size"] = pd.to_numeric(out["size"], errors="coerce")
    out = (
        out.dropna(subset=["trade_id"])
        .drop_duplicates(subset=["trade_id"], keep="last")
        .sort_values(["timestamp_utc", "trade_id"], kind="stable")
        .reset_index(drop=True)
    )
    out["date_utc"] = out["timestamp_utc"].dt.strftime("%Y-%m-%d")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    out.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(target)
    return int(len(out))


def existing_days(output_dir: Path, from_date: str, to_date: str) -> set[str]:
    days: set[str] = set()
    if not output_dir.exists():
        return days
    for path in output_dir.glob("date=*/part.parquet"):
        day = path.parent.name.split("=", 1)[-1]
        if from_date <= day <= to_date:
            days.add(day)
    return days


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", force=True)
    logger = logging.getLogger("coinbase_raw_trades")

    output_dir = Path(args.output_dir).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    start_ts = pd.Timestamp(args.from_date, tz="UTC")
    end_exclusive = pd.Timestamp(args.to_date, tz="UTC") + pd.Timedelta(days=1)
    if start_ts >= end_exclusive:
        raise SystemExit("--from-date must be <= --to-date")

    present_days = existing_days(output_dir, args.from_date, args.to_date)
    print(
        f"[{stamp()}] PLAN raw Coinbase product={args.product_id} "
        f"range={args.from_date}..{args.to_date} output={output_dir} "
        f"existing_days={len(present_days)} overwrite={args.overwrite} execute={args.execute}",
        flush=True,
    )
    if args.dry_run:
        return 0

    if output_dir.exists() and args.overwrite:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runtime = load_runtime(config_path, args)
    client = CoinbaseTradesClient(runtime=runtime, logger=logger)
    seek_target = end_exclusive - pd.Timedelta(microseconds=1)
    cursor = _seek_cursor_for_timestamp(client, args.product_id, seek_target, logger)

    buffers: dict[str, list[pd.DataFrame]] = {}
    written_rows_by_day: dict[str, int] = {}
    pages = 0
    trade_rows_seen = 0
    trade_rows_kept = 0
    started = time.perf_counter()
    oldest_seen: pd.Timestamp | None = None
    newest_seen: pd.Timestamp | None = None
    reached_start = False

    def flush_completed_days(current_oldest_day: str | None = None, *, force: bool = False) -> None:
        completed: list[str] = []
        for day in sorted(buffers):
            if force or (current_oldest_day is not None and day > current_oldest_day):
                completed.append(day)
        for day in completed:
            rows = write_day(day, buffers.pop(day), output_dir, overwrite=args.overwrite)
            written_rows_by_day[day] = rows
            print(f"[{stamp()}] WRITE raw coinbase day={day} rows={rows:,}", flush=True)

    def progress_stats() -> tuple[float, float | None, float, str]:
        elapsed = max(1.0, time.perf_counter() - started)
        pages_per_min = pages / (elapsed / 60.0)
        if oldest_seen is None:
            return pages_per_min, None, 0.0, "estimating"
        total_seconds = max(1.0, (end_exclusive - start_ts).total_seconds())
        covered_seconds = max(0.0, (end_exclusive - oldest_seen).total_seconds())
        pct = min(100.0, 100.0 * covered_seconds / total_seconds)
        if pct <= 0.01:
            return pages_per_min, pct, 0.0, "estimating"
        eta_seconds = elapsed * (100.0 - pct) / pct
        return pages_per_min, pct, eta_seconds, fmt(eta_seconds)

    while True:
        page, next_cursor = client.fetch_trades_page(args.product_id, cursor)
        pages += 1
        if page.empty:
            break
        page["timestamp_utc"] = pd.to_datetime(page["timestamp_utc"], utc=True, errors="coerce")
        page = page.dropna(subset=["timestamp_utc"])
        if page.empty:
            cursor = next_cursor
            continue

        page_min = page["timestamp_utc"].min()
        page_max = page["timestamp_utc"].max()
        oldest_seen = page_min if oldest_seen is None else min(oldest_seen, page_min)
        newest_seen = page_max if newest_seen is None else max(newest_seen, page_max)
        trade_rows_seen += int(len(page))

        if page_min < start_ts:
            reached_start = True
        keep = page.loc[page["timestamp_utc"].ge(start_ts) & page["timestamp_utc"].lt(end_exclusive)].copy()
        if not keep.empty:
            keep["date_utc"] = keep["timestamp_utc"].dt.strftime("%Y-%m-%d")
            for day, part in keep.groupby("date_utc", sort=False):
                if day in present_days and not args.overwrite:
                    continue
                buffers.setdefault(str(day), []).append(part[["trade_id", "price", "size", "side", "timestamp_utc", "date_utc"]])
                trade_rows_kept += int(len(part))

        flush_completed_days(str(page_min.date()))

        if pages % max(1, int(args.pages_per_flush)) == 0:
            elapsed = time.perf_counter() - started
            pages_per_min, pct, _eta_seconds, eta_str = progress_stats()
            print(
                f"[{stamp()}] PROGRESS pages={pages:,} pages_per_min={pages_per_min:.1f} "
                f"progress_pct={'estimating' if pct is None else f'{pct:.2f}'} eta={eta_str} "
                f"seen_rows={trade_rows_seen:,} kept_rows={trade_rows_kept:,} "
                f"oldest_seen={None if oldest_seen is None else oldest_seen.isoformat()} "
                f"elapsed={fmt(elapsed)}",
                flush=True,
            )

        cursor = next_cursor
        if cursor is None or reached_start or (args.max_pages and pages >= int(args.max_pages)):
            break

    flush_completed_days(force=True)
    manifest = {
        "product_id": args.product_id,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "output_dir": str(output_dir),
        "pages_fetched": int(pages),
        "trade_rows_seen": int(trade_rows_seen),
        "trade_rows_kept": int(trade_rows_kept),
        "days_written": len(written_rows_by_day),
        "oldest_seen_utc": None if oldest_seen is None else oldest_seen.isoformat(),
        "newest_seen_utc": None if newest_seen is None else newest_seen.isoformat(),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "raw_coinbase_download_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pd.DataFrame(
        [{"date_utc": day, "rows": rows} for day, rows in sorted(written_rows_by_day.items())]
    ).to_csv(output_dir / "raw_coinbase_download_summary.csv", index=False)
    print(f"[{stamp()}] COMPLETE raw Coinbase pages={pages:,} days_written={len(written_rows_by_day)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
