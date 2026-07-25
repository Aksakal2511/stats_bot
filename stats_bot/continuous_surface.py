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
    ) -> None:
        self.model_kind = str(model_kind)
        self.side_mode = str(side_mode)
        self.c_value = float(c_value)
        self.spline_z_basis = int(spline_z_basis)
        self.spline_p_basis = int(spline_p_basis)
        self.spline_penalty = float(spline_penalty)
        self.momentum_feature = str(momentum_feature or "")
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
        if self.model_kind == "btc_linear_z_momentum_price_bucket":
            if not self.momentum_feature:
                raise ValueError("Momentum feature is required for btc_linear_z_momentum_price_bucket")
            return btc_momentum_feature_matrix(frame, self.side_mode, self.momentum_feature)
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
    ) -> None:
        if model_kind != "btc_linear_z_momentum_price_bucket":
            raise ValueError(f"Unsupported price-bucket model kind: {model_kind!r}")
        self.model_kind = str(model_kind)
        self.side_mode = str(side_mode)
        self.c_value = float(c_value)
        self.price_bucket_scheme = normalize_price_bucket_scheme(price_bucket_scheme)
        self.momentum_feature = str(momentum_feature)
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
        )
