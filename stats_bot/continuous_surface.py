from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import SplineTransformer, StandardScaler


EPS = 1e-5
ASYMMETRIC_PRICE_BUCKET_EDGES: tuple[tuple[float, float, str], ...] = (
    (0.20, 0.30, "p20_30"),
    (0.30, 0.40, "p30_40"),
    (0.40, 0.45, "p40_45"),
    (0.45, 0.48, "p45_48"),
    (0.48, 0.52, "p48_52"),
    (0.52, 0.55, "p52_55"),
    (0.55, 0.60, "p55_60"),
    (0.60, 0.70, "p60_70"),
    (0.70, 0.80, "p70_80"),
)
PRICE_BUCKET_SCHEMES: dict[str, tuple[tuple[float, float, str], ...]] = {
    "asymmetric": ASYMMETRIC_PRICE_BUCKET_EDGES,
    "current": ASYMMETRIC_PRICE_BUCKET_EDGES,
}
STAGEA_DAY_GROUPS: tuple[str, ...] = ("mon_wed", "thu_fri", "weekend")
STAGEA_TIME_SCHEMES: dict[str, tuple[tuple[float, float, str], ...]] = {
    "current": (
        (0.0, 8.0, "00_08"),
        (8.0, 14.5, "08_1430"),
        (14.5, 17.0, "1430_17"),
        (17.0, 22.0, "17_22"),
        (22.0, 24.0, "22_24"),
    ),
    "nyse_wide": (
        (0.0, 8.0, "00_08"),
        (8.0, 14.5, "08_1430"),
        (14.5, 18.0, "1430_18"),
        (18.0, 24.0, "18_24"),
    ),
}
STAGEA_PRICE_BUCKET_MODEL_KINDS: set[str] = {
    "stagea_pb_base",
    "stagea_pb_day_group",
    "stagea_pb_time",
    "stagea_pb_day_time",
    "stagea_pb_hour",
    "stagea_pb_day_hour",
}


def logit(values: pd.Series | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    clipped = np.clip(arr, EPS, 1.0 - EPS)
    return np.log(clipped / (1.0 - clipped))


def normalize_price_bucket_scheme(scheme: str) -> str:
    normalized = str(scheme or "asymmetric").strip().lower()
    if normalized not in PRICE_BUCKET_SCHEMES:
        raise ValueError(f"Unsupported price bucket scheme: {scheme!r}")
    return "asymmetric" if normalized == "current" else normalized


def price_bucket_label(value: float | None, scheme: str = "asymmetric") -> str | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price):
        return None
    edges = PRICE_BUCKET_SCHEMES[normalize_price_bucket_scheme(scheme)]
    for idx, (low, high, label) in enumerate(edges):
        if price >= low and (price < high or (idx == len(edges) - 1 and price <= high)):
            return label
    return None


def rbf_features(z: np.ndarray, p: np.ndarray) -> np.ndarray:
    z_centers = np.array([-2.5, -1.5, -0.75, 0.0, 0.75, 1.5, 2.5], dtype=float)
    p_centers = np.array([0.25, 0.35, 0.45, 0.55, 0.65, 0.75], dtype=float)
    z_bw = 0.85
    p_bw = 0.085
    pieces = []
    for zc in z_centers:
        dz = ((z - zc) / z_bw) ** 2
        for pc in p_centers:
            dp = ((p - pc) / p_bw) ** 2
            pieces.append(np.exp(-0.5 * (dz + dp)))
    return np.column_stack(pieces)


def linear_feature_matrix(frame: pd.DataFrame, model_kind: str, side_mode: str) -> np.ndarray:
    z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
    p = pd.to_numeric(frame["selected_ask"], errors="coerce").to_numpy(float)
    lp = logit(p)
    base = [
        z,
        lp,
        z * lp,
        z * z,
        lp * lp,
    ]
    if side_mode == "with_side":
        base.append(frame["side"].astype(str).str.upper().eq("DOWN").to_numpy(float))
    x = np.column_stack(base)
    if model_kind == "surface_rbf":
        x = np.column_stack([x, rbf_features(z, p)])
    elif model_kind != "surface_linear":
        raise ValueError(f"Unknown model kind: {model_kind}")
    return x


def btc_momentum_feature_matrix(frame: pd.DataFrame, side_mode: str, momentum_feature: str) -> np.ndarray:
    z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
    momentum = pd.to_numeric(frame[momentum_feature], errors="coerce").to_numpy(float)
    pieces: list[Any] = [z, momentum]
    if side_mode == "with_side":
        pieces.append(frame["side"].astype(str).str.upper().eq("DOWN").to_numpy(float))
    return np.column_stack(pieces)


def btc_z_feature_matrix(frame: pd.DataFrame, side_mode: str) -> np.ndarray:
    z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
    pieces: list[Any] = [z]
    if side_mode == "with_side":
        pieces.append(frame["side"].astype(str).str.upper().eq("DOWN").to_numpy(float))
    return np.column_stack(pieces)


def _side_indicator(frame: pd.DataFrame) -> np.ndarray:
    return frame["side"].astype(str).str.upper().eq("DOWN").to_numpy(float)


def _stagea_time_mode(model_kind: str) -> str:
    text = str(model_kind)
    prefix = "stagea_pb_"
    if not text.startswith(prefix):
        raise ValueError(f"Not a Stage A price-bucket model kind: {model_kind!r}")
    return text.removeprefix(prefix)


def _stagea_london_time(frame: pd.DataFrame) -> pd.Series:
    if "timestamp_london" in frame.columns:
        timestamps = pd.to_datetime(frame["timestamp_london"], errors="coerce")
        if getattr(timestamps.dt, "tz", None) is None:
            timestamps = timestamps.dt.tz_localize("Europe/London")
        return timestamps.dt.tz_convert("Europe/London")
    timestamps = pd.to_datetime(frame["timestamp_utc"], utc=True, errors="coerce")
    return timestamps.dt.tz_convert("Europe/London")


def _stagea_day_group_series(frame: pd.DataFrame) -> pd.Series:
    london = _stagea_london_time(frame)
    weekday = london.dt.weekday
    labels = np.select(
        [weekday.le(2), weekday.between(3, 4), weekday.ge(5)],
        ["mon_wed", "thu_fri", "weekend"],
        default="",
    )
    return pd.Series(labels, index=frame.index, dtype="string")


def _stagea_time_block_categories(stagea_time_scheme: str) -> tuple[str, ...]:
    scheme = str(stagea_time_scheme or "current")
    if scheme not in STAGEA_TIME_SCHEMES:
        raise ValueError(f"Unsupported Stage A time scheme: {stagea_time_scheme!r}")
    return tuple(label for _start, _end, label in STAGEA_TIME_SCHEMES[scheme])


def _stagea_day_time_groups(stagea_time_scheme: str) -> tuple[str, ...]:
    return tuple(
        f"{day_group}_{time_block}"
        for day_group in STAGEA_DAY_GROUPS
        for time_block in _stagea_time_block_categories(stagea_time_scheme)
    )


def _stagea_time_block_series(frame: pd.DataFrame, stagea_time_scheme: str) -> pd.Series:
    london = _stagea_london_time(frame)
    hour_of_day = (
        london.dt.hour.astype(float)
        + london.dt.minute.astype(float) / 60.0
        + london.dt.second.astype(float) / 3600.0
        + london.dt.microsecond.astype(float) / 3_600_000_000.0
    )
    choices: list[pd.Series] = []
    labels: list[str] = []
    for start_hour, end_hour, label in STAGEA_TIME_SCHEMES[str(stagea_time_scheme or "current")]:
        choices.append(hour_of_day.ge(float(start_hour)) & hour_of_day.lt(float(end_hour)))
        labels.append(label)
    values = np.select(choices, labels, default="")
    values = np.where(hour_of_day.ge(24.0), labels[-1], values)
    return pd.Series(values, index=frame.index, dtype="string")


def _one_hot_drop_first(labels: pd.Series, categories: tuple[str, ...]) -> np.ndarray:
    values = labels.astype("string")
    columns = [values.eq(category).to_numpy(float) for category in categories[1:]]
    if not columns:
        return np.empty((len(labels), 0), dtype=float)
    return np.column_stack(columns)


def _stagea_hour_features(frame: pd.DataFrame) -> np.ndarray:
    london = _stagea_london_time(frame)
    hour = (
        london.dt.hour.astype(float)
        + london.dt.minute.astype(float) / 60.0
        + london.dt.second.astype(float) / 3600.0
        + london.dt.microsecond.astype(float) / 3_600_000_000.0
    ).to_numpy(float)
    angle = 2.0 * np.pi * hour / 24.0
    return np.column_stack([np.sin(angle), np.cos(angle), np.sin(2.0 * angle), np.cos(2.0 * angle)])


def stagea_feature_matrix(
    frame: pd.DataFrame,
    model_kind: str,
    side_mode: str,
    stagea_time_scheme: str,
) -> np.ndarray:
    z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
    pieces: list[Any] = [z]
    mode = _stagea_time_mode(model_kind)
    if mode == "day_group":
        pieces.append(_one_hot_drop_first(_stagea_day_group_series(frame), STAGEA_DAY_GROUPS))
    elif mode == "time":
        pieces.append(
            _one_hot_drop_first(
                _stagea_time_block_series(frame, stagea_time_scheme),
                _stagea_time_block_categories(stagea_time_scheme),
            )
        )
    elif mode == "day_time":
        day_time = _stagea_day_group_series(frame) + "_" + _stagea_time_block_series(frame, stagea_time_scheme)
        pieces.append(_one_hot_drop_first(day_time, _stagea_day_time_groups(stagea_time_scheme)))
    elif mode == "hour":
        pieces.append(_stagea_hour_features(frame))
    elif mode == "day_hour":
        pieces.append(_one_hot_drop_first(_stagea_day_group_series(frame), STAGEA_DAY_GROUPS))
        pieces.append(_stagea_hour_features(frame))
    elif mode != "base":
        raise ValueError(f"Unsupported Stage A mode: {mode!r}")
    if side_mode == "with_side":
        pieces.append(_side_indicator(frame))
    return np.column_stack(pieces)


class SurfaceModel:
    def __init__(
        self,
        *,
        model_kind: str,
        side_mode: str,
        c_value: float,
        spline_z_basis: int = 7,
        spline_p_basis: int = 6,
        spline_penalty: float = 10.0,
        momentum_feature: str = "",
        stagea_time_scheme: str = "current",
    ) -> None:
        self.model_kind = str(model_kind)
        self.side_mode = str(side_mode)
        self.c_value = float(c_value)
        self.spline_z_basis = int(spline_z_basis)
        self.spline_p_basis = int(spline_p_basis)
        self.spline_penalty = float(spline_penalty)
        self.momentum_feature = str(momentum_feature or "")
        self.stagea_time_scheme = str(stagea_time_scheme or "current")
        self.scaler = StandardScaler()
        self.model = LogisticRegression(
            C=max(float(c_value), 1e-6),
            penalty="l2",
            solver="lbfgs",
            max_iter=500,
            n_jobs=1,
        )
        self.z_spline: SplineTransformer | None = None
        self.p_spline: SplineTransformer | None = None
        self.rows = 0

    @staticmethod
    def _spline_knots_for_basis(basis_count: int, degree: int = 3) -> int:
        return max(2, int(basis_count) - int(degree) + 1)

    def _spline_matrix(self, frame: pd.DataFrame, *, fit: bool) -> np.ndarray:
        z = pd.to_numeric(frame["move_dz"], errors="coerce").to_numpy(float)
        p = pd.to_numeric(frame["selected_ask"], errors="coerce").to_numpy(float)
        lp = logit(p)
        if fit or self.z_spline is None or self.p_spline is None:
            self.z_spline = SplineTransformer(
                n_knots=self._spline_knots_for_basis(self.spline_z_basis),
                degree=3,
                include_bias=False,
                extrapolation="constant",
            )
            self.p_spline = SplineTransformer(
                n_knots=self._spline_knots_for_basis(self.spline_p_basis),
                degree=3,
                include_bias=False,
                extrapolation="constant",
            )
            z_basis = self.z_spline.fit_transform(z.reshape(-1, 1))
            p_basis = self.p_spline.fit_transform(p.reshape(-1, 1))
        else:
            z_basis = self.z_spline.transform(z.reshape(-1, 1))
            p_basis = self.p_spline.transform(p.reshape(-1, 1))
        interaction = (z_basis[:, :, None] * p_basis[:, None, :]).reshape(len(frame), -1)
        pieces: list[Any] = [z, lp, z * lp, z_basis, p_basis, interaction]
        if self.side_mode == "with_side":
            pieces.append(frame["side"].astype(str).str.upper().eq("DOWN").to_numpy(float))
        return np.column_stack(pieces)

    def _feature_matrix(self, frame: pd.DataFrame, *, fit: bool = False) -> np.ndarray:
        if self.model_kind == "surface_spline":
            return self._spline_matrix(frame, fit=fit)
        if self.model_kind == "btc_linear_z_price_bucket":
            return btc_z_feature_matrix(frame, self.side_mode)
        if self.model_kind == "btc_linear_z_momentum_price_bucket":
            if not self.momentum_feature:
                raise ValueError("Momentum feature is required for btc_linear_z_momentum_price_bucket")
            return btc_momentum_feature_matrix(frame, self.side_mode, self.momentum_feature)
        if self.model_kind in STAGEA_PRICE_BUCKET_MODEL_KINDS:
            return stagea_feature_matrix(frame, self.model_kind, self.side_mode, self.stagea_time_scheme)
        return linear_feature_matrix(frame, self.model_kind, self.side_mode)

    def fit(self, frame: pd.DataFrame, weights: np.ndarray) -> "SurfaceModel":
        x = self._feature_matrix(frame, fit=True)
        y = pd.to_numeric(frame["side_win"], errors="coerce").to_numpy(int)
        self.rows = int(len(frame))
        self.model.fit(self.scaler.fit_transform(x), y, sample_weight=weights)
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        x = self._feature_matrix(frame, fit=False)
        return self.model.predict_proba(self.scaler.transform(x))[:, 1]

    def predict_one(
        self,
        *,
        move_dz: float,
        selected_ask: float,
        side: str,
        side_dz_delta_2s: float | None = None,
        timestamp_utc: Any | None = None,
        timestamp_london: Any | None = None,
    ) -> float | None:
        try:
            z = float(move_dz)
            ask = float(selected_ask)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(z) or not math.isfinite(ask) or not EPS < ask < 1.0 - EPS:
            return None
        data: dict[str, list[object]] = {
            "move_dz": [z],
            "selected_ask": [ask],
            "side": [str(side).upper()],
        }
        if self.model_kind in STAGEA_PRICE_BUCKET_MODEL_KINDS:
            timestamp = timestamp_london if timestamp_london is not None else timestamp_utc
            if timestamp is None:
                return None
            data["timestamp_utc"] = [timestamp]
        if self.model_kind == "btc_linear_z_momentum_price_bucket":
            try:
                momentum = float(side_dz_delta_2s)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(momentum):
                return None
            data[self.momentum_feature] = [momentum]
        frame = pd.DataFrame(data)
        value = float(self.predict(frame)[0])
        return min(max(value, 0.0), 1.0)


class PriceBucketSurfaceModel:
    def __init__(
        self,
        *,
        model_kind: str,
        side_mode: str,
        c_value: float,
        price_bucket_scheme: str = "asymmetric",
        momentum_feature: str = "side_dz_delta_2s",
        price_bucket_min_rows: int = 5000,
        price_bucket_min_class: int = 100,
        stagea_time_scheme: str = "current",
    ) -> None:
        if model_kind not in {
            "btc_linear_z_price_bucket",
            "btc_linear_z_momentum_price_bucket",
            *STAGEA_PRICE_BUCKET_MODEL_KINDS,
        }:
            raise ValueError(f"Unsupported price-bucket model kind: {model_kind!r}")
        self.model_kind = str(model_kind)
        self.side_mode = str(side_mode)
        self.c_value = float(c_value)
        self.price_bucket_scheme = normalize_price_bucket_scheme(price_bucket_scheme)
        self.momentum_feature = str(momentum_feature or "") if model_kind == "btc_linear_z_momentum_price_bucket" else ""
        self.stagea_time_scheme = str(stagea_time_scheme or "current")
        if self.model_kind == "btc_linear_z_momentum_price_bucket" and not self.momentum_feature:
            raise ValueError("Momentum feature is required for btc_linear_z_momentum_price_bucket")
        self.price_bucket_min_rows = int(price_bucket_min_rows)
        self.price_bucket_min_class = int(price_bucket_min_class)
        self.bucket_models: dict[str, SurfaceModel] = {}
        self.bucket_rows: dict[str, int] = {}
        self.rows = 0

    def bucket_label(self, selected_ask: float | None) -> str | None:
        return price_bucket_label(selected_ask, self.price_bucket_scheme)

    def fit(self, frame: pd.DataFrame, weights: np.ndarray) -> "PriceBucketSurfaceModel":
        if len(frame) != len(weights):
            raise ValueError("Training frame and weights length mismatch")
        labels = frame["selected_ask"].map(lambda value: self.bucket_label(value))
        total_rows = 0
        for _low, _high, label in PRICE_BUCKET_SCHEMES[self.price_bucket_scheme]:
            bucket_mask = labels.eq(label).to_numpy()
            part = frame.loc[bucket_mask].copy()
            if len(part) < self.price_bucket_min_rows:
                continue
            outcomes = pd.to_numeric(part["side_win"], errors="coerce")
            counts = outcomes.value_counts()
            if (
                int(counts.get(0.0, 0)) < self.price_bucket_min_class
                or int(counts.get(1.0, 0)) < self.price_bucket_min_class
            ):
                continue
            part_weights = np.asarray(weights[bucket_mask], dtype=float)
            model = SurfaceModel(
                model_kind=self.model_kind,
                side_mode=self.side_mode,
                c_value=self.c_value,
                momentum_feature=self.momentum_feature,
                stagea_time_scheme=self.stagea_time_scheme,
            ).fit(part.reset_index(drop=True), part_weights)
            self.bucket_models[label] = model
            self.bucket_rows[label] = int(len(part))
            total_rows += int(len(part))
        self.rows = total_rows
        if not self.bucket_models:
            raise RuntimeError("No price buckets had enough support to train.")
        return self

    def predict_one(
        self,
        *,
        move_dz: float,
        selected_ask: float,
        side: str,
        side_dz_delta_2s: float | None = None,
        timestamp_utc: Any | None = None,
        timestamp_london: Any | None = None,
    ) -> float | None:
        label = self.bucket_label(selected_ask)
        if label is None:
            return None
        model = self.bucket_models.get(label)
        if model is None:
            return None
        return model.predict_one(
            move_dz=move_dz,
            selected_ask=selected_ask,
            side=side,
            side_dz_delta_2s=side_dz_delta_2s,
            timestamp_utc=timestamp_utc,
            timestamp_london=timestamp_london,
        )
