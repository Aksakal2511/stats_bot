from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class PendingEntryOrder:
    order_id: str
    token_id: str
    side: str
    price: float
    target_shares: float
    created_at_utc: datetime
    market_slug: str
    policy_key_text: str
    policy_side: str
    snapshot: dict[str, Any]
    target_notional: float | None = None
    wallet_balance_before: float | None = None
    wallet_reconciled: bool = False
    matched_shares_seen: float = 0.0
    cancel_requested: bool = False
    stale_quote_started_at_utc: datetime | None = None


@dataclass
class HedgeLeg:
    token_id: str
    side: str
    shares: float
    avg_price: float
    cost: float
    fee: float
    order_id: str | None
    trigger_source: str
    trigger_z: float
    trigger_time_utc: datetime
    fill_time_utc: datetime
    limit_price: float
    ask_at_trigger: float
    status: str = "filled"


@dataclass
class OpenPosition:
    token_id: str
    market_slug: str
    market_id: str
    market_question: str
    market_end_utc: datetime
    held_side: str
    shares: float
    avg_entry_price: float
    entry_cost: float
    entry_time_utc: datetime
    entry_bps: float
    entry_day_type: str
    entry_session_bucket: str
    entry_edge_threshold: float
    entry_bps_threshold: float
    entry_policy_key_text: str
    entry_snapshot: dict[str, Any]
    exit_triggered: bool = False
    exit_reason: str = ""
    exit_proceeds_accumulated: float = 0.0
    exited_shares_accumulated: float = 0.0
    realized_pnl_accumulated: float = 0.0
    last_exit_attempt_utc: datetime | None = None
    fok_attempted: bool = False
    hedge_leg: HedgeLeg | None = None
    pressure_defence_status: str = "idle"
    pressure_defence_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeEvent:
    timestamp_utc: datetime
    event_type: str
    message: str
    fields: dict[str, Any]
