from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .strategy import EntryDecision, bucket_price, bucket_tte, finite_float


@dataclass(frozen=True)
class MacroPolicyMatch:
    side: str
    key: tuple[str, ...]
    value: float
    support: int
    unique_markets: int
    level: str
    move_z: float
    move_z_bucket: str
    price_bucket: str
    trade_style: str
    bid: float
    ask: float
    mid: float | None
    spread: float | None


@dataclass(frozen=True)
class EntryParameters:
    edge: float
    min_tte: int
    max_tte: int


class MacroLocalPolicy:
    def __init__(self, path: Path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_version = int(payload.get("schema_version", 0))
        if schema_version not in {1, 2}:
            raise ValueError(f"Unsupported macro policy schema: {payload.get('schema_version')}")
        self.path = path
        self.strategy_id = str(payload["strategy_id"])
        self.training_start = str(payload["training_start"])
        self.training_end = str(payload["training_end"])
        self.signal_source = str(payload.get("signal_source") or "coinbase_btc_usd")
        self.settlement_label = str(payload.get("settlement_label") or "unspecified")
        self.entry = dict(payload["entry"])
        profile_path = path.with_name("entry_profiles.json")
        profile_payload = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
        self.entry_overrides = {
            str(key): EntryParameters(
                edge=float(value["edge"]),
                min_tte=int(value["min_tte"]),
                max_tte=int(value["max_tte"]),
            )
            for key, value in profile_payload.get("overrides", {}).items()
        }
        self.session_display_names = {
            str(key): str(value) for key, value in payload.get("session_display_names", {}).items()
        }
        if schema_version == 1:
            branches = {
                "default": {
                    "feature_config": payload["feature_config"],
                    "model_config": payload["model_config"],
                    "tables": payload["tables"],
                    "support_kind": "raw_observations",
                }
            }
            self.branch_selection = {"weekday": "default", "weekend": "default"}
        else:
            branches = dict(payload["policy_branches"])
            self.branch_selection = {
                str(key): str(value) for key, value in payload["branch_selection"].items()
            }
        self.branches = {
            name: self._build_branch(name, branch) for name, branch in branches.items()
        }
        default_branch = self.branches[self.branch_selection.get("weekday", next(iter(self.branches)))]
        self.feature_config = default_branch["feature_config"]
        self.levels = default_branch["levels"]
        self.lookups = default_branch["lookups"]

    @staticmethod
    def _build_branch(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        levels = list(payload["model_config"]["levels"])
        lookups = []
        for level in levels:
            level_name = str(level["name"])
            keys = tuple(str(key) for key in level["keys"])
            lookup = {
                tuple(str(row[key]) for key in keys): row
                for row in payload["tables"][level_name]
            }
            lookups.append((level_name, keys, lookup))
        return {
            "name": name,
            "feature_config": dict(payload["feature_config"]),
            "levels": levels,
            "lookups": lookups,
            "support_kind": str(payload.get("support_kind") or "raw_observations"),
        }

    def _branch(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        day_type = str(snapshot.get("day_type") or "weekday")
        name = self.branch_selection.get(day_type, self.branch_selection.get("weekday"))
        if name not in self.branches:
            raise ValueError(f"No policy branch configured for day type {day_type!r}")
        snapshot["policy_branch"] = name
        return self.branches[name]

    def __len__(self) -> int:
        return sum(
            len(lookup)
            for branch in self.branches.values()
            for _, _, lookup in branch["lookups"]
        )

    def macro_session(self, timestamp_london: Any) -> tuple[str | None, str | None]:
        hour = getattr(timestamp_london, "hour", None)
        if hour is None:
            return None, None
        candidate = self.feature_config.get("candidate", {})
        kind = str(candidate.get("session_kind") or "macro")
        numeric_hour = int(hour)
        if kind == "equal":
            width = int(candidate.get("session_width_hours") or 6)
            start = numeric_hour // width * width
            session_label = f"h{start:02d}_{min(start + width, 24):02d}"
        elif numeric_hour < 8:
            session_label = "h00_08"
        elif numeric_hour < 14:
            session_label = "h08_14"
        else:
            session_label = "h14_24"
        fallback_names = {
            "h00_08": "ASIA", "h08_14": "UK", "h14_24": "US",
            "h00_06": "ASIA", "h06_12": "EUROPE",
            "h12_18": "US_OPEN", "h18_24": "US_LATE",
        }
        return self.session_display_names.get(
            session_label, fallback_names.get(session_label, session_label)
        ), session_label

    def entry_parameters(
        self,
        day_type: str,
        session_label: str,
        trade_style: str,
    ) -> EntryParameters:
        default = EntryParameters(
            edge=float(self.entry[f"{trade_style}_edge"]),
            min_tte=int(self.entry["min_tte"]),
            max_tte=int(self.entry["max_tte"]),
        )
        return self.entry_overrides.get(
            f"{day_type}|{session_label}|{trade_style}", default
        )

    def entry_profile(self, snapshot: dict[str, Any]) -> tuple[
        str, str, EntryParameters, EntryParameters
    ]:
        day_type = str(snapshot.get("day_type") or "weekday")
        session_name, session_label = self.macro_session(snapshot.get("timestamp_london"))
        label = str(session_label or "NA")
        return (
            day_type,
            str(session_name or "NA"),
            self.entry_parameters(day_type, label, "continuation"),
            self.entry_parameters(day_type, label, "mean_reversion"),
        )

    @staticmethod
    def _move_z_bucket(value: float, edges: list[float]) -> str:
        positive = sorted(float(edge) for edge in edges)
        boundaries = [-edge for edge in reversed(positive)] + positive
        return f"z_{bisect.bisect_left(boundaries, value):02d}"

    def _annotate_side_signal(self, snapshot: dict[str, Any], side: str) -> tuple[float, str] | None:
        branch = self._branch(snapshot)
        feature_config = branch["feature_config"]
        day_type = str(snapshot.get("day_type") or "")
        _, session_label = self.macro_session(snapshot.get("timestamp_london"))
        signed_bps = finite_float(snapshot.get("cb_signed_bps_raw"))
        local_rv = finite_float(snapshot.get("local_volatility_1h_bps"))
        elapsed = finite_float(snapshot.get("elapsed_seconds_since_market_start"))
        tte = finite_float(snapshot.get("canonical_tte_seconds"))
        if elapsed is None and tte is not None:
            elapsed = 300.0 - tte
        if None in (session_label, signed_bps, local_rv, elapsed):
            return None
        if local_rv <= 0.0:
            return None
        edge_key = f"{day_type}|{session_label}"
        z_edges = feature_config["z_edges"].get(edge_key)
        if z_edges is None:
            return None
        expected_move = max(0.05, local_rv * math.sqrt(max(1.0, elapsed) / 3600.0))
        aligned_bps = signed_bps if side == "UP" else -signed_bps
        move_z = aligned_bps / expected_move
        move_z_bucket = self._move_z_bucket(move_z, z_edges)
        snapshot[f"{side.lower()}_move_z"] = float(move_z)
        snapshot[f"{side.lower()}_move_z_bucket"] = move_z_bucket
        return float(move_z), move_z_bucket

    def _side_match(self, snapshot: dict[str, Any], side: str) -> MacroPolicyMatch | None:
        branch = self._branch(snapshot)
        feature_config = branch["feature_config"]
        day_type = str(snapshot.get("day_type") or "")
        macro_name, session_label = self.macro_session(snapshot.get("timestamp_london"))
        tte = finite_float(snapshot.get("canonical_tte_seconds"))
        signed_bps = finite_float(snapshot.get("cb_signed_bps_raw"))
        local_rv = finite_float(snapshot.get("local_volatility_1h_bps"))
        bid = finite_float(snapshot.get(f"{side.lower()}_bid"))
        ask = finite_float(snapshot.get(f"{side.lower()}_ask"))
        if None in (macro_name, session_label, tte, signed_bps, local_rv, bid, ask):
            return None
        if local_rv <= 0.0 or ask <= bid:
            return None
        price_bucket = bucket_price(ask)
        tte_bucket = bucket_tte(tte)
        if price_bucket is None or tte_bucket is None:
            return None
        elapsed = max(1.0, 300.0 - tte)
        expected_move = max(0.05, local_rv * math.sqrt(elapsed / 3600.0))
        aligned_bps = signed_bps if side == "UP" else -signed_bps
        move_z = aligned_bps / expected_move
        edge_key = f"{day_type}|{session_label}"
        z_edges = feature_config["z_edges"].get(edge_key)
        if z_edges is None:
            return None
        move_z_bucket = self._move_z_bucket(move_z, z_edges)
        style = "continuation" if aligned_bps > 0.01 else "mean_reversion" if aligned_bps < -0.01 else "neutral"
        fields = {
            "day_type": day_type,
            "seasonal_session": session_label,
            "tte_bucket": tte_bucket,
            "move_z_bucket": move_z_bucket,
            "selected_price_bucket": price_bucket,
        }
        matched_row = None
        matched_level = None
        matched_key: tuple[str, ...] | None = None
        for level, keys, lookup in branch["lookups"]:
            key = tuple(str(fields[name]) for name in keys)
            row = lookup.get(key)
            if row is not None:
                matched_row = row
                matched_level = level
                matched_key = key
        if matched_row is None or matched_level is None or matched_key is None:
            return None
        mid = finite_float(snapshot.get(f"{side.lower()}_mid"))
        spread = finite_float(snapshot.get(f"{side.lower()}_spread"))
        return MacroPolicyMatch(
            side=side,
            key=(day_type, session_label, tte_bucket, move_z_bucket, price_bucket, side),
            value=float(matched_row["regularized_value"]),
            support=int(round(float(matched_row["observations"]))),
            unique_markets=int(matched_row.get("unique_markets") or 0),
            level=matched_level,
            move_z=float(move_z),
            move_z_bucket=move_z_bucket,
            price_bucket=price_bucket,
            trade_style=style,
            bid=float(bid),
            ask=float(ask),
            mid=mid,
            spread=spread,
        )

    def evaluate(self, snapshot: dict[str, Any] | None, *, start_block_seconds: int) -> EntryDecision:
        blockers: list[str] = []
        if snapshot is None:
            return _empty_decision(("NO_SNAPSHOT",))
        macro_name, _ = self.macro_session(snapshot.get("timestamp_london"))
        snapshot["macro_session"] = macro_name
        for side in ("UP", "DOWN"):
            self._annotate_side_signal(snapshot, side)
        tte = finite_float(snapshot.get("canonical_tte_seconds"))
        elapsed = finite_float(snapshot.get("elapsed_seconds_since_market_start"))
        if tte is None:
            blockers.append("TTE_MISSING")
        elif not (float(self.entry["min_tte"]) <= tte <= float(self.entry["max_tte"])):
            blockers.append("TTE_OUTSIDE_ENTRY_RANGE")
        if elapsed is None:
            blockers.append("ELAPSED_SECONDS_MISSING")
        elif elapsed < start_block_seconds:
            blockers.append("WINDOW_START_BLOCK_ACTIVE")
        if finite_float(snapshot.get("cb_signed_bps_raw")) is None:
            blockers.append("CB_SIGNED_BPS_MISSING")
        local_rv = finite_float(snapshot.get("local_volatility_1h_bps"))
        if local_rv is None:
            blockers.append(str(snapshot.get("local_volatility_status") or "LOCAL_VOL_WARMUP"))

        matches: list[tuple[MacroPolicyMatch, float, EntryParameters, bool, bool]] = []
        candidates: list[MacroPolicyMatch] = []
        for side in ("UP", "DOWN"):
            match = self._side_match(snapshot, side)
            if match is None:
                continue
            _, session_label = self.macro_session(snapshot.get("timestamp_london"))
            parameters = (
                self.entry_parameters(
                    str(snapshot.get("day_type") or "weekday"),
                    str(session_label or ""),
                    match.trade_style,
                )
                if match.trade_style != "neutral"
                else EntryParameters(math.inf, int(self.entry["min_tte"]), int(self.entry["max_tte"]))
            )
            side_threshold = parameters.edge
            snapshot[f"{side.lower()}_policy_value"] = match.value
            snapshot[f"{side.lower()}_move_z"] = match.move_z
            snapshot[f"{side.lower()}_move_z_bucket"] = match.move_z_bucket
            limit = min(match.ask + 0.01, float(self.entry["max_price"]))
            price_ok = float(self.entry["min_price"]) <= match.ask <= float(self.entry["max_price"])
            tte_ok = tte is not None and parameters.min_tte <= tte <= parameters.max_tte
            matches.append((match, match.value - match.ask, parameters, price_ok, tte_ok))
            if (
                price_ok and tte_ok
                and match.value - match.ask >= side_threshold - 1e-12
                and match.value - limit >= side_threshold - 1e-12
            ):
                candidates.append(match)
        selected = max(candidates, key=lambda item: (item.value - item.ask, item.value, item.side == "UP"), default=None)
        if selected is None:
            if not blockers:
                blockers.append("POLICY_NO_SIDE_EDGE")
            if matches and not any(item[4] for item in matches):
                blockers.append("TTE_OUTSIDE_STYLE_RANGE")
            best = max(
                matches,
                key=lambda item: (item[3], item[4], item[1], item[0].value, item[0].side == "UP"),
                default=None,
            )
            if best is None:
                return _empty_decision(tuple(dict.fromkeys(blockers)))
            best_match, best_edge, best_parameters, _, _ = best
            snapshot.update({
                "macro_session": self.macro_session(snapshot.get("timestamp_london"))[0],
                "selected_move_z": best_match.move_z,
                "selected_move_z_bucket": best_match.move_z_bucket,
                "selected_price_bucket": best_match.price_bucket,
                "policy_support": best_match.support,
                "policy_unique_markets": best_match.unique_markets,
                "policy_level": best_match.level,
            })
            return EntryDecision(
                viable=False,
                blockers=tuple(dict.fromkeys(blockers)),
                policy_key=best_match.key,
                policy_record=best_match,
                selected_side=best_match.side,
                selected_bid=best_match.bid,
                selected_ask=best_match.ask,
                selected_mid=best_match.mid,
                selected_spread=best_match.spread,
                selected_trade_value=best_match.value,
                selected_policy_edge=best_edge,
                selected_trade_style=best_match.trade_style,
                entry_edge_threshold=None if math.isinf(best_parameters.edge) else best_parameters.edge,
                entry_session_enabled=True,
                threshold_bps=0.0,
            )

        _, session_label = self.macro_session(snapshot.get("timestamp_london"))
        threshold = self.entry_parameters(
            str(snapshot.get("day_type") or "weekday"),
            str(session_label or ""),
            selected.trade_style,
        ).edge
        snapshot.update({
            "macro_session": self.macro_session(snapshot.get("timestamp_london"))[0],
            "selected_move_z": selected.move_z,
            "selected_move_z_bucket": selected.move_z_bucket,
            "selected_price_bucket": selected.price_bucket,
            "policy_support": selected.support,
            "policy_unique_markets": selected.unique_markets,
            "policy_level": selected.level,
        })
        return EntryDecision(
            viable=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
            policy_key=selected.key,
            policy_record=selected,
            selected_side=selected.side,
            selected_bid=selected.bid,
            selected_ask=selected.ask,
            selected_mid=selected.mid,
            selected_spread=selected.spread,
            selected_trade_value=selected.value,
            selected_policy_edge=selected.value - selected.ask,
            selected_trade_style=selected.trade_style,
            entry_edge_threshold=threshold,
            entry_session_enabled=True,
            threshold_bps=0.0,
        )


def _empty_decision(blockers: tuple[str, ...]) -> EntryDecision:
    return EntryDecision(
        viable=False, blockers=blockers, policy_key=None, policy_record=None,
        selected_side=None, selected_bid=None, selected_ask=None, selected_mid=None,
        selected_spread=None, selected_trade_value=None, selected_policy_edge=None,
        selected_trade_style=None, entry_edge_threshold=None,
        entry_session_enabled=None, threshold_bps=0.0,
    )
