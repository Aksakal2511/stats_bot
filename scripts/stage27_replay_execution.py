#!/usr/bin/env python3
"""Stage 27 E0 canonical and E1 live-like historical execution replay."""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

STATS_ROOT = Path(__file__).resolve().parents[1]
POLICY_ROOT = Path("/Volumes/Kate_DATA/policy_finder")
sys.path.insert(0, str(POLICY_ROOT))

from scripts.research.audit_twap_dynamic_edge_blockers import (  # noqa: E402
    replay_candidate_with_audit,
)
from scripts.research.run_stage23_5_mr_fixed_edge_btc import REPLAY_BASE, fixed_state  # noqa: E402
from scripts.research.run_twap_native_dynamic_edge_leave_one_day_oos import (  # noqa: E402
    build_decisions_from_state,
)
from scripts.research.run_twap_native_stress_resampled_validation import (  # noqa: E402
    ReplayConfig,
    statsbot_limit,
)
from scripts.stage26d_transition_pulse_preflight import (  # noqa: E402
    DATES,
    DETECTOR,
    LATENCIES,
    STAGE26B,
    stamp,
)

STATE_CACHE = (
    POLICY_ROOT
    / "twap/data/artifacts/stage23_5_mr_fixed_edge_btc/cache/development_state"
)
STAGE26D = POLICY_ROOT / "twap/data/artifacts/stage26d_local_pulse_guard_btc_corrected"
DEFAULT_OUTPUT = STATS_ROOT / "twap/data/artifacts/stage27_live_execution_fee_edge_btc"
EDGES = (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
SHARES = 5.0
FEE_RATE = 0.07
RETRY_SECONDS = 2.0
MAX_SUBMISSIONS = 3


def fee(shares: float, price: float) -> float:
    return round(float(shares) * FEE_RATE * float(price) * (1.0 - float(price)), 5)


def stage27_state(
    frame: pd.DataFrame,
    timeline: pd.DataFrame,
    edge_cents: float,
    *,
    p15_enabled: bool = True,
) -> pd.DataFrame:
    state = (
        fixed_state(frame, edge_cents, 99.0, f"C{edge_cents:g}")
        .sort_values("timestamp_utc")
        .copy()
    )
    state["timestamp_utc"] = stamp(state.timestamp_utc)
    state.loc[~state.trade_style.eq("continuation"), "threshold"] = np.inf
    state.loc[~state.trade_style.eq("continuation"), "blocked_by_guard"] = True
    enters = (
        timeline.loc[timeline.transition.eq("ENTER_BAD"), "evaluation_time"]
        if p15_enabled
        else pd.Series(dtype="datetime64[ns, UTC]")
    )
    from scripts.stage26d_pulse_replay import pulse_active

    active, _ = pulse_active(state.timestamp_utc, enters, 15)
    state["pulse_active"] = active
    state["pulse_policy_epoch"] = active.astype(np.int8)
    state.loc[active, "threshold"] = np.inf
    state.loc[active, "blocked_by_guard"] = True
    state["limit_price"] = statsbot_limit(
        state.selected_ask, state.value, state.threshold
    )
    state["edge_at_ask"] = state.value - state.selected_ask
    state["edge_at_limit"] = state.value - state.limit_price
    state["edge_surplus"] = state.edge_at_limit - state.threshold
    return state


def _context(row: pd.Series) -> dict[str, object]:
    bid = pd.to_numeric(pd.Series([row.get("selected_bid")]), errors="coerce").iloc[0]
    ask = float(row["selected_ask"])
    return {
        "branch_used": row.get("branch_used"),
        "twap_dz": row.get("selected_twap_move_dz"),
        "vol_1h": row.get("VOL_1H", row.get("local_volatility_1h_bps")),
        "model_edge": float(row["value"]) - ask,
        "edge_surplus": float(row["edge_surplus"]),
        "spread": ask - float(bid) if pd.notna(bid) else np.nan,
        "tte": row.get("canonical_tte_seconds"),
    }


def _record_trade(
    slug: str,
    pending: dict[str, object],
    row: pd.Series,
    price: float,
    liquidity: str,
    latency: int,
    edge_cents: float,
) -> dict[str, object]:
    outcome = int(pending["outcome"])
    gross = SHARES * ((1.0 - price) if outcome else -price)
    entry_fee = fee(SHARES, price) if liquidity == "TAKER" else 0.0
    return {
        "polymarket_slug": slug,
        "date_utc": pending["date_utc"],
        "signal_time_utc": pending["signal_time"],
        "fill_time_utc": row["timestamp_utc"],
        "latency_ms": latency,
        "side": pending["side"],
        "trade_style": "continuation",
        "submitted_limit": pending["limit"],
        "entry_limit": pending["limit"],
        "fill_price": price,
        "entry_ask": pending["entry_ask"],
        "entry_value": pending["value"],
        "threshold": edge_cents / 100.0,
        "outcome": outcome,
        "liquidity": liquidity,
        "gross_strategy_pnl": gross,
        "fees": entry_fee,
        "net_strategy_pnl": gross - entry_fee,
        "pnl": gross - entry_fee,
        "submitted_limit_cost": SHARES * float(pending["limit"]),
        "actual_fill_cost": SHARES * price,
        "price_improvement_vs_limit": SHARES * (float(pending["limit"]) - price),
        **pending["context"],
    }


def replay_live_like(
    state: pd.DataFrame, decisions: pd.DataFrame, latency: int, edge_cents: float
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Causal top-of-book approximation of the current live fast-limit lifecycle."""
    state_lookup = {
        (slug, side): g.sort_values("timestamp_utc", kind="mergesort").reset_index(
            drop=True
        )
        for (slug, side), g in state.groupby(["polymarket_slug", "side"], observed=True)
    }
    market_decisions = {
        slug: g.sort_values("timestamp_utc", kind="mergesort").reset_index(drop=True)
        for slug, g in decisions.groupby("polymarket_slug", observed=True)
    }
    trades: list[dict[str, object]] = []
    attempts: list[dict[str, object]] = []
    opportunities: list[dict[str, object]] = []
    for slug, drows in market_decisions.items():
        cursor, submissions, filled = 0, 0, False
        first_opportunity_written = False
        while cursor < len(drows) and not filled and submissions < MAX_SUBMISSIONS:
            decision = drows.iloc[cursor]
            side_state = state_lookup.get((slug, decision.side))
            if side_state is None or side_state.empty:
                cursor += 1
                continue
            signal_time = decision.timestamp_utc
            live_at = signal_time + pd.Timedelta(milliseconds=latency)
            future = side_state.loc[side_state.timestamp_utc.ge(signal_time)]
            ready = future.loc[future.timestamp_utc.le(live_at)]
            submit = ready.iloc[-1] if not ready.empty else future.iloc[0]
            limit_price = float(decision.limit_price)
            submit_ask = float(submit.selected_ask)
            ctx = _context(decision)
            if not first_opportunity_written:
                opportunities.append(
                    {
                        "polymarket_slug": slug,
                        "date_utc": str(decision.date_utc),
                        "latency_ms": latency,
                        "c_edge_cents": edge_cents,
                        "signal_time_utc": signal_time,
                        "executable_time_utc": submit.timestamp_utc,
                        "side": decision.side,
                        "submitted_limit": limit_price,
                        "executable_ask": submit_ask,
                        "outcome": int(decision.outcome),
                        "pulse_active": bool(decision.get("pulse_active", False)),
                        **ctx,
                    }
                )
                first_opportunity_written = True
            if submit_ask > limit_price + 0.02 + 1e-12:
                attempts.append(
                    {
                        "polymarket_slug": slug,
                        "status": "SKIPPED",
                        "reason": "PRE_SUBMIT_ASK_REVALIDATION",
                        "signal_time_utc": signal_time,
                        "event_time_utc": submit.timestamp_utc,
                        "latency_ms": latency,
                        "entry_limit": limit_price,
                        "event_ask": submit_ask,
                    }
                )
                nxt = drows.index[drows.timestamp_utc.gt(submit.timestamp_utc)]
                cursor = int(nxt[0]) if len(nxt) else len(drows)
                continue
            submissions += 1
            pending = {
                "date_utc": str(decision.date_utc),
                "signal_time": signal_time,
                "side": decision.side,
                "limit": limit_price,
                "entry_ask": float(decision.selected_ask),
                "value": float(decision.value),
                "threshold": float(decision.threshold),
                "outcome": int(decision.outcome),
                "epoch": decision.pulse_policy_epoch,
                "context": ctx,
            }
            attempts.append(
                {
                    "polymarket_slug": slug,
                    "status": "SUBMITTED",
                    "reason": "GTC_FAST_LIMIT",
                    "signal_time_utc": signal_time,
                    "event_time_utc": submit.timestamp_utc,
                    "latency_ms": latency,
                    "entry_limit": limit_price,
                    "event_ask": submit_ask,
                }
            )
            if submit_ask <= limit_price + 1e-12:
                trades.append(
                    _record_trade(
                        slug, pending, submit, submit_ask, "TAKER", latency, edge_cents
                    )
                )
                filled = True
                break
            stale_start = None
            cancel_time = submit.timestamp_utc
            resting = future.loc[future.timestamp_utc.ge(submit.timestamp_utc)]
            for _, event in resting.iterrows():
                now = event.timestamp_utc
                age = (now - submit.timestamp_utc).total_seconds()
                ask = float(event.selected_ask)
                current_edge = float(event.value) - limit_price
                if event.pulse_policy_epoch != pending["epoch"] and (
                    bool(event.blocked_by_guard)
                    or current_edge + 1e-12 < float(event.threshold)
                ):
                    cancel_time = now
                    break
                if age >= 3.0:
                    cancel_time = now
                    break
                if ask <= limit_price + 1e-12:
                    trades.append(
                        _record_trade(
                            slug,
                            pending,
                            event,
                            limit_price,
                            "MAKER",
                            latency,
                            edge_cents,
                        )
                    )
                    filled = True
                    break
                if current_edge + 1e-12 < float(pending["threshold"]):
                    if not (age <= 1.5 and current_edge + 1e-12 >= 0.01):
                        cancel_time = now
                        break
                if ask - limit_price > 0.02 + 1e-12:
                    stale_start = now if stale_start is None else stale_start
                    if (now - stale_start).total_seconds() >= 0.6:
                        cancel_time = now
                        break
                else:
                    stale_start = None
                cancel_time = now
            if filled:
                break
            attempts.append(
                {
                    "polymarket_slug": slug,
                    "status": "CANCELLED",
                    "reason": "LIVE_LIFECYCLE_CANCEL",
                    "signal_time_utc": signal_time,
                    "event_time_utc": cancel_time,
                    "latency_ms": latency,
                    "entry_limit": limit_price,
                    "event_ask": np.nan,
                }
            )
            retry_at = cancel_time + pd.Timedelta(seconds=RETRY_SECONDS)
            nxt = drows.index[drows.timestamp_utc.ge(retry_at)]
            cursor = int(nxt[0]) if len(nxt) else len(drows)
    return pd.DataFrame(trades), pd.DataFrame(attempts), pd.DataFrame(opportunities)


def _reference_p15(date: str, latency: int) -> pd.DataFrame:
    return pd.read_parquet(
        STAGE26D
        / "replay"
        / f"date={date}"
        / f"latency={latency}"
        / "scenario=P15_PAUSE/trades.parquet"
    )


def validate_e0_p15(trades: pd.DataFrame, date: str, latency: int) -> None:
    ref = _reference_p15(date, latency)
    keys = ["polymarket_slug", "side", "signal_time_utc", "fill_time_utc"]
    if len(trades) != len(ref):
        raise RuntimeError(
            f"E0 STOP fill count {date}/{latency}: {len(trades)}/{len(ref)}"
        )
    left, right = (
        trades.sort_values(keys).reset_index(drop=True),
        ref.sort_values(keys).reset_index(drop=True),
    )
    for col in ("signal_time_utc", "fill_time_utc"):
        left[col], right[col] = stamp(left[col]), stamp(right[col])
    if not all(left[c].eq(right[c]).all() for c in keys):
        raise RuntimeError(f"E0 STOP identity mismatch {date}/{latency}")
    if not np.allclose(left.entry_limit, right.entry_limit, rtol=1e-10, atol=1e-12):
        raise RuntimeError(f"E0 STOP entry mismatch {date}/{latency}")
    if not np.allclose(left.pnl, right.pnl, rtol=1e-10, atol=1e-12):
        raise RuntimeError(f"E0 STOP PnL mismatch {date}/{latency}")


def work_unit(
    date: str, latency: int, edge: float, scenario: str, output_root: str, resume: bool
) -> str:
    root = Path(output_root)
    dest = (
        root
        / "replay"
        / f"scenario={scenario}"
        / f"c_edge={edge:.1f}"
        / f"latency={latency}"
        / f"date={date}"
    )
    marker = dest / "_COMPLETE.json"
    counterfactual_required = scenario == "E1" and math.isclose(edge, 2.5)
    if (
        resume
        and marker.exists()
        and (
            not counterfactual_required
            or (dest / "counterfactual_no_p15_trades.parquet").exists()
        )
    ):
        return f"SKIP {scenario} C{edge:.1f} {latency} {date}"
    dest.mkdir(parents=True, exist_ok=True)
    frame = pd.read_parquet(STATE_CACHE / f"date_utc={date}.parquet")
    timeline = pd.read_parquet(STAGE26B / "detector_timeline.parquet")
    timeline = timeline.loc[
        timeline.config_id.eq(DETECTOR) & timeline.latency_ms.eq(latency)
    ].copy()
    state = stage27_state(frame, timeline, edge)
    decisions = build_decisions_from_state(state, 100)
    if scenario == "E0":
        trades, attempts = replay_candidate_with_audit(
            state,
            decisions,
            ReplayConfig(latency_ms=latency, **REPLAY_BASE),
            policy_epoch_column="pulse_policy_epoch",
        )
        if math.isclose(edge, 2.5):
            validate_e0_p15(trades, date, latency)
        if len(trades):
            trades = trades.copy()
            trades["submitted_limit"] = trades.entry_limit
            trades["fill_price"] = trades.entry_limit
            trades["liquidity"] = "CANONICAL"
            trades["gross_strategy_pnl"] = trades.pnl
            trades["fees"] = 0.0
            trades["net_strategy_pnl"] = trades.pnl
            trades["submitted_limit_cost"] = SHARES * trades.entry_limit
            trades["actual_fill_cost"] = SHARES * trades.entry_limit
            trades["price_improvement_vs_limit"] = 0.0
        opportunities = (
            decisions.groupby("polymarket_slug", observed=True).first().reset_index()
        )
    else:
        trades, attempts, opportunities = replay_live_like(
            state, decisions, latency, edge
        )
        if math.isclose(edge, 2.5):
            no_p15_state = stage27_state(frame, timeline, edge, p15_enabled=False)
            no_p15_decisions = build_decisions_from_state(no_p15_state, 100)
            no_p15_trades, _, _ = replay_live_like(
                no_p15_state, no_p15_decisions, latency, edge
            )
            no_p15_trades.to_parquet(
                dest / "counterfactual_no_p15_trades.parquet",
                index=False,
                compression="zstd",
            )
    for frame_out, name in (
        (trades, "trades.parquet"),
        (attempts, "attempts.parquet"),
        (opportunities, "opportunities.parquet"),
    ):
        frame_out.to_parquet(dest / name, index=False, compression="zstd")
    metrics = {
        "date_utc": date,
        "latency_ms": latency,
        "c_edge_cents": edge,
        "scenario": scenario,
        "eligible_markets": int(decisions.polymarket_slug.nunique()),
        "eligible_evaluations": len(decisions),
        "submitted_markets": int(
            attempts.loc[attempts.status.eq("SUBMITTED"), "polymarket_slug"].nunique()
        )
        if len(attempts) and "status" in attempts
        else np.nan,
        "attempts": int(attempts.status.eq("SUBMITTED").sum())
        if len(attempts) and "status" in attempts
        else len(attempts),
        "fills": len(trades),
        "gross_pnl": float(
            trades.get(
                "gross_strategy_pnl", trades.get("pnl", pd.Series(dtype=float))
            ).sum()
        ),
        "fees": float(trades.get("fees", pd.Series(dtype=float)).sum()),
        "net_pnl": float(
            trades.get(
                "net_strategy_pnl", trades.get("pnl", pd.Series(dtype=float))
            ).sum()
        ),
    }
    (dest / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    marker.write_text(json.dumps({"complete": True, **metrics}, indent=2) + "\n")
    return f"DONE {scenario} C{edge:.1f} {latency} {date} fills={len(trades)} net={metrics['net_pnl']:.2f}"


def parse_list(value: str, cast):
    return [cast(x) for x in value.split(",") if x.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("E0", "E1", "ALL"), required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--scenario", choices=("E0", "E1", "ALL"), default="ALL")
    p.add_argument("--c-edge", default="2.5")
    p.add_argument("--latency", default="175,225,325")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    scenarios = [args.phase] if args.phase != "ALL" else ["E0", "E1"]
    if args.scenario != "ALL":
        scenarios = [args.scenario]
    edges, latencies = parse_list(args.c_edge, float), parse_list(args.latency, int)
    if (
        not set(edges).issubset(EDGES)
        or not set(latencies).issubset(LATENCIES)
        or not 1 <= args.workers <= 4
    ):
        p.error("edges, latencies, or workers outside the predeclared Stage27 set")
    if (
        "E1" in scenarios
        and (args.output_root / "STOP_E0_REPRODUCTION_FAILED").exists()
    ):
        raise RuntimeError("E0 STOP marker exists; E1 is prohibited")
    jobs = [
        (date, latency, edge, scenario)
        for scenario in scenarios
        for edge in edges
        for latency in latencies
        for date in DATES
    ]
    args.output_root.mkdir(parents=True, exist_ok=True)
    try:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(
                    work_unit,
                    date,
                    latency,
                    edge,
                    scenario,
                    str(args.output_root),
                    args.resume,
                )
                for date, latency, edge, scenario in jobs
            ]
            for n, future in enumerate(as_completed(futures), 1):
                print(f"[{n}/{len(futures)}] {future.result()}", flush=True)
    except Exception as exc:
        if "E0 STOP" in str(exc):
            (args.output_root / "STOP_E0_REPRODUCTION_FAILED").write_text(
                str(exc) + "\n"
            )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
