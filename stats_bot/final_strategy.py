from __future__ import annotations

import json
import math
import os
import tempfile
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .strategy import EntryDecision, finite_float


@dataclass(frozen=True)
class FrozenEntryParameters:
    edge: float
    min_tte: int = 60
    max_tte: int = 220


def _empty_decision(blockers: tuple[str, ...]) -> EntryDecision:
    return EntryDecision(
        viable=False,
        blockers=blockers,
        policy_key=None,
        policy_record=None,
        selected_side=None,
        selected_bid=None,
        selected_ask=None,
        selected_mid=None,
        selected_spread=None,
        selected_trade_value=None,
        selected_policy_edge=None,
        selected_trade_style=None,
        entry_edge_threshold=None,
        entry_session_enabled=None,
        threshold_bps=0.0,
    )


def twap_effective_time_seconds(tte_seconds: float) -> float:
    tte = float(tte_seconds)
    if abs(tte - 60.0) <= 1e-9:
        return float(sum(k * k for k in range(1, 60)) / (59.0 * 59.0))
    return float((tte - 60.0) + sum(k * k for k in range(1, 60)) / 3600.0)


def selected_twap_dz(raw_move_dz: float, volatility_bps: float, tte_seconds: float, side: str) -> float | None:
    if not all(math.isfinite(x) for x in (raw_move_dz, volatility_bps, tte_seconds)) or tte_seconds < 60.0 - 1e-9:
        return None
    side = str(side).upper()
    if side not in {"UP", "DOWN"}:
        return None
    sign = 1.0 if side == "UP" else -1.0
    old_denom = max(0.05, volatility_bps * math.sqrt(max(1.0, tte_seconds) / 3600.0))
    move_bps = raw_move_dz * old_denom
    up_move_bps = sign * move_bps
    current_ratio = 1.0 + up_move_bps / 10_000.0
    required_ratio = 1.0
    if abs(tte_seconds - 60.0) <= 1e-9:
        required_ratio = (1.0 - current_ratio / 60.0) / (59.0 / 60.0)
    selected_margin_bps = sign * 10_000.0 * (current_ratio / required_ratio - 1.0)
    denom = max(0.05, volatility_bps * math.sqrt(max(0.0, twap_effective_time_seconds(tte_seconds)) / 3600.0))
    return selected_margin_bps / denom


def _ols_total(path: np.ndarray, horizon: int) -> float:
    values = np.asarray(path[: horizon + 1], dtype=float)
    x = -np.arange(horizon + 1, dtype=float)
    xc = x - x.mean()
    yc = values - values.mean()
    return float(horizon * np.dot(yc, xc) / np.dot(xc, xc))


def _segment_slope(path: np.ndarray, start_lag: int, end_lag: int) -> float:
    values = np.asarray(path[start_lag : end_lag + 1], dtype=float)
    x = -np.arange(start_lag, end_lag + 1, dtype=float)
    xc = x - x.mean()
    yc = values - values.mean()
    return float(np.dot(yc, xc) / np.dot(xc, xc))


class CausalBpsHistory:
    def __init__(self) -> None:
        self._market_slug = ""
        self._by_side: dict[str, deque[tuple[datetime, float]]] = {
            "UP": deque(),
            "DOWN": deque(),
        }

    def update(self, market_slug: str, timestamp: datetime, up_bps: float, down_bps: float) -> None:
        if market_slug != self._market_slug:
            self._market_slug = market_slug
            for values in self._by_side.values():
                values.clear()
        timestamp = timestamp.astimezone(timezone.utc)
        for side, value in (("UP", up_bps), ("DOWN", down_bps)):
            if not math.isfinite(value):
                continue
            values = self._by_side[side]
            if values and timestamp < values[-1][0]:
                continue
            if values and timestamp == values[-1][0]:
                values[-1] = (timestamp, value)
            else:
                values.append((timestamp, value))
            cutoff = timestamp - timedelta(seconds=65)
            while values and values[0][0] < cutoff:
                values.popleft()

    def features(self, side: str, timestamp: datetime) -> dict[str, float | bool]:
        values = self._by_side[str(side).upper()]
        result: dict[str, float | bool] = {
            "BPS_15": float("nan"),
            "BPS_60": float("nan"),
            "BPS_ACCEL_5_15": float("nan"),
            "BPS_15__available": False,
            "BPS_60__available": False,
            "BPS_ACCEL_5_15__available": False,
        }
        if not values:
            return result
        timestamps = [item[0] for item in values]
        observed = np.asarray([item[1] for item in values], dtype=float)
        path = np.full(61, np.nan, dtype=float)
        for lag in range(61):
            target = timestamp - timedelta(seconds=lag)
            position = bisect_right(timestamps, target) - 1
            if position >= 0:
                path[lag] = observed[position]
        if np.isfinite(path[:16]).all() and timestamps[0] <= timestamp - timedelta(seconds=15):
            result["BPS_15"] = _ols_total(path, 15)
            result["BPS_15__available"] = True
            result["BPS_ACCEL_5_15"] = _segment_slope(path, 0, 5) - _segment_slope(path, 5, 15)
            result["BPS_ACCEL_5_15__available"] = True
        if np.isfinite(path).all() and timestamps[0] <= timestamp - timedelta(seconds=60):
            result["BPS_60"] = _ols_total(path, 60)
            result["BPS_60__available"] = True
        return result


class P15PauseGuard:
    def __init__(self, spec: dict[str, Any], state_path: Path) -> None:
        self.spec = spec
        self.state_path = Path(state_path)
        self.detector_state = "NORMAL"
        self.recovery_hits = 0
        self.last_evaluation: datetime | None = None
        self.pause_until: datetime | None = None
        self.observations: deque[tuple[datetime, str, float]] = deque()
        self.counterfactual_blocked = 0
        self.last_value: float | None = None
        self.last_unique_markets = 0
        self.last_transition = ""
        self.last_transition_at: datetime | None = None
        self.counterfactual_markets: set[str] = set()
        self.legacy_counterfactual_event_count = 0
        self._last_observation_key: tuple[str, datetime] | None = None
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text())
        if data.get("detector_id") != self.spec["id"]:
            raise RuntimeError("Persisted detector state belongs to a different detector")
        self.detector_state = str(data.get("detector_state") or "NORMAL")
        self.recovery_hits = int(data.get("recovery_hits") or 0)
        self.last_evaluation = _parse_timestamp(data.get("last_evaluation"))
        self.pause_until = _parse_timestamp(data.get("pause_until"))
        self.last_transition = str(data.get("last_transition") or "")
        self.last_transition_at = _parse_timestamp(data.get("last_transition_at"))
        self.counterfactual_markets = {
            str(value) for value in data.get("counterfactual_markets", []) if str(value)
        }
        if int(data.get("state_schema_version") or 1) >= 2:
            self.counterfactual_blocked = int(data.get("counterfactual_blocked") or 0)
            self.legacy_counterfactual_event_count = int(data.get("legacy_counterfactual_event_count") or 0)
        else:
            # Version 1 incremented on every decision tick. Preserve it for audit,
            # but start the distinct-opportunity counter with the corrected meaning.
            self.legacy_counterfactual_event_count = int(data.get("counterfactual_blocked") or 0)
            self.counterfactual_blocked = len(self.counterfactual_markets)
        for record in data.get("observations", []):
            timestamp = _parse_timestamp(record.get("timestamp"))
            value = finite_float(record.get("model_edge"))
            if timestamp is not None and value is not None:
                self.observations.append((timestamp, str(record.get("market_slug") or ""), value))

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "state_schema_version": 2,
            "detector_id": self.spec["id"],
            "detector_state": self.detector_state,
            "recovery_hits": self.recovery_hits,
            "last_evaluation": self.last_evaluation.isoformat() if self.last_evaluation else None,
            "pause_until": self.pause_until.isoformat() if self.pause_until else None,
            "counterfactual_blocked": self.counterfactual_blocked,
            "counterfactual_markets": sorted(self.counterfactual_markets),
            "legacy_counterfactual_event_count": self.legacy_counterfactual_event_count,
            "last_transition": self.last_transition,
            "last_transition_at": self.last_transition_at.isoformat() if self.last_transition_at else None,
            "observations": [
                {"timestamp": timestamp.isoformat(), "market_slug": market, "model_edge": edge}
                for timestamp, market, edge in self.observations
            ],
        }
        fd, temporary = tempfile.mkstemp(prefix=self.state_path.name, dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.write("\n")
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def observe(self, now: datetime, market_slug: str, model_edge: float) -> None:
        now = now.astimezone(timezone.utc)
        key = (market_slug, now)
        if key != self._last_observation_key:
            self.observations.append((now, market_slug, float(model_edge)))
            self._last_observation_key = key
        cutoff = now - timedelta(minutes=float(self.spec["window_minutes"]) + 5.0)
        while self.observations and self.observations[0][0] < cutoff:
            self.observations.popleft()
        slot = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % int(self.spec["evaluation_minutes"]))
        if self.last_evaluation is None:
            self.last_evaluation = slot - timedelta(minutes=int(self.spec["evaluation_minutes"]))
        while self.last_evaluation + timedelta(minutes=int(self.spec["evaluation_minutes"])) <= slot:
            self.last_evaluation += timedelta(minutes=int(self.spec["evaluation_minutes"]))
            self._evaluate(self.last_evaluation)
        self._persist()

    def _evaluate(self, evaluation: datetime) -> None:
        start = evaluation - timedelta(minutes=float(self.spec["window_minutes"]))
        current = [(market, edge) for timestamp, market, edge in self.observations if start <= timestamp <= evaluation]
        self.last_unique_markets = len({market for market, _ in current})
        self.last_value = float(np.median([edge for _, edge in current])) if current else None
        available = self.last_value is not None and self.last_unique_markets >= int(self.spec["min_unique_markets"])
        if self.detector_state == "NORMAL":
            if available and self.last_value <= float(self.spec["enter_threshold"]):
                self.detector_state = "BAD"
                self.recovery_hits = 0
                self.pause_until = evaluation + timedelta(minutes=float(self.spec["pause_minutes"]))
                self.last_transition = "ENTER_BAD"
                self.last_transition_at = evaluation
        else:
            if available and self.last_value > float(self.spec["exit_threshold"]):
                self.recovery_hits += 1
            else:
                self.recovery_hits = 0
            required = int(self.spec["recovery_minutes"]) // int(self.spec["evaluation_minutes"])
            if self.recovery_hits >= required:
                self.detector_state = "NORMAL"
                self.recovery_hits = 0
                self.last_transition = "EXIT_BAD"
                self.last_transition_at = evaluation

    def is_paused(self, now: datetime) -> bool:
        return self.pause_until is not None and now.astimezone(timezone.utc) < self.pause_until

    def mark_counterfactual(self, market_slug: str) -> bool:
        market_slug = str(market_slug or "")
        if not market_slug or market_slug in self.counterfactual_markets:
            return False
        self.counterfactual_markets.add(market_slug)
        self.counterfactual_blocked += 1
        self._persist()
        return True

    def snapshot_fields(self, now: datetime) -> dict[str, Any]:
        warmed = self.last_value is not None and self.last_unique_markets >= int(self.spec["min_unique_markets"])
        remaining = max(0.0, (self.pause_until - now.astimezone(timezone.utc)).total_seconds()) if self.pause_until else 0.0
        return {
            "detector_id": self.spec["id"],
            "detector_status": self.detector_state if warmed else "WARMING",
            "detector_state": self.detector_state,
            "detector_value": self.last_value,
            "detector_q20": float(self.spec["enter_threshold"]),
            "detector_q30": float(self.spec["exit_threshold"]),
            "detector_unique_markets": self.last_unique_markets,
            "detector_recovery_hits": self.recovery_hits,
            "detector_recovery_required": int(self.spec["recovery_minutes"]) // int(self.spec["evaluation_minutes"]),
            "detector_armed": self.detector_state == "NORMAL",
            "detector_last_transition": self.last_transition,
            "detector_last_transition_at_utc": self.last_transition_at,
            "p15_pause_active": self.is_paused(now),
            "p15_pause_until_utc": self.pause_until,
            "p15_pause_remaining_seconds": remaining,
            "p15_counterfactual_blocked_opportunities": self.counterfactual_blocked,
        }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class FinalBtc5mStrategy:
    def __init__(self, artifact_path: Path, detector_state_path: Path) -> None:
        self.artifact_path = Path(artifact_path)
        if not self.artifact_path.exists():
            raise FileNotFoundError(f"Frozen final strategy artifact missing: {self.artifact_path}")
        self.artifact = json.loads(self.artifact_path.read_text())
        if self.artifact.get("strategy_version") != "btc5m_v1_aug14_sep14":
            raise RuntimeError("Unexpected final strategy artifact version")
        parity = self.artifact.get("parity_validation", {})
        if parity.get("holdout_used_for_fit") is not False or int(parity.get("branch_mismatches", -1)) != 0:
            raise RuntimeError("Final strategy artifact has not passed frozen holdout parity")
        self.strategy_id = str(self.artifact["strategy_version"])
        self.training_start = str(self.artifact["training_start"])
        self.training_end = str(self.artifact["training_end"])
        self.policy_type = "frozen_twap_dz_probability_stack"
        self.signal_feature = "TWAP_DZ"
        self.signal_price_mode = "coinbase_spot"
        self.entry = {"min_price": .20, "max_price": .80, "min_tte": 60, "max_tte": 220, "shares": 5, "edge_profile": "fixed_c2p5"}
        self.dynamic_edge_selector = None
        self.timed_entry_overrides: list[Any] = []
        self.session_display_names = {"all": "all"}
        self._parameters = FrozenEntryParameters(.025)
        self._coefficients = np.asarray(self.artifact["base_model"]["coefficients"], dtype=float)
        self._history = CausalBpsHistory()
        self.pause_guard = P15PauseGuard(self.artifact["detector"], detector_state_path)

    def __len__(self) -> int:
        return 1

    @staticmethod
    def display_trade_style(style: str | None) -> str:
        return "C" if str(style or "").lower() == "continuation" else str(style or "NA")

    def entry_parameters(self, *_: Any) -> FrozenEntryParameters:
        return self._parameters

    def entry_profile(self, snapshot: dict[str, Any]) -> tuple[str, str, FrozenEntryParameters, FrozenEntryParameters]:
        return str(snapshot.get("day_type") or "all"), "all", self._parameters, FrozenEntryParameters(99.0)

    def _raw_probability(self, ask: float, z: float) -> tuple[float, str]:
        names = self.artifact["base_model"]["bucket_names"]
        bounds = ((.20, .30), (.30, .45), (.45, .55), (.55, .70), (.70, .80))
        bucket_index = next((i for i, (low, high) in enumerate(bounds) if ask >= low and (ask < high or (i == 4 and ask <= high))), -1)
        if bucket_index < 0:
            raise ValueError("PRICE_OUT_OF_RANGE")
        x = np.zeros(len(self._coefficients), dtype=float)
        x[0] = 1.0
        if bucket_index:
            x[bucket_index] = 1.0
        x[5] = z
        x[6 + bucket_index] = z
        return float(np.clip(np.dot(x, self._coefficients), .001, .999)), str(names[bucket_index])

    def _isotonic(self, raw: float, ask: float, style: str) -> float:
        spec = self.artifact["isotonic"][style]
        calibrated_edge = float(np.interp((raw - ask) * 100.0, spec["x_thresholds"], spec["y_thresholds"]))
        return float(np.clip(ask + calibrated_edge / 100.0, .001, .999))

    def _residual(self, p0: float, features: dict[str, float | bool]) -> tuple[float, str]:
        if features["BPS_15__available"] and features["BPS_60__available"]:
            branch = "BPS15_BPS60"
        elif features["BPS_15__available"] and features["BPS_ACCEL_5_15__available"]:
            branch = "BPS15_ACCEL"
        else:
            return p0, "BASE_ONLY"
        spec = self.artifact["residual_branches"][branch]
        values = np.asarray([float(features[name]) for name in spec["features"]], dtype=float)
        z = np.asarray([
            np.clip((values[i] - scaler["mean"]) / scaler["sd"], -float(scaler["clip"]), float(scaler["clip"]))
            for i, scaler in enumerate(spec["scalers"])
        ])
        clipped = float(np.clip(p0, 1e-4, 1 - 1e-4))
        logit = math.log(clipped / (1.0 - clipped))
        correction = float(spec["beta0"]) + float(np.dot(z, np.asarray(spec["betas"], dtype=float)))
        return float(1.0 / (1.0 + math.exp(-float(np.clip(logit + correction, -35.0, 35.0))))), branch

    @staticmethod
    def _research_limit(ask: float, value: float, threshold: float, tick: float = .01) -> float:
        base = round(min(ask + tick, .99) * 100.0) / 100.0
        surplus = value - base - threshold
        spend = 0
        if surplus >= .015 - 1e-12:
            spend = min(2, max(1, int(math.floor((surplus - .015 + 1e-12) / .010)) + 1))
        adaptive = round(min(ask + (1 + spend) * tick, .99) * 100.0) / 100.0
        return adaptive if value - adaptive >= threshold - 1e-12 else base

    def evaluate(self, snapshot: dict[str, Any] | None, *, start_block_seconds: int = 0) -> EntryDecision:
        if snapshot is None:
            return _empty_decision(("NO_SNAPSHOT",))
        timestamp = snapshot.get("timestamp_utc")
        if not isinstance(timestamp, datetime):
            return _empty_decision(("NO_TIMESTAMP",))
        snapshot.update(self.pause_guard.snapshot_fields(timestamp))
        tte = finite_float(snapshot.get("canonical_tte_seconds", snapshot.get("tte_seconds")))
        vol = finite_float(snapshot.get("local_volatility_1h_bps"))
        raw_up = finite_float(snapshot.get("up_raw_move_dz", snapshot.get("raw_move_dz")))
        snapshot.update({"C_required": .025, "entry_rule_mr_edge": 99.0})
        if tte is None:
            return _empty_decision(("TTE_OUT_OF_RANGE",))
        tte_eligible = 60.0 <= tte <= 220.0
        # TWAP_DZ's remaining-window equation is undefined below 60 seconds.
        # Above 220 seconds we still compute read-only diagnostics, but never
        # admit the observation to trading or the P15 candidate stream.
        if tte < 60.0:
            return _empty_decision(("TTE_OUT_OF_RANGE",))
        if vol is None or raw_up is None:
            return _empty_decision(("MODEL_FEATURE_UNAVAILABLE",))
        raw_down = finite_float(snapshot.get("down_raw_move_dz"))
        if raw_down is None:
            raw_down = -raw_up
        old_denom = max(.05, vol * math.sqrt(max(1.0, tte) / 3600.0))
        market_slug = str(snapshot.get("canonical_window_id") or snapshot.get("market_slug") or "")
        self._history.update(market_slug, timestamp, raw_up * old_denom, raw_down * old_denom)
        candidates: list[dict[str, Any]] = []
        for side, raw in (("UP", raw_up), ("DOWN", raw_down)):
            ask = finite_float(snapshot.get(f"{side.lower()}_ask"))
            bid = finite_float(snapshot.get(f"{side.lower()}_bid"))
            state = selected_twap_dz(raw, vol, tte, side)
            snapshot[f"{side.lower()}_TWAP_DZ"] = state
            if ask is None or not .20 <= ask <= .80:
                continue
            if state is None:
                continue
            p_raw, bucket = self._raw_probability(ask, state)
            continuation_state = state > 1e-12
            diagnostic_style = "continuation" if continuation_state else "non_continuation"
            # The live strategy is continuation-only. Keep blocked-side
            # diagnostics on that same calibration; never reactivate the
            # retired MR trade family through a display-only calculation.
            p0 = self._isotonic(p_raw, ask, "continuation")
            prefix = side.lower()
            if f"{prefix}_BPS_15" in snapshot:
                features = {
                    name: snapshot.get(f"{prefix}_{name}")
                    for name in ("BPS_15", "BPS_60", "BPS_ACCEL_5_15")
                }
                features.update({
                    f"{name}__available": bool(snapshot.get(f"{prefix}_{name}__available"))
                    for name in ("BPS_15", "BPS_60", "BPS_ACCEL_5_15")
                })
            else:
                features = self._history.features(side, timestamp)
            p_final, branch = self._residual(p0, features)
            limit = self._research_limit(ask, p_final, .025)
            diagnostic = {
                "side": side, "ask": ask, "bid": bid, "state": state,
                "p_raw": p_raw, "p0": p0, "p_final": p_final, "branch": branch,
                "features": features, "bucket": bucket, "limit": limit,
                "edge_at_ask": p_final - ask, "edge_at_limit": p_final - limit,
            }
            snapshot.update({
                f"{prefix}_p_base_raw": p_raw,
                f"{prefix}_p_base_iso": p0,
                f"{prefix}_p_final": p_final,
                f"{prefix}_model_edge": p_final - ask,
                f"{prefix}_bps_branch": branch,
                f"{prefix}_diagnostic_style": diagnostic_style,
            })
            if continuation_state:
                candidates.append(diagnostic)
        if not candidates:
            snapshot.update(self.pause_guard.snapshot_fields(timestamp))
            blocker = "TTE_OUT_OF_RANGE" if not tte_eligible else "NO_CONTINUATION_SIDE"
            return _empty_decision((blocker,))
        selected = max(candidates, key=lambda item: item["edge_at_limit"])
        side = selected["side"]
        snapshot.update({
            "policy_signal_name": "TWAP_DZ",
            "selected_side": side,
            "selected_price_bucket": selected["bucket"],
            "selected_trade_style": "continuation",
            "TWAP_DZ": selected["state"],
            "selected_twap_dz": selected["state"],
            "p_base_raw": selected["p_raw"],
            "p_base_iso": selected["p0"],
            "p_final": selected["p_final"],
            "bps_branch": selected["branch"],
            "model_edge": selected["edge_at_ask"],
            "model_edge_at_limit": selected["edge_at_limit"],
            "C_required": .025,
            "entry_rule_name": "FIXED_C2P5",
            "entry_rule_min_tte": 60,
            "entry_rule_max_tte": 220,
            "entry_rule_c_edge": .025,
            "entry_rule_mr_edge": 99.0,
            **selected["features"],
        })
        blockers: list[str] = []
        edge_opportunity = selected["edge_at_limit"] >= .025 - 1e-12
        if not tte_eligible:
            blockers.append("TTE_OUT_OF_RANGE")
        elif not edge_opportunity:
            blockers.append("POLICY_NO_SIDE_EDGE")
        else:
            self.pause_guard.observe(timestamp, market_slug, selected["edge_at_ask"])
        snapshot.update(self.pause_guard.snapshot_fields(timestamp))
        if tte_eligible and edge_opportunity and self.pause_guard.is_paused(timestamp):
            blockers.append("P15_PAUSE")
            snapshot["p15_counterfactual_opportunity"] = self.pause_guard.mark_counterfactual(market_slug)
        mid = None if selected["bid"] is None else (selected["bid"] + selected["ask"]) / 2.0
        spread = None if selected["bid"] is None else selected["ask"] - selected["bid"]
        return EntryDecision(
            viable=not blockers,
            blockers=tuple(blockers),
            policy_key=(self.strategy_id, "continuation", selected["branch"], selected["bucket"]),
            policy_record=selected,
            selected_side=side,
            selected_bid=selected["bid"],
            selected_ask=selected["ask"],
            selected_mid=mid,
            selected_spread=spread,
            selected_trade_value=selected["p_final"],
            selected_policy_edge=selected["edge_at_ask"],
            selected_trade_style="continuation",
            entry_edge_threshold=.025,
            entry_session_enabled=True,
            threshold_bps=0.0,
        )
