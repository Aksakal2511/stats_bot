#!/usr/bin/env python3
"""Expand 1-second combined replay rows to Polymarket quote-event timestamps.

This preserves the existing BTC/Coinbase/Binance feature cadence and only
densifies the Polymarket price path.  For each raw Telonex PM quote event inside
the five-minute market, the latest 1s combined row for that second is duplicated
and its PM bid/ask/mid/spread fields are replaced with the event-level as-of
top-of-book prices.  The default output contains PM event rows only; the original
1s dataset remains the canonical base dataset.
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DAILY = (
    REPO_ROOT
    / "data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250/intermediate/daily"
)
DEFAULT_RAW_QUOTES = REPO_ROOT / "data/telonex/polymarket_btc_5m/raw/quotes"
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250_pm_events"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-dir", default=str(DEFAULT_DAILY))
    parser.add_argument("--raw-quotes-dir", default=str(DEFAULT_RAW_QUOTES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start-date", default="2026-02-14")
    parser.add_argument("--end-date", default="2026-07-25")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--price-change-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep only rows where event-level PM bid/ask changed versus the prior event row in the market.",
    )
    parser.add_argument(
        "--include-base-rows",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Include original 1s rows alongside PM event rows.",
    )
    parser.add_argument("--limit-days", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}h:{seconds % 3600 // 60:02d}m:{seconds % 60:02d}s"


def discover_days(daily_dir: Path, start_date: str, end_date: str) -> list[str]:
    days: list[str] = []
    with os.scandir(daily_dir) as entries:
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith(("date_utc=", "date=")):
                continue
            day = entry.name.split("=", 1)[1]
            if start_date <= day <= end_date and (Path(entry.path) / "part.parquet").exists():
                days.append(day)
    return sorted(set(days))


def daily_part_path(daily_dir: Path, day: str) -> Path:
    for prefix in ("date_utc", "date"):
        path = daily_dir / f"{prefix}={day}" / "part.parquet"
        if path.exists():
            return path
    raise FileNotFoundError(f"missing daily part for {day} under {daily_dir}")


def raw_quote_path(raw_quotes_dir: Path, day: str, slug: str, side: str) -> Path:
    return raw_quotes_dir / f"date={day}" / f"slug={slug}" / f"outcome={side}.parquet"


def top_quote_events(path: Path, start: pd.Timestamp, end: pd.Timestamp, side: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["event_time_utc", f"{side}_bid", f"{side}_ask"])
    schema = pq.ParquetFile(path).schema_arrow
    names = set(schema.names)
    if "ts_utc" in names:
        timestamp_col = "ts_utc"
    elif "timestamp_us" in names:
        timestamp_col = "timestamp_us"
    else:
        return pd.DataFrame(columns=["event_time_utc", f"{side}_bid", f"{side}_ask"])

    quote_cols = [col for col in ("bid_price", "ask_price") if col in names]
    if not quote_cols:
        return pd.DataFrame(columns=["event_time_utc", f"{side}_bid", f"{side}_ask"])

    frame = pd.read_parquet(path, columns=[timestamp_col, *quote_cols])
    if frame.empty:
        return pd.DataFrame(columns=["event_time_utc", f"{side}_bid", f"{side}_ask"])
    if "bid_price" not in frame.columns:
        frame["bid_price"] = np.nan
    if "ask_price" not in frame.columns:
        frame["ask_price"] = np.nan
    if timestamp_col == "ts_utc":
        frame["event_time_utc"] = pd.to_datetime(frame["ts_utc"], utc=True, errors="coerce")
    else:
        frame["event_time_utc"] = pd.to_datetime(
            pd.to_numeric(frame["timestamp_us"], errors="coerce"),
            unit="us",
            utc=True,
            errors="coerce",
        )
    frame["bid_price"] = pd.to_numeric(frame["bid_price"], errors="coerce")
    frame["ask_price"] = pd.to_numeric(frame["ask_price"], errors="coerce")
    frame = frame.loc[
        frame["event_time_utc"].notna()
        & frame["event_time_utc"].ge(start)
        & frame["event_time_utc"].lt(end)
        & (frame["bid_price"].notna() | frame["ask_price"].notna())
    ]
    if frame.empty:
        return pd.DataFrame(columns=["event_time_utc", f"{side}_bid", f"{side}_ask"])
    out = (
        frame.groupby("event_time_utc", as_index=False)
        .agg(
            **{
                f"{side}_bid": ("bid_price", "max"),
                f"{side}_ask": ("ask_price", "min"),
            }
        )
        .sort_values("event_time_utc", kind="stable")
        .reset_index(drop=True)
    )
    return out


def expanded_events_for_market(
    base: pd.DataFrame,
    raw_quotes_dir: Path,
    day: str,
    slug: str,
    *,
    price_change_only: bool,
) -> pd.DataFrame:
    market = base.loc[base["polymarket_slug"].astype(str).eq(str(slug))].copy()
    if market.empty:
        return pd.DataFrame()
    market = market.sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
    start = pd.to_datetime(market["polymarket_market_start_utc"].dropna().iloc[0], utc=True, errors="coerce")
    end = pd.to_datetime(market["polymarket_market_end_utc"].dropna().iloc[0], utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        start = pd.to_datetime(market["timestamp_utc"].min(), utc=True)
        end = pd.to_datetime(market["timestamp_utc"].max(), utc=True) + pd.Timedelta(seconds=1)

    up = top_quote_events(raw_quote_path(raw_quotes_dir, day, slug, "Up"), start, end, "up")
    down = top_quote_events(raw_quote_path(raw_quotes_dir, day, slug, "Down"), start, end, "down")
    if up.empty and down.empty:
        return pd.DataFrame()

    event_times = pd.concat(
        [part[["event_time_utc"]] for part in (up, down) if not part.empty],
        ignore_index=True,
    ).drop_duplicates().sort_values("event_time_utc", kind="stable")
    if event_times.empty:
        return pd.DataFrame()

    events = event_times.copy()
    if not up.empty:
        events = pd.merge_asof(events, up, on="event_time_utc", direction="backward")
    else:
        events["up_bid"] = np.nan
        events["up_ask"] = np.nan
    if not down.empty:
        events = pd.merge_asof(events, down, on="event_time_utc", direction="backward")
    else:
        events["down_bid"] = np.nan
        events["down_ask"] = np.nan

    events = events.loc[
        events[["up_bid", "up_ask", "down_bid", "down_ask"]].notna().all(axis=1)
        & events["up_ask"].ge(events["up_bid"])
        & events["down_ask"].ge(events["down_bid"])
    ].copy()
    if events.empty:
        return pd.DataFrame()

    events["base_second_utc"] = events["event_time_utc"].dt.floor("s")
    market_base = market.rename(columns={"timestamp_utc": "base_second_utc"}).copy()
    market_base["base_second_utc"] = pd.to_datetime(market_base["base_second_utc"], utc=True)
    expanded = events.merge(market_base, on="base_second_utc", how="inner", sort=False)
    if expanded.empty:
        return pd.DataFrame()

    expanded["pm_event_time_utc"] = expanded["event_time_utc"]
    expanded["pm_event_base_second_utc"] = expanded["base_second_utc"]
    expanded["timestamp_utc"] = expanded["event_time_utc"]
    expanded["polymarket_up_bid"] = expanded["up_bid"].astype(float)
    expanded["polymarket_up_ask"] = expanded["up_ask"].astype(float)
    expanded["polymarket_down_bid"] = expanded["down_bid"].astype(float)
    expanded["polymarket_down_ask"] = expanded["down_ask"].astype(float)
    expanded["polymarket_up_mid"] = (expanded["polymarket_up_bid"] + expanded["polymarket_up_ask"]) / 2.0
    expanded["polymarket_down_mid"] = (expanded["polymarket_down_bid"] + expanded["polymarket_down_ask"]) / 2.0
    expanded["polymarket_up_down_mid_sum"] = expanded["polymarket_up_mid"] + expanded["polymarket_down_mid"]
    expanded["polymarket_spread_up"] = expanded["polymarket_up_ask"] - expanded["polymarket_up_bid"]
    expanded["polymarket_spread_down"] = expanded["polymarket_down_ask"] - expanded["polymarket_down_bid"]
    expanded["polymarket_yes_no_spread"] = expanded["polymarket_up_ask"] + expanded["polymarket_down_ask"] - 1.0
    expanded["dq_polymarket_quote_present"] = True
    expanded["pm_event_expanded"] = True

    if "time_utc" in expanded.columns:
        expanded["time_utc"] = expanded["timestamp_utc"].dt.strftime("%H:%M:%S.%f")
    if "unix_seconds" in expanded.columns:
        expanded["unix_seconds"] = (expanded["timestamp_utc"].astype("int64") // 1_000_000_000).astype("int64")
    if "polymarket_sample_ts_utc" in expanded.columns:
        expanded["polymarket_sample_ts_utc"] = expanded["timestamp_utc"]
    if "sample_offset_mode" in expanded.columns:
        expanded["sample_offset_mode"] = "pm_event"
    if "combined_build_mode" in expanded.columns:
        expanded["combined_build_mode"] = expanded["combined_build_mode"].astype("object")

    if price_change_only:
        price_cols = ["polymarket_up_bid", "polymarket_up_ask", "polymarket_down_bid", "polymarket_down_ask"]
        changed = expanded[price_cols].ne(expanded[price_cols].shift()).any(axis=1)
        expanded = expanded.loc[changed].copy()

    drop_cols = [
        "event_time_utc",
        "base_second_utc",
        "up_bid",
        "up_ask",
        "down_bid",
        "down_ask",
    ]
    expanded = expanded.drop(columns=[col for col in drop_cols if col in expanded.columns])
    return expanded


def build_day(
    day: str,
    daily_dir: Path,
    raw_quotes_dir: Path,
    output_dir: Path,
    *,
    include_base_rows: bool,
    price_change_only: bool,
    overwrite: bool,
) -> dict[str, object]:
    target = output_dir / "intermediate" / "daily" / f"date_utc={day}" / "part.parquet"
    if target.exists() and not overwrite:
        rows = pd.read_parquet(target, columns=["timestamp_utc"]).shape[0]
        return {"date_utc": day, "status": "REUSE", "rows": int(rows), "event_rows": None}

    base = pd.read_parquet(daily_part_path(daily_dir, day))
    base["timestamp_utc"] = pd.to_datetime(base["timestamp_utc"], utc=True, errors="coerce")
    base["pm_event_expanded"] = False
    base["pm_event_time_utc"] = pd.NaT
    base["pm_event_base_second_utc"] = pd.NaT

    event_parts: list[pd.DataFrame] = []
    slugs = base["polymarket_slug"].dropna().astype(str).drop_duplicates().tolist()
    for slug in slugs:
        part = expanded_events_for_market(base, raw_quotes_dir, day, slug, price_change_only=price_change_only)
        if not part.empty:
            event_parts.append(part)

    frames = []
    if include_base_rows:
        frames.append(base)
    if event_parts:
        frames.append(pd.concat(event_parts, ignore_index=True, sort=False))
    if frames:
        out = pd.concat(frames, ignore_index=True, sort=False)
    else:
        out = base.iloc[0:0].copy()
    out = out.sort_values(["timestamp_utc", "canonical_window_id", "pm_event_expanded"], kind="stable").reset_index(drop=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    out.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(target)
    return {
        "date_utc": day,
        "status": "WRITE",
        "rows": int(len(out)),
        "base_rows": int(len(base)),
        "event_rows": int(sum(len(part) for part in event_parts)),
        "markets": int(len(slugs)),
    }


def main() -> int:
    args = parse_args()
    daily_dir = Path(args.daily_dir).expanduser().resolve()
    raw_quotes_dir = Path(args.raw_quotes_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    days = discover_days(daily_dir, args.start_date, args.end_date)
    if args.limit_days:
        days = days[: args.limit_days]
    if not days:
        raise FileNotFoundError(f"no daily parts found for {args.start_date}..{args.end_date} in {daily_dir}")

    if output_dir.exists() and args.overwrite:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "intermediate" / "daily").mkdir(parents=True, exist_ok=True)

    print(
        f"[{stamp()}] PLAN days={len(days)} start={days[0]} end={days[-1]} "
        f"include_base={args.include_base_rows} price_change_only={args.price_change_only} output={output_dir}",
        flush=True,
    )
    started = time.perf_counter()
    rows: list[dict[str, object]] = []
    worker_count = max(1, int(args.workers))
    with ProcessPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(
                build_day,
                day,
                daily_dir,
                raw_quotes_dir,
                output_dir,
                include_base_rows=bool(args.include_base_rows),
                price_change_only=bool(args.price_change_only),
                overwrite=bool(args.overwrite),
            ): day
            for day in days
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            rows.append(result)
            elapsed = time.perf_counter() - started
            eta = elapsed / idx * (len(futures) - idx)
            print(
                f"[{stamp()}] {result['status']} day={result['date_utc']} "
                f"rows={int(result['rows']):,} event_rows={result.get('event_rows')} "
                f"progress={idx}/{len(futures)} elapsed={fmt(elapsed)} eta={fmt(eta)}",
                flush=True,
            )

    summary = pd.DataFrame(rows).sort_values("date_utc", kind="stable")
    summary.to_csv(output_dir / "pm_event_expansion_summary.csv", index=False)
    manifest = {
        "source_daily_dir": str(daily_dir),
        "raw_quotes_dir": str(raw_quotes_dir),
        "output_dir": str(output_dir),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "include_base_rows": bool(args.include_base_rows),
        "price_change_only": bool(args.price_change_only),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "BTC/exchange features remain 1s; PM bid/ask fields are event-expanded from raw Telonex quotes.",
    }
    (output_dir / "pm_event_expansion_manifest.json").write_text(
        pd.Series(manifest).to_json(indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[{stamp()}] COMPLETE output={output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
