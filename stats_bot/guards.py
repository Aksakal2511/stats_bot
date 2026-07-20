from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .strategy import finite_float


BPS_YES_CONFLICT_GUARD_START_TTE_SECONDS = 270.0
BPS_YES_CONFLICT_BPS_THRESHOLD = 0.05
BPS_YES_CONFLICT_UP_MIN_YES_MID = 0.45
BPS_YES_CONFLICT_DOWN_MAX_YES_MID = 0.55
BPS_YES_CONFLICT_GUARD_MIN_PERSISTENCE_SECONDS = 3
BPS_YES_CONFLICT_GUARD_COOLDOWN_SECONDS = 60
BPS_YES_CONFLICT_GUARD_WINDOW_LOCK_SECONDS = 5
BPS_YES_POSITION_CONFLICT_EXIT_MIN_PERSIST_SECONDS = 3
NORMALISED_SHARE_BASE = 5.0


def normalised_5_share_pnl(pnl: float, shares: float) -> float:
    if shares <= 0:
        return 0.0
    return float(pnl) * (NORMALISED_SHARE_BASE / float(shares))


class DrawdownGuard:
    def __init__(self) -> None:
        self.current_session_bucket: str | None = None

    def sync_session_bucket(self, session_bucket: str | None) -> None:
        bucket = str(session_bucket or "")
        if not bucket:
            return
        if self.current_session_bucket is None:
            self.current_session_bucket = bucket
            return
        self.current_session_bucket = bucket

    @property
    def paused_cells(self) -> set[str]:
        return set()

    def paused_cell_details(self, current_window_start: int | None) -> list[Any]:
        return []

    def record_closed_trade(
        self,
        *,
        day_type: str | None,
        session_bucket: str | None,
        trade_style: str | None,
        pnl: float,
        shares: float,
        market_slug: str | None = None,
    ) -> float:
        self.sync_session_bucket(session_bucket)
        return normalised_5_share_pnl(pnl, shares)

    def entry_blockers(
        self,
        *,
        snapshot: dict[str, Any] | None,
        trade_style: str | None,
        selected_policy_edge: float | None,
        entry_edge_threshold: float | None,
        open_positions: list[Any],
    ) -> tuple[str, ...]:
        if snapshot is None:
            return ()
        session_bucket = str(snapshot.get("session_bucket") or "")
        self.sync_session_bucket(session_bucket)
        return ()


@dataclass
class SessionStyleDrawdownGuard:
    """Block only the same London date + fixed time bucket + trade style after peak drawdown."""

    drawdown_per_share_usd: float = 1.5
    degraded_fraction: float = 0.5
    poor_fraction: float = 5.0 / 6.0
    degraded_edge_penalty: float = 0.0
    poor_edge_penalty: float = 0.0
    closed_pnl_by_key: dict[str, float] = field(default_factory=dict)
    peak_pnl_by_key: dict[str, float] = field(default_factory=dict)
    triggered_at_by_key: dict[str, datetime] = field(default_factory=dict)

    @staticmethod
    def make_key(london_date: str | None, session_label: str | None, trade_style: str | None) -> str | None:
        date = str(london_date or "").strip()
        session = str(session_label or "").strip()
        style = str(trade_style or "").strip().lower()
        if not date or not session or style not in {"continuation", "mean_reversion"}:
            return None
        return f"{date}|{session}|{style}"

    def record_closed_trade(
        self,
        *,
        london_date: str | None,
        session_label: str | None,
        trade_style: str | None,
        pnl: float,
        closed_at_utc: datetime,
        threshold_usd: float,
    ) -> tuple[str | None, float, bool]:
        key = self.make_key(london_date, session_label, trade_style)
        if key is None:
            return None, 0.0, False
        previous = float(self.closed_pnl_by_key.get(key, 0.0))
        updated = previous + float(pnl)
        self.closed_pnl_by_key[key] = updated
        peak = max(float(self.peak_pnl_by_key.get(key, 0.0)), updated)
        self.peak_pnl_by_key[key] = peak
        drawdown = max(0.0, peak - updated)
        activated = False
        if drawdown >= float(threshold_usd) and key not in self.triggered_at_by_key:
            self.triggered_at_by_key[key] = closed_at_utc
            activated = True
        return key, updated, activated

    def ladder_for_drawdown(self, drawdown: float, threshold_usd: float) -> tuple[str, float, float]:
        threshold = max(0.0, float(threshold_usd))
        ratio = float(drawdown) / threshold if threshold > 0.0 else 0.0
        if threshold > 0.0 and drawdown >= threshold:
            return "killed", float(self.poor_edge_penalty), ratio
        if ratio >= float(self.poor_fraction):
            return "poor", float(self.poor_edge_penalty), ratio
        if ratio >= float(self.degraded_fraction):
            return "degraded", float(self.degraded_edge_penalty), ratio
        return "normal", 0.0, ratio

    def details(
        self,
        *,
        london_date: str | None,
        session_label: str | None,
        session_name: str | None,
        trade_style: str | None,
        threshold_usd: float,
    ) -> dict[str, Any]:
        key = self.make_key(london_date, session_label, trade_style)
        pnl = float(self.closed_pnl_by_key.get(key, 0.0)) if key is not None else 0.0
        peak = float(self.peak_pnl_by_key.get(key, 0.0)) if key is not None else 0.0
        drawdown = max(0.0, peak - pnl)
        ladder_level, edge_penalty, drawdown_ratio = self.ladder_for_drawdown(drawdown, threshold_usd)
        return {
            "key": key or "",
            "london_date": str(london_date or ""),
            "session_label": str(session_label or ""),
            "session_name": str(session_name or session_label or ""),
            "trade_style": str(trade_style or ""),
            "closed_pnl": pnl,
            "peak_pnl": peak,
            "drawdown_usd": drawdown,
            "threshold_usd": float(threshold_usd),
            "drawdown_ratio": drawdown_ratio,
            "ladder_level": ladder_level,
            "edge_penalty": edge_penalty,
            "active": bool(key is not None and drawdown >= float(threshold_usd)),
            "triggered_at_utc": self.triggered_at_by_key.get(key) if key is not None else None,
        }

    def entry_blocker(
        self,
        *,
        london_date: str | None,
        session_label: str | None,
        session_name: str | None,
        trade_style: str | None,
        threshold_usd: float,
    ) -> tuple[str, ...]:
        detail = self.details(
            london_date=london_date,
            session_label=session_label,
            session_name=session_name,
            trade_style=trade_style,
            threshold_usd=threshold_usd,
        )
        if detail["active"]:
            return ("SESSION_STYLE_DRAWDOWN",)
        return ()


@dataclass
class BpsYesGuard:
    last_second_utc: datetime | None = None
    persistence_seconds: int = 0
    cooldown_until_utc: datetime | None = None
    window_blocked: bool = False
    position_last_second_utc: datetime | None = None
    position_persistence_seconds: int = 0

    def reset_window(self) -> None:
        self.last_second_utc = None
        self.persistence_seconds = 0
        self.cooldown_until_utc = None
        self.window_blocked = False
        self.position_last_second_utc = None
        self.position_persistence_seconds = 0

    def _bump_persistence(self, *, current_second: datetime, condition: bool, attr_last: str, attr_count: str) -> int:
        if condition:
            last = getattr(self, attr_last)
            if last is None:
                setattr(self, attr_count, 1)
            else:
                delta = int(round((current_second - last).total_seconds()))
                if delta == 1:
                    setattr(self, attr_count, int(getattr(self, attr_count)) + 1)
                elif delta <= 0:
                    pass
                else:
                    setattr(self, attr_count, 1)
            setattr(self, attr_last, current_second)
        else:
            setattr(self, attr_count, 0)
            setattr(self, attr_last, current_second)
        return int(getattr(self, attr_count))

    def _contradictory(self, *, signed_bps_raw: float, yes_mid: float, side: str | None) -> bool:
        normalized_side = str(side or "").upper()
        if normalized_side == "UP":
            return signed_bps_raw > BPS_YES_CONFLICT_BPS_THRESHOLD and yes_mid < BPS_YES_CONFLICT_UP_MIN_YES_MID
        if normalized_side == "DOWN":
            return signed_bps_raw < -BPS_YES_CONFLICT_BPS_THRESHOLD and yes_mid > BPS_YES_CONFLICT_DOWN_MAX_YES_MID
        return (
            (signed_bps_raw > BPS_YES_CONFLICT_BPS_THRESHOLD and yes_mid < BPS_YES_CONFLICT_UP_MIN_YES_MID)
            or (
                signed_bps_raw < -BPS_YES_CONFLICT_BPS_THRESHOLD
                and yes_mid > BPS_YES_CONFLICT_DOWN_MAX_YES_MID
            )
        )

    def update_entry_guard(self, *, now_utc: datetime, snapshot: dict[str, Any] | None) -> tuple[bool, str | None]:
        if snapshot is None:
            return False, None
        current_second = now_utc.replace(microsecond=0)
        tte = finite_float(snapshot.get("canonical_tte_seconds", snapshot.get("tte_seconds")))
        yes_mid = finite_float(snapshot.get("up_mid"))
        signed_bps_raw = finite_float(snapshot.get("cb_signed_bps_raw", snapshot.get("signed_bps_raw")))
        condition = bool(
            tte is not None
            and signed_bps_raw is not None
            and yes_mid is not None
            and math.isfinite(tte)
            and tte <= BPS_YES_CONFLICT_GUARD_START_TTE_SECONDS
            and self._contradictory(
                signed_bps_raw=signed_bps_raw,
                yes_mid=yes_mid,
                side=None,
            )
        )
        observed = self._bump_persistence(
            current_second=current_second,
            condition=condition,
            attr_last="last_second_utc",
            attr_count="persistence_seconds",
        )
        if condition and observed >= BPS_YES_CONFLICT_GUARD_MIN_PERSISTENCE_SECONDS:
            cooldown_until = current_second + timedelta(seconds=BPS_YES_CONFLICT_GUARD_COOLDOWN_SECONDS)
            if self.cooldown_until_utc is None or cooldown_until > self.cooldown_until_utc:
                self.cooldown_until_utc = cooldown_until
        if condition and observed >= BPS_YES_CONFLICT_GUARD_WINDOW_LOCK_SECONDS:
            self.window_blocked = True

        cooldown_active = False
        if self.cooldown_until_utc is not None:
            remaining = int((self.cooldown_until_utc - current_second).total_seconds())
            if remaining > 0:
                cooldown_active = True
            else:
                self.cooldown_until_utc = None

        if self.window_blocked:
            return True, "BPS_YES_CONFLICT_WINDOW_BLOCKED"
        if condition:
            return True, "BPS_YES_CONFLICT_CONDITION_MET"
        if cooldown_active:
            return True, "BPS_YES_CONFLICT_COOLDOWN_ACTIVE"
        return False, None

    def update_position_exit_guard(
        self,
        *,
        now_utc: datetime,
        snapshot: dict[str, Any] | None,
        held_side: str | None,
    ) -> tuple[bool, str | None]:
        if snapshot is None or held_side is None:
            self.position_last_second_utc = None
            self.position_persistence_seconds = 0
            return False, None
        current_second = now_utc.replace(microsecond=0)
        yes_mid = finite_float(snapshot.get("up_mid"))
        signed_bps_raw = finite_float(snapshot.get("cb_signed_bps_raw", snapshot.get("signed_bps_raw")))
        if signed_bps_raw is None or yes_mid is None:
            self.position_last_second_utc = current_second
            self.position_persistence_seconds = 0
            return False, None

        side = str(held_side).upper()
        contradictory = self._contradictory(
            signed_bps_raw=signed_bps_raw,
            yes_mid=yes_mid,
            side=side,
        )
        observed = self._bump_persistence(
            current_second=current_second,
            condition=contradictory,
            attr_last="position_last_second_utc",
            attr_count="position_persistence_seconds",
        )
        if observed >= BPS_YES_POSITION_CONFLICT_EXIT_MIN_PERSIST_SECONDS:
            return True, "BPS_YES_CONFLICT_POSITION_EMERGENCY_EXIT"
        return False, None

    def cooldown_remaining_seconds(self, now_utc: datetime) -> int:
        if self.cooldown_until_utc is None:
            return 0
        remaining = int((self.cooldown_until_utc - now_utc.replace(microsecond=0)).total_seconds())
        return max(0, remaining)
