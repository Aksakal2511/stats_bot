from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import SplineTransformer, StandardScaler


EPS = 1e-5


def logit(values: pd.Series | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    clipped = np.clip(arr, EPS, 1.0 - EPS)
    return np.log(clipped / (1.0 - clipped))


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
    ) -> None:
        self.model_kind = str(model_kind)
        self.side_mode = str(side_mode)
        self.c_value = float(c_value)
        self.spline_z_basis = int(spline_z_basis)
        self.spline_p_basis = int(spline_p_basis)
        self.spline_penalty = float(spline_penalty)
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

    def predict_one(self, *, move_dz: float, selected_ask: float, side: str) -> float | None:
        try:
            z = float(move_dz)
            ask = float(selected_ask)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(z) or not math.isfinite(ask) or not EPS < ask < 1.0 - EPS:
            return None
        frame = pd.DataFrame(
            {
                "move_dz": [z],
                "selected_ask": [ask],
                "side": [str(side).upper()],
            }
        )
        value = float(self.predict(frame)[0])
        return min(max(value, 0.0), 1.0)
