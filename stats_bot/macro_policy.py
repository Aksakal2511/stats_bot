from __future__ import annotations

import bisect
import json
import math
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .continuous_surface import PriceBucketSurfaceModel, SurfaceModel
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
    signal_name: str
    price_bucket: str
    trade_style: str
    bid: float
    ask: float
    mid: float | None
    spread: float | None
    stressed_edge: float | None = None
    up_value: float | None = None
    up_edge: float | None = None
    down_value: float | None = None
    down_edge: float | None = None


@dataclass(frozen=True)
class EntryParameters:
    edge: float
    min_tte: int
    max_tte: int


@dataclass(frozen=True)
class TimedEntryOverride:
    day_type: str
    day_group: str
    start_minute: int
    end_minute: int
    label: str
    parameters: dict[str, EntryParameters]


@dataclass(frozen=True)
class DynamicEdgeSelection:
    parameters: EntryParameters
    rule_label: str
    source_family: str
    source_segment: str
    child_segment: str
    fallback_depth: int
    support: int
    edge_pair: str
    contradiction_state: str
    vol_bucket: str
    time_segment: str
    day_group: str


class DynamicEdgeSelector:
    def __init__(self, payload: dict[str, Any], *, default_min_tte: int, default_max_tte: int):
        self.rule_label = str(payload.get("entry_rule_label") or "dynamic_edges")
        self.support_threshold = int(payload.get("support_threshold") or 0)
        self.default_min_tte = int(default_min_tte)
        self.default_max_tte = int(default_max_tte)
        vol = dict(payload.get("vol_q4") or {})
        self.vol_q25 = float(vol["q25"])
        self.vol_q50 = float(vol["q50"])
        self.vol_q75 = float(vol["q75"])
        tables = dict(payload.get("tables") or {})
        self.vol_time_table = self._parse_table(tables.get("day_group_time_vol_q4") or {})
        self.contradiction_vol_table = self._parse_table(tables.get("day_group_time_contradiction_vol_q4") or {})
        self.child_table = self.vol_time_table or self.contradiction_vol_table
        self.contradiction_table = self._parse_table(tables.get("day_group_time_contradiction") or {})
        self.day_time_table = self._parse_table(tables.get("day_group_time") or {})

    @staticmethod
    def _parse_table(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        parsed: dict[str, dict[str, Any]] = {}
        for segment, row in raw.items():
            if not isinstance(row, dict):
                continue
            parsed[str(segment)] = {
                "edge_pair": str(row.get("edge_pair") or ""),
                "continuation": EntryParameters(
                    edge=float(row["continuation_edge"]),
                    min_tte=45,
                    max_tte=265,
                ),
                "mean_reversion": EntryParameters(
                    edge=float(row["mean_reversion_edge"]),
                    min_tte=45,
                    max_tte=265,
                ),
                "support": int(row.get("support") or 0),
            }
        return parsed

    @staticmethod
    def _time_segment(timestamp_london: Any) -> str | None:
        hour = getattr(timestamp_london, "hour", None)
        minute = getattr(timestamp_london, "minute", None)
        if hour is None or minute is None:
            return None
        total = int(hour) * 60 + int(minute)
        if total < 8 * 60:
            return "00_08"
        if total < 14 * 60 + 30:
            return "08_1430"
        if total < 17 * 60:
            return "1430_17"
        if total < 22 * 60:
            return "17_22"
        return "22_24"

    def _vol_bucket(self, local_vol_1h_bps: float | None) -> str | None:
        if local_vol_1h_bps is None:
            return None
        if local_vol_1h_bps <= self.vol_q25:
            return "v0_low"
        if local_vol_1h_bps <= self.vol_q50:
            return "v1_midlow"
        if local_vol_1h_bps <= self.vol_q75:
            return "v2_midhigh"
        return "v3_high"

    @staticmethod
    def _contradiction_state(trade_style: str, selected_ask: float | None) -> str:
        if trade_style == "continuation" and selected_ask is not None and selected_ask <= 0.48 + 1e-12:
            return "cheap_continuation"
        return "normal"

    def select(
        self,
        *,
        day_group: str,
        timestamp_london: Any,
        local_vol_1h_bps: float | None,
        trade_style: str,
        selected_ask: float | None,
    ) -> DynamicEdgeSelection | None:
        if trade_style not in {"continuation", "mean_reversion"}:
            return None
        time_segment = self._time_segment(timestamp_london)
        vol_bucket = self._vol_bucket(local_vol_1h_bps)
        if time_segment is None or vol_bucket is None:
            return None
        contradiction = self._contradiction_state(trade_style, selected_ask)
        if self.vol_time_table:
            child_segment = f"{day_group}|{time_segment}|{vol_bucket}"
            fallback_segments = (
                ("day_group_time_vol_q4", child_segment, self.vol_time_table, True),
                ("day_group_time", f"{day_group}|{time_segment}", self.day_time_table, False),
            )
        else:
            child_segment = f"{day_group}|{time_segment}|{contradiction}|{vol_bucket}"
            fallback_segments = (
                ("day_group_time_contradiction_vol_q4", child_segment, self.contradiction_vol_table, True),
                ("day_group_time_contradiction", f"{day_group}|{time_segment}|{contradiction}", self.contradiction_table, False),
                ("day_group_time", f"{day_group}|{time_segment}", self.day_time_table, False),
            )
        for depth, (family, segment, table, require_support) in enumerate(fallback_segments):
            row = table.get(segment)
            if row is None:
                continue
            support = int(row.get("support") or 0)
            if require_support and support < self.support_threshold:
                continue
            parameters = row.get(trade_style)
            if not isinstance(parameters, EntryParameters):
                continue
            return DynamicEdgeSelection(
                parameters=EntryParameters(
                    edge=parameters.edge,
                    min_tte=self.default_min_tte,
                    max_tte=self.default_max_tte,
                ),
                rule_label=self.rule_label,
                source_family=family,
                source_segment=segment,
                child_segment=child_segment,
                fallback_depth=depth,
                support=support,
                edge_pair=str(row.get("edge_pair") or ""),
                contradiction_state=contradiction,
                vol_bucket=vol_bucket,
                time_segment=time_segment,
                day_group=day_group,
            )
        return None


class MacroLocalPolicy:
    def __init__(self, path: Path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_version = int(payload.get("schema_version", 0))
        if schema_version not in {1, 2, 3}:
            raise ValueError(f"Unsupported macro policy schema: {payload.get('schema_version')}")
        self.path = path
        self.schema_version = schema_version
        self.policy_type = str(payload.get("policy_type") or "bucket_lookup")
        self.strategy_id = str(payload["strategy_id"])
        self.training_start = str(payload["training_start"])
        self.training_end = str(payload["training_end"])
        self.signal_source = str(payload.get("signal_source") or "coinbase_btc_usd")
        self.signal_feature = str(payload.get("signal_feature") or "move_z").strip().lower()
        self.signal_price_mode = str(payload.get("signal_price_mode") or "spot").strip().lower()
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
        self.price_bucket_entry_edges = self._parse_price_bucket_entry_edges(profile_payload)
        self.dynamic_edge_selector = self._parse_dynamic_edge_selector(profile_payload, path)
        self.timed_entry_overrides = [
            self._parse_timed_entry_override(row)
            for row in profile_payload.get("timed_overrides", [])
        ]
        self.session_display_names = {
            str(key): str(value) for key, value in payload.get("session_display_names", {}).items()
        }
        self.surface_model: SurfaceModel | PriceBucketSurfaceModel | None = None
        self.surface_model_branches: dict[str, dict[str, Any]] = {}
        self.surface_day_type_mode = "included"
        self.surface_side_mode = "pooled"
        self.surface_model_label = ""
        self.surface_policy_level = ""
        self.surface_model_rows = 0
        if schema_version == 3:
            self._init_surface(payload)
            return
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
        if self.signal_feature not in {"move_z", "move_dz"}:
            raise ValueError(f"Unsupported macro policy signal_feature: {self.signal_feature!r}")

    def _load_surface_model(self, model_cfg: dict[str, Any]) -> SurfaceModel | PriceBucketSurfaceModel:
        artifact = Path(str(model_cfg.get("artifact") or ""))
        if not artifact.is_absolute():
            artifact = self.path.parent / artifact
        with artifact.open("rb") as handle:
            model = pickle.load(handle)
        if not isinstance(model, (SurfaceModel, PriceBucketSurfaceModel)):
            raise TypeError(f"Unsupported surface model artifact type: {type(model)!r}")
        return model

    def _init_surface(self, payload: dict[str, Any]) -> None:
        if self.signal_feature != "move_dz":
            raise ValueError("Continuous surface policies currently require signal_feature='move_dz'")
        branch_cfgs = payload.get("model_branches")
        if isinstance(branch_cfgs, dict) and branch_cfgs:
            total_rows = 0
            first_model: SurfaceModel | PriceBucketSurfaceModel | None = None
            for name, raw_cfg in branch_cfgs.items():
                cfg = dict(raw_cfg or {})
                model = self._load_surface_model(cfg)
                if first_model is None:
                    first_model = model
                rows = int(cfg.get("training_rows") or model.rows or 0)
                total_rows += rows
                self.surface_model_branches[str(name)] = {
                    "model": model,
                    "day_type_mode": str(cfg.get("day_type_mode") or "excluded"),
                    "side_mode": str(cfg.get("side_mode") or model.side_mode or "pooled"),
                    "label": str(cfg.get("label") or model.model_kind),
                    "policy_level": str(cfg.get("policy_level") or f"{name}|POOLED"),
                    "training_rows": rows,
                }
            self.surface_model = first_model
            model_cfg = dict(payload.get("model") or {})
            self.surface_day_type_mode = str(model_cfg.get("day_type_mode") or "hybrid")
            self.surface_side_mode = str(model_cfg.get("side_mode") or "pooled")
            self.surface_model_label = str(model_cfg.get("label") or "hybrid_surface")
            self.surface_policy_level = str(model_cfg.get("policy_level") or "hybrid|POOLED")
            self.surface_model_rows = int(model_cfg.get("training_rows") or total_rows)
        else:
            model_cfg = dict(payload.get("model") or {})
            model = self._load_surface_model(model_cfg)
            self.surface_model = model
            self.surface_day_type_mode = str(model_cfg.get("day_type_mode") or "excluded")
            self.surface_side_mode = str(model_cfg.get("side_mode") or model.side_mode or "pooled")
            self.surface_model_label = str(model_cfg.get("label") or model.model_kind)
            self.surface_policy_level = str(model_cfg.get("policy_level") or "ALL|POOLED")
            self.surface_model_rows = int(model_cfg.get("training_rows") or model.rows or 0)
        self.feature_config = {
            "candidate": {
                "session_kind": "equal",
                "session_width_hours": int(payload.get("display_session_width_hours") or 4),
            }
        }
        self.levels = []
        self.lookups = []
        self.branches = {}
        self.branch_selection = {"weekday": "surface", "weekend": "surface"}

    def _surface_branch_config_for_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if not self.surface_model_branches:
            if self.surface_model is None:
                return None
            return {
                "model": self.surface_model,
                "day_type_mode": self.surface_day_type_mode,
                "side_mode": self.surface_side_mode,
                "label": self.surface_model_label,
                "policy_level": self.surface_policy_level,
                "training_rows": self.surface_model_rows,
            }
        day_type = str(snapshot.get("day_type") or "weekday").strip().lower()
        branch_name = "weekend" if day_type == "weekend" else "weekday"
        return (
            self.surface_model_branches.get(branch_name)
            or self.surface_model_branches.get(day_type)
            or next(iter(self.surface_model_branches.values()))
        )

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
        if self.schema_version == 3:
            branch_cfg = self._surface_branch_config_for_snapshot(snapshot)
            branch_name = str(branch_cfg.get("policy_level") or self.surface_policy_level or "surface") if branch_cfg else "surface"
            snapshot["policy_branch"] = branch_name
            return {
                "name": branch_name,
                "feature_config": self.feature_config,
                "levels": [],
                "lookups": [],
                "support_kind": "weighted_training_rows",
            }
        day_type = str(snapshot.get("day_type") or "weekday")
        name = self.branch_selection.get(day_type, self.branch_selection.get("weekday"))
        if name not in self.branches:
            raise ValueError(f"No policy branch configured for day type {day_type!r}")
        snapshot["policy_branch"] = name
        return self.branches[name]

    def __len__(self) -> int:
        if self.schema_version == 3:
            if self.surface_model_branches:
                return len(self.surface_model_branches)
            return 1 if self.surface_model is not None else 0
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
            wrapped_label = f"h{start:02d}_00"
            z_edges = self.feature_config.get("z_edges", {})
            has_wrapped_key = (
                wrapped_label in self.session_display_names
                or wrapped_label in z_edges
                or any(str(key).endswith(f"|{wrapped_label}") for key in z_edges)
            )
            if start + width >= 24 and has_wrapped_key:
                session_label = wrapped_label
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
        default = self._default_entry_parameters(trade_style)
        return self.entry_overrides.get(
            f"{day_type}|{session_label}|{trade_style}", default
        )

    def _default_entry_parameters(self, trade_style: str) -> EntryParameters:
        return EntryParameters(
            edge=float(self.entry[f"{trade_style}_edge"]),
            min_tte=int(self.entry["min_tte"]),
            max_tte=int(self.entry["max_tte"]),
        )

    def _parse_price_bucket_entry_edges(
        self,
        profile_payload: dict[str, Any],
    ) -> dict[str, dict[str, EntryParameters]]:
        raw = profile_payload.get("price_bucket_edges")
        if not isinstance(raw, dict):
            return {}
        default_min_tte = int(profile_payload.get("default", {}).get("min_tte", self.entry["min_tte"]))
        default_max_tte = int(profile_payload.get("default", {}).get("max_tte", self.entry["max_tte"]))
        parsed: dict[str, dict[str, EntryParameters]] = {}
        for style in ("continuation", "mean_reversion"):
            style_raw = raw.get(style)
            if not isinstance(style_raw, dict):
                continue
            parsed_style: dict[str, EntryParameters] = {}
            for bucket, value in style_raw.items():
                bucket_label = str(bucket)
                min_tte = default_min_tte
                max_tte = default_max_tte
                edge_value: float
                if value is None:
                    edge_value = math.inf
                elif isinstance(value, dict):
                    edge = value.get("edge")
                    edge_value = math.inf if edge is None else float(edge)
                    min_tte = int(value.get("min_tte", min_tte))
                    max_tte = int(value.get("max_tte", max_tte))
                else:
                    edge_value = float(value)
                parsed_style[bucket_label] = EntryParameters(
                    edge=edge_value,
                    min_tte=min_tte,
                    max_tte=max_tte,
                )
            parsed[style] = parsed_style
        return parsed

    def _parse_dynamic_edge_selector(
        self,
        profile_payload: dict[str, Any],
        manifest_path: Path,
    ) -> DynamicEdgeSelector | None:
        raw = profile_payload.get("dynamic_edge_selector")
        if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
            return None
        artifact = Path(str(raw.get("artifact") or ""))
        if not artifact:
            raise ValueError("dynamic_edge_selector requires an artifact path")
        if not artifact.is_absolute():
            artifact = manifest_path.parent / artifact
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        default_min_tte = int(profile_payload.get("default", {}).get("min_tte", self.entry["min_tte"]))
        default_max_tte = int(profile_payload.get("default", {}).get("max_tte", self.entry["max_tte"]))
        return DynamicEdgeSelector(
            payload,
            default_min_tte=default_min_tte,
            default_max_tte=default_max_tte,
        )

    def _price_bucket_entry_parameters(
        self,
        trade_style: str,
        price_bucket: str | None,
    ) -> EntryParameters | None:
        if not price_bucket:
            return None
        style_edges = self.price_bucket_entry_edges.get(trade_style)
        if not style_edges:
            return None
        return style_edges.get(str(price_bucket))

    def _enabled_price_buckets(self, trade_style: str) -> list[str]:
        style_edges = self.price_bucket_entry_edges.get(trade_style) or {}
        return [
            bucket
            for bucket, parameters in style_edges.items()
            if math.isfinite(parameters.edge)
        ]

    @staticmethod
    def _parse_time_minutes(value: Any) -> int:
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            minutes = int(value)
        else:
            text = str(value or "").strip()
            if not text:
                raise ValueError("Timed entry override is missing a start/end time")
            if ":" in text:
                hour_text, minute_text = text.split(":", 1)
                minutes = int(hour_text) * 60 + int(minute_text)
            elif len(text) in {3, 4} and text.isdigit():
                minutes = int(text[:-2]) * 60 + int(text[-2:])
            else:
                minutes = int(text) * 60
        if not 0 <= minutes <= 24 * 60:
            raise ValueError(f"Timed entry override time is outside 00:00-24:00: {value!r}")
        return minutes

    @classmethod
    def _parse_timed_entry_override(cls, row: dict[str, Any]) -> TimedEntryOverride:
        day_type = str(row.get("day_type") or "").strip().lower()
        if day_type not in {"weekday", "weekend"}:
            raise ValueError(f"Timed entry override has invalid day_type: {row.get('day_type')!r}")
        day_group = str(row.get("day_group") or day_type).strip().lower()
        valid_groups = {"weekday", "weekend", "mon_wed", "thu_fri"}
        if day_group not in valid_groups:
            raise ValueError(f"Timed entry override has invalid day_group: {row.get('day_group')!r}")
        if day_group in {"mon_wed", "thu_fri"} and day_type != "weekday":
            raise ValueError(f"Timed entry override day_group requires weekday day_type: {row!r}")
        if day_group == "weekend" and day_type != "weekend":
            raise ValueError(f"Timed entry override weekend day_group requires weekend day_type: {row!r}")
        start_minute = cls._parse_time_minutes(row.get("start"))
        end_minute = cls._parse_time_minutes(row.get("end"))
        if start_minute == end_minute:
            raise ValueError(f"Timed entry override has empty range: {row!r}")
        label = str(row.get("label") or f"{start_minute:04d}_{end_minute:04d}").strip()
        min_tte = int(row.get("min_tte"))
        max_tte = int(row.get("max_tte"))
        parameters = {
            "continuation": EntryParameters(
                edge=float(row["continuation_edge"]),
                min_tte=min_tte,
                max_tte=max_tte,
            ),
            "mean_reversion": EntryParameters(
                edge=float(row["mean_reversion_edge"]),
                min_tte=min_tte,
                max_tte=max_tte,
            ),
        }
        return TimedEntryOverride(
            day_type=day_type,
            day_group=day_group,
            start_minute=start_minute,
            end_minute=end_minute,
            label=label,
            parameters=parameters,
        )

    @staticmethod
    def _time_in_minutes(timestamp_london: Any) -> int | None:
        hour = getattr(timestamp_london, "hour", None)
        minute = getattr(timestamp_london, "minute", None)
        if hour is None or minute is None:
            return None
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _entry_day_group(day_type: str, timestamp_london: Any) -> str:
        normalized_day_type = str(day_type or "").strip().lower()
        if normalized_day_type == "weekend":
            return "weekend"
        weekday = getattr(timestamp_london, "weekday", None)
        if callable(weekday):
            day_index = int(weekday())
            if 0 <= day_index <= 2:
                return "mon_wed"
            if 3 <= day_index <= 4:
                return "thu_fri"
            return "weekend"
        return normalized_day_type or "weekday"

    def _timed_entry_override(
        self,
        day_type: str,
        timestamp_london: Any,
    ) -> TimedEntryOverride | None:
        minute = self._time_in_minutes(timestamp_london)
        if minute is None:
            return None
        normalized_day_type = str(day_type or "").strip().lower()
        normalized_day_group = self._entry_day_group(normalized_day_type, timestamp_london)
        for override in self.timed_entry_overrides:
            if override.day_type != normalized_day_type:
                continue
            if override.day_group != normalized_day_group and override.day_group != normalized_day_type:
                continue
            if override.start_minute < override.end_minute:
                if override.start_minute <= minute < override.end_minute:
                    return override
            elif minute >= override.start_minute or minute < override.end_minute:
                return override
        return None

    def _dynamic_edge_selection_for_snapshot(
        self,
        snapshot: dict[str, Any],
        trade_style: str,
        selected_ask: float | None,
    ) -> DynamicEdgeSelection | None:
        selector = self.dynamic_edge_selector
        if selector is None:
            return None
        day_type = str(snapshot.get("day_type") or "weekday")
        day_group = self._entry_day_group(day_type, snapshot.get("timestamp_london"))
        local_vol = finite_float(snapshot.get("local_volatility_1h_bps"))
        return selector.select(
            day_group=day_group,
            timestamp_london=snapshot.get("timestamp_london"),
            local_vol_1h_bps=local_vol,
            trade_style=trade_style,
            selected_ask=selected_ask,
        )

    def _entry_parameters_and_rule_for_snapshot(
        self,
        snapshot: dict[str, Any],
        trade_style: str,
        price_bucket: str | None = None,
        selected_ask: float | None = None,
    ) -> tuple[EntryParameters, DynamicEdgeSelection | None]:
        dynamic_selection = self._dynamic_edge_selection_for_snapshot(snapshot, trade_style, selected_ask)
        if dynamic_selection is not None:
            return dynamic_selection.parameters, dynamic_selection
        bucket_parameters = self._price_bucket_entry_parameters(trade_style, price_bucket)
        if bucket_parameters is not None:
            return bucket_parameters, None
        day_type = str(snapshot.get("day_type") or "weekday")
        timed = self._timed_entry_override(day_type, snapshot.get("timestamp_london"))
        if timed is not None and trade_style in timed.parameters:
            return timed.parameters[trade_style], None
        _, session_label = self.macro_session(snapshot.get("timestamp_london"))
        return self.entry_parameters(day_type, str(session_label or ""), trade_style), None

    def _entry_parameters_for_snapshot(
        self,
        snapshot: dict[str, Any],
        trade_style: str,
        price_bucket: str | None = None,
        selected_ask: float | None = None,
    ) -> EntryParameters:
        parameters, _ = self._entry_parameters_and_rule_for_snapshot(
            snapshot,
            trade_style,
            price_bucket,
            selected_ask,
        )
        return parameters

    def _store_active_entry_rule(
        self,
        snapshot: dict[str, Any],
        match: MacroPolicyMatch,
        parameters: EntryParameters,
        dynamic_selection: DynamicEdgeSelection | None,
    ) -> None:
        if dynamic_selection is None:
            if self.dynamic_edge_selector is not None:
                rule_name = self.dynamic_edge_selector.rule_label
            elif self.price_bucket_entry_edges:
                rule_name = "price_bucket_edges"
            else:
                rule_name = str(self.entry.get("edge_profile") or "global_edges")
            snapshot["entry_rule_name"] = (
                rule_name
            )
            snapshot["entry_rule_source_family"] = ""
            snapshot["entry_rule_source_segment"] = ""
            snapshot["entry_rule_child_segment"] = ""
            snapshot["entry_rule_support"] = ""
            snapshot["entry_rule_fallback_depth"] = ""
            snapshot["entry_rule_edge_pair"] = ""
            snapshot["entry_rule_contradiction_state"] = ""
            snapshot["entry_rule_vol_bucket"] = ""
            snapshot["entry_rule_time_segment"] = ""
        else:
            snapshot["entry_rule_name"] = dynamic_selection.rule_label
            snapshot["entry_rule_source_family"] = dynamic_selection.source_family
            snapshot["entry_rule_source_segment"] = dynamic_selection.source_segment
            snapshot["entry_rule_child_segment"] = dynamic_selection.child_segment
            snapshot["entry_rule_support"] = dynamic_selection.support
            snapshot["entry_rule_fallback_depth"] = dynamic_selection.fallback_depth
            snapshot["entry_rule_edge_pair"] = dynamic_selection.edge_pair
            snapshot["entry_rule_contradiction_state"] = dynamic_selection.contradiction_state
            snapshot["entry_rule_vol_bucket"] = dynamic_selection.vol_bucket
            snapshot["entry_rule_time_segment"] = dynamic_selection.time_segment
        snapshot["entry_rule_min_tte"] = parameters.min_tte
        snapshot["entry_rule_max_tte"] = parameters.max_tte
        snapshot["active_edge_trade_style"] = match.trade_style
        snapshot["active_edge_price_bucket"] = match.price_bucket
        snapshot["active_edge_cents"] = (
            round(float(parameters.edge) * 100.0)
            if math.isfinite(float(parameters.edge))
            else ""
        )

    def _display_entry_parameters_for_snapshot(
        self,
        snapshot: dict[str, Any],
        trade_style: str,
    ) -> EntryParameters:
        selected_bucket = snapshot.get("selected_price_bucket")
        selected_style = snapshot.get("selected_trade_style")
        bucket: str | None = None
        if selected_style == trade_style and selected_bucket:
            bucket = str(selected_bucket)
        elif self.price_bucket_entry_edges:
            enabled = self._enabled_price_buckets(trade_style)
            if len(enabled) == 1:
                bucket = enabled[0]
            elif selected_bucket and str(selected_bucket) in (self.price_bucket_entry_edges.get(trade_style) or {}):
                bucket = str(selected_bucket)
        bucket_parameters = self._price_bucket_entry_parameters(trade_style, bucket)
        if bucket_parameters is not None:
            return bucket_parameters
        if self.price_bucket_entry_edges:
            return self._default_entry_parameters(trade_style)
        return self._entry_parameters_for_snapshot(snapshot, trade_style)

    def entry_profile(self, snapshot: dict[str, Any]) -> tuple[
        str, str, EntryParameters, EntryParameters
    ]:
        day_type = str(snapshot.get("day_type") or "weekday")
        session_name, session_label = self.macro_session(snapshot.get("timestamp_london"))
        label = str(session_label or "NA")
        timed = self._timed_entry_override(day_type, snapshot.get("timestamp_london"))
        if timed is not None:
            return (
                day_type,
                timed.label,
                self._display_entry_parameters_for_snapshot(snapshot, "continuation"),
                self._display_entry_parameters_for_snapshot(snapshot, "mean_reversion"),
            )
        return (
            day_type,
            str(session_name or "NA"),
            self._display_entry_parameters_for_snapshot(snapshot, "continuation"),
            self._display_entry_parameters_for_snapshot(snapshot, "mean_reversion"),
        )

    @staticmethod
    def _move_z_bucket(value: float, edges: list[float]) -> str:
        positive = sorted(float(edge) for edge in edges)
        boundaries = [-edge for edge in reversed(positive)] + positive
        return f"z_{bisect.bisect_left(boundaries, value):02d}"

    @staticmethod
    def _first_finite(snapshot: dict[str, Any], *names: str) -> float | None:
        for name in names:
            value = finite_float(snapshot.get(name))
            if value is not None:
                return value
        return None

    @staticmethod
    def _z_edges(feature_config: dict[str, Any], day_type: str, session_label: str) -> list[float] | None:
        edges_by_key = feature_config.get("z_edges", {})
        return (
            edges_by_key.get(f"{day_type}|{session_label}")
            or edges_by_key.get(session_label)
            or edges_by_key.get(str(day_type))
            or edges_by_key.get("GLOBAL")
        )

    def _side_signal_value(self, snapshot: dict[str, Any], side: str) -> tuple[float, float] | None:
        local_rv = finite_float(snapshot.get("local_volatility_1h_bps"))
        if local_rv is None or local_rv <= 0.0:
            return None
        tte = finite_float(snapshot.get("canonical_tte_seconds"))
        elapsed = finite_float(snapshot.get("elapsed_seconds_since_market_start"))
        if elapsed is None and tte is not None:
            elapsed = 300.0 - tte

        if self.signal_feature == "move_dz":
            signal_price_mode = getattr(self, "signal_price_mode", "spot")
            if signal_price_mode in {
                "analytic_twap_pre_w30s",
                "coinbase_analytic_twap_pre_w30s",
                "coinbase_twap_analytic_pre_w30s",
                "analytic_twap_pre_w60s",
                "coinbase_analytic_twap_pre_w60s",
                "coinbase_twap_analytic_pre_w60s",
            }:
                side_key = side.lower()
                window_suffix = "w60s" if "w60" in signal_price_mode else "w30s"
                move_dz = finite_float(snapshot.get(f"{side_key}_analytic_twap_pre_z_{window_suffix}"))
                if move_dz is None:
                    base_z = finite_float(snapshot.get(f"analytic_twap_pre_z_{window_suffix}"))
                    if base_z is not None:
                        move_dz = base_z if side == "UP" else -base_z
                signal_bps = self._first_finite(snapshot, "cb_signed_bps_raw", "cb_spot_signed_bps_raw")
                if move_dz is None or signal_bps is None:
                    return None
                aligned_bps = float(signal_bps) if side == "UP" else -float(signal_bps)
                return float(move_dz), float(aligned_bps)
            close = self._first_finite(snapshot, "cb_signal_price", "cb_close_1s")
            anchor = self._first_finite(
                snapshot,
                "cb_price_to_beat_anchor",
                "price_to_beat",
                "window_open_btc_price",
            )
            if None in (close, anchor, tte) or anchor is None or anchor <= 0.0:
                return None
            distance_bps = 10_000.0 * (float(close) - float(anchor)) / float(anchor)
            remaining_vol = max(0.05, local_rv * math.sqrt(max(1.0, float(tte)) / 3600.0))
            aligned_bps = distance_bps if side == "UP" else -distance_bps
            return float(aligned_bps / remaining_vol), float(aligned_bps)

        signed_bps = finite_float(snapshot.get("cb_signed_bps_raw"))
        if None in (signed_bps, elapsed):
            return None
        expected_move = max(0.05, local_rv * math.sqrt(max(1.0, float(elapsed)) / 3600.0))
        aligned_bps = signed_bps if side == "UP" else -signed_bps
        return float(aligned_bps / expected_move), float(aligned_bps)

    def _annotate_side_signal(self, snapshot: dict[str, Any], side: str) -> tuple[float, str] | None:
        if self.schema_version == 3:
            signal = self._side_signal_value(snapshot, side)
            if signal is None:
                return None
            signal_value, _ = signal
            side_key = side.lower()
            snapshot["policy_signal_name"] = self.signal_feature
            snapshot[f"{side_key}_{self.signal_feature}"] = float(signal_value)
            snapshot[f"{side_key}_{self.signal_feature}_bucket"] = "continuous"
            snapshot[f"{side_key}_move_z"] = float(signal_value)
            snapshot[f"{side_key}_move_z_bucket"] = "continuous"
            return float(signal_value), "continuous"
        branch = self._branch(snapshot)
        feature_config = branch["feature_config"]
        day_type = str(snapshot.get("day_type") or "")
        _, session_label = self.macro_session(snapshot.get("timestamp_london"))
        if session_label is None:
            return None
        z_edges = self._z_edges(feature_config, day_type, session_label)
        if z_edges is None:
            return None
        signal = self._side_signal_value(snapshot, side)
        if signal is None:
            return None
        signal_value, _ = signal
        move_z_bucket = self._move_z_bucket(signal_value, z_edges)
        side_key = side.lower()
        snapshot["policy_signal_name"] = self.signal_feature
        snapshot[f"{side_key}_{self.signal_feature}"] = float(signal_value)
        snapshot[f"{side_key}_{self.signal_feature}_bucket"] = move_z_bucket
        snapshot[f"{side_key}_move_z"] = float(signal_value)
        snapshot[f"{side_key}_move_z_bucket"] = move_z_bucket
        return float(signal_value), move_z_bucket

    def _side_match(self, snapshot: dict[str, Any], side: str) -> MacroPolicyMatch | None:
        if self.schema_version == 3:
            return self._surface_side_match(snapshot, side)
        branch = self._branch(snapshot)
        feature_config = branch["feature_config"]
        day_type = str(snapshot.get("day_type") or "")
        macro_name, session_label = self.macro_session(snapshot.get("timestamp_london"))
        tte = finite_float(snapshot.get("canonical_tte_seconds"))
        bid = finite_float(snapshot.get(f"{side.lower()}_bid"))
        ask = finite_float(snapshot.get(f"{side.lower()}_ask"))
        if None in (macro_name, session_label, bid, ask):
            return None
        if ask <= bid:
            return None
        price_bucket = bucket_price(ask)
        uses_tte_bucket = any("tte_bucket" in keys for _, keys, _ in branch["lookups"])
        tte_bucket = bucket_tte(tte) if tte is not None else None
        if price_bucket is None or (uses_tte_bucket and tte_bucket is None):
            return None
        signal = self._side_signal_value(snapshot, side)
        if signal is None:
            return None
        move_z, aligned_bps = signal
        z_edges = self._z_edges(feature_config, day_type, session_label)
        if z_edges is None:
            return None
        move_z_bucket = self._move_z_bucket(move_z, z_edges)
        style_epsilon = 0.01 if self.signal_feature == "move_z" else 1e-12
        style = "continuation" if aligned_bps > style_epsilon else "mean_reversion" if aligned_bps < -style_epsilon else "neutral"
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
            key=tuple(str(part) for part in (*matched_key, side)),
            value=float(matched_row["regularized_value"]),
            support=int(round(float(matched_row["observations"]))),
            unique_markets=int(matched_row.get("unique_markets") or 0),
            level=matched_level,
            move_z=float(move_z),
            move_z_bucket=move_z_bucket,
            signal_name=self.signal_feature,
            price_bucket=price_bucket,
            trade_style=style,
            bid=float(bid),
            ask=float(ask),
            mid=mid,
            spread=spread,
        )

    def _surface_side_match(self, snapshot: dict[str, Any], side: str) -> MacroPolicyMatch | None:
        branch_cfg = self._surface_branch_config_for_snapshot(snapshot)
        if branch_cfg is None:
            return None
        model = branch_cfg.get("model")
        if model is None or not callable(getattr(model, "predict_one", None)):
            return None
        bid = finite_float(snapshot.get(f"{side.lower()}_bid"))
        ask = finite_float(snapshot.get(f"{side.lower()}_ask"))
        if bid is None or ask is None or ask <= bid:
            return None
        signal = self._side_signal_value(snapshot, side)
        if signal is None:
            return None
        move_dz, aligned_bps = signal
        momentum_feature = str(getattr(model, "momentum_feature", "") or "")
        momentum_value: float | None = None
        if momentum_feature:
            momentum_value = finite_float(snapshot.get(f"{side.lower()}_{momentum_feature}"))
            if momentum_value is None:
                return None
        value = model.predict_one(
            move_dz=move_dz,
            selected_ask=ask,
            side=side,
            side_dz_delta_2s=momentum_value,
            timestamp_utc=snapshot.get("timestamp_utc"),
            timestamp_london=snapshot.get("timestamp_london"),
        )
        if value is None:
            return None
        style = "continuation" if aligned_bps > 1e-12 else "mean_reversion" if aligned_bps < -1e-12 else "neutral"
        mid = finite_float(snapshot.get(f"{side.lower()}_mid"))
        spread = finite_float(snapshot.get(f"{side.lower()}_spread"))
        limit = min(float(ask) + 0.01, float(self.entry["max_price"]))
        price_bucket = "continuous"
        bucket_label = getattr(model, "bucket_label", None)
        if callable(bucket_label):
            price_bucket = str(bucket_label(ask) or "NA")
        model_label = str(branch_cfg.get("label") or self.surface_model_label or "surface")
        policy_level = str(branch_cfg.get("policy_level") or self.surface_policy_level or "surface")
        model_rows = int(branch_cfg.get("training_rows") or self.surface_model_rows or 0)
        snapshot["policy_branch"] = policy_level
        return MacroPolicyMatch(
            side=side,
            key=(
                self.strategy_id,
                model_label,
                f"{policy_level}|{price_bucket}",
                side,
            ),
            value=float(value),
            support=model_rows,
            unique_markets=int(self.entry.get("unique_markets") or 0),
            level="surface",
            move_z=float(move_dz),
            move_z_bucket="continuous",
            signal_name="move_dz",
            price_bucket=price_bucket,
            trade_style=style,
            bid=float(bid),
            ask=float(ask),
            mid=mid,
            spread=spread,
            stressed_edge=float(value) - float(limit),
        )

    def evaluate(self, snapshot: dict[str, Any] | None, *, start_block_seconds: int) -> EntryDecision:
        blockers: list[str] = []
        if snapshot is None:
            return _empty_decision(("NO_SNAPSHOT",))
        snapshot["policy_signal_name"] = self.signal_feature
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
        if self.signal_feature == "move_z" and finite_float(snapshot.get("cb_signed_bps_raw")) is None:
            blockers.append("CB_SIGNED_BPS_MISSING")
        if self.signal_feature == "move_dz":
            if self._first_finite(snapshot, "cb_signal_price", "cb_close_1s") is None:
                blockers.append("BTC_CLOSE_MISSING")
            if self._first_finite(snapshot, "cb_price_to_beat_anchor", "price_to_beat", "window_open_btc_price") is None:
                blockers.append("PRICE_TO_BEAT_MISSING")
        local_rv = finite_float(snapshot.get("local_volatility_1h_bps"))
        if local_rv is None:
            blockers.append(str(snapshot.get("local_volatility_status") or "LOCAL_VOL_WARMUP"))

        matches: list[tuple[MacroPolicyMatch, float, EntryParameters, bool, bool]] = []
        candidates: list[MacroPolicyMatch] = []
        for side in ("UP", "DOWN"):
            match = self._side_match(snapshot, side)
            if match is None:
                continue
            if match.trade_style != "neutral":
                parameters, dynamic_selection = self._entry_parameters_and_rule_for_snapshot(
                    snapshot,
                    match.trade_style,
                    match.price_bucket,
                    match.ask,
                )
            else:
                parameters = EntryParameters(math.inf, int(self.entry["min_tte"]), int(self.entry["max_tte"]))
                dynamic_selection = None
            side_threshold = parameters.edge
            if dynamic_selection is not None:
                side_entry_rule_name = dynamic_selection.rule_label
            elif self.price_bucket_entry_edges:
                side_entry_rule_name = "price_bucket_edges"
            else:
                side_entry_rule_name = str(self.entry.get("edge_profile") or "global_edges")
            snapshot[f"{side.lower()}_entry_rule_name"] = (
                side_entry_rule_name
            )
            snapshot[f"{side.lower()}_entry_rule_source_family"] = (
                dynamic_selection.source_family if dynamic_selection is not None else ""
            )
            snapshot[f"{side.lower()}_entry_rule_source_segment"] = (
                dynamic_selection.source_segment if dynamic_selection is not None else ""
            )
            snapshot[f"{side.lower()}_policy_value"] = match.value
            snapshot[f"{side.lower()}_policy_edge"] = match.value - match.ask
            snapshot[f"{side.lower()}_policy_stressed_edge"] = match.stressed_edge
            snapshot[f"{side.lower()}_{match.signal_name}"] = match.move_z
            snapshot[f"{side.lower()}_{match.signal_name}_bucket"] = match.move_z_bucket
            snapshot[f"{side.lower()}_move_z"] = match.move_z
            snapshot[f"{side.lower()}_move_z_bucket"] = match.move_z_bucket
            branch_cfg = self._surface_branch_config_for_snapshot(snapshot)
            branch_model = branch_cfg.get("model") if branch_cfg else None
            momentum_feature = str(getattr(branch_model, "momentum_feature", "") or "")
            if momentum_feature:
                momentum_value = finite_float(snapshot.get(f"{side.lower()}_{momentum_feature}"))
                if momentum_value is not None:
                    snapshot[f"candidate_{side.lower()}_{momentum_feature}"] = momentum_value
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
                "policy_signal_name": best_match.signal_name,
                f"selected_{best_match.signal_name}": best_match.move_z,
                f"selected_{best_match.signal_name}_bucket": best_match.move_z_bucket,
                "selected_move_z": best_match.move_z,
                "selected_move_z_bucket": best_match.move_z_bucket,
                "selected_price_bucket": best_match.price_bucket,
                "selected_trade_style": best_match.trade_style,
                "policy_support": best_match.support,
                "policy_unique_markets": best_match.unique_markets,
                "policy_level": best_match.level,
                "selected_policy_stressed_edge": best_match.stressed_edge,
            })
            best_dynamic_selection = self._dynamic_edge_selection_for_snapshot(
                snapshot,
                best_match.trade_style,
                best_match.ask,
            )
            self._store_active_entry_rule(snapshot, best_match, best_parameters, best_dynamic_selection)
            branch_cfg = self._surface_branch_config_for_snapshot(snapshot)
            branch_model = branch_cfg.get("model") if branch_cfg else None
            momentum_feature = str(getattr(branch_model, "momentum_feature", "") or "")
            if momentum_feature:
                momentum_value = finite_float(snapshot.get(f"{best_match.side.lower()}_{momentum_feature}"))
                if momentum_value is not None:
                    snapshot[f"selected_{momentum_feature}"] = momentum_value
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

        selected_parameters, selected_dynamic_selection = self._entry_parameters_and_rule_for_snapshot(
            snapshot,
            selected.trade_style,
            selected.price_bucket,
            selected.ask,
        )
        threshold = selected_parameters.edge
        snapshot.update({
            "macro_session": self.macro_session(snapshot.get("timestamp_london"))[0],
            "policy_signal_name": selected.signal_name,
            f"selected_{selected.signal_name}": selected.move_z,
            f"selected_{selected.signal_name}_bucket": selected.move_z_bucket,
            "selected_move_z": selected.move_z,
            "selected_move_z_bucket": selected.move_z_bucket,
            "selected_price_bucket": selected.price_bucket,
            "selected_trade_style": selected.trade_style,
            "policy_support": selected.support,
            "policy_unique_markets": selected.unique_markets,
            "policy_level": selected.level,
            "selected_policy_stressed_edge": selected.stressed_edge,
        })
        self._store_active_entry_rule(snapshot, selected, selected_parameters, selected_dynamic_selection)
        branch_cfg = self._surface_branch_config_for_snapshot(snapshot)
        branch_model = branch_cfg.get("model") if branch_cfg else None
        momentum_feature = str(getattr(branch_model, "momentum_feature", "") or "")
        if momentum_feature:
            momentum_value = finite_float(snapshot.get(f"{selected.side.lower()}_{momentum_feature}"))
            if momentum_value is not None:
                snapshot[f"selected_{momentum_feature}"] = momentum_value
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
