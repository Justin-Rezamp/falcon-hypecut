"""Frame-derived signals: cuts, motion, flashes and HUD regions.

All of these read the tiny grayscale plane already on the context, so the
whole set costs a few hundred milliseconds for an hour of footage.
"""

from __future__ import annotations

import numpy as np

from ..types import AnalysisContext
from .base import Signal, register

__all__ = ["SceneChange", "Motion", "Flash", "RoiActivity", "CameraPan"]


def _frames(ctx: AnalysisContext) -> np.ndarray:
    assert ctx.gray is not None
    return ctx.gray.astype(np.float32, copy=False)


@register("scene_change")
class SceneChange(Signal):
    """Mean absolute difference between consecutive frames.

    Spikes on hard cuts, respawns, scoreboard pop-ups and killcams — the
    structural boundaries an editor would also notice.

    Params
    ------
    compensate_camera: measure the difference *after* aligning out the
        camera's own pan/tilt/zoom (default false). For auto-tracking
        cameras such as the XbotGo Falcon, where the raw difference mostly
        measures the camera moving. See :mod:`hypecut.camera`.
    """

    description = "Frame-to-frame difference — cuts, respawns, killcams."
    requires_video = True
    # Luma levels. Real dark-scene cuts measure ~2.9; below 1.5 is codec flicker.
    noise_floor = 1.5

    def compute(self, ctx: AnalysisContext) -> np.ndarray:
        f = _frames(ctx)
        if f.shape[0] < 2:
            return np.zeros(ctx.n)
        if _compensate(self.params):
            return _camera(ctx, self.params).residual_mean
        diff = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
        return np.concatenate([diff[:1], diff])


@register("motion")
class Motion(Signal):
    """Spatial variance of the frame difference — how *much* of the screen moves.

    A camera flick moves everything; a menu cursor moves nothing. Weighting
    by coverage separates real action from idle UI churn.

    Params
    ------
    threshold: per-pixel difference counted as motion (default 6).
    compensate_camera: count only motion the camera's own pan/tilt/zoom does
        not explain (default false) — on an auto-tracking camera that is the
        players and the ball rather than the camera following them.
    """

    description = "Fraction of the frame in motion — action density."
    requires_video = True
    # Fraction of the frame. Under 2% is dither and mouse cursors.
    noise_floor = 0.02

    def compute(self, ctx: AnalysisContext) -> np.ndarray:
        thr = float(self.params.get("threshold", 6.0))
        f = _frames(ctx)
        if f.shape[0] < 2:
            return np.zeros(ctx.n)
        if _compensate(self.params):
            return _camera(ctx, {**self.params, "threshold": thr}).residual_coverage
        d = np.abs(np.diff(f, axis=0))
        coverage = (d > thr).mean(axis=(1, 2))
        return np.concatenate([coverage[:1], coverage])


@register("flash")
class Flash(Signal):
    """Global brightness jumps — explosions, flashbangs, ult animations."""

    description = "Global luminance jumps — explosions, flashes, ultimates."
    requires_video = True
    # Luma levels, same units as scene_change.
    noise_floor = 1.5

    def compute(self, ctx: AnalysisContext) -> np.ndarray:
        f = _frames(ctx)
        if f.shape[0] < 2:
            return np.zeros(ctx.n)
        mean = f.mean(axis=(1, 2))
        jump = np.abs(np.diff(mean))
        return np.concatenate([jump[:1], jump])


@register("roi_activity")
class RoiActivity(Signal):
    """Activity inside a fixed region of interest, e.g. the kill feed.

    This is the cheapest way to encode game-specific knowledge without OCR:
    most shooters draw the kill feed in the top-right corner, so churn in
    that rectangle is close to a kill counter. The rectangle is expressed in
    normalised coordinates so it survives any resolution.

    Params
    ------
    box: ``[x0, y0, x1, y1]`` in 0-1 coordinates (default top-right corner).
    threshold: per-pixel difference counted as activity (default 8).
    """

    description = "Change inside a normalised ROI box — kill feed / scoreboard."
    requires_video = True
    # Fraction of the box. A kill-feed line appearing changes far more than 5%.
    noise_floor = 0.05

    def compute(self, ctx: AnalysisContext) -> np.ndarray:
        box = self.params.get("box", [0.62, 0.03, 0.99, 0.30])
        thr = float(self.params.get("threshold", 8.0))
        f = _frames(ctx)
        if f.shape[0] < 2:
            return np.zeros(ctx.n)
        h, w = f.shape[1], f.shape[2]
        x0 = int(np.clip(box[0], 0, 1) * w)
        y0 = int(np.clip(box[1], 0, 1) * h)
        x1 = max(x0 + 1, int(np.clip(box[2], 0, 1) * w))
        y1 = max(y0 + 1, int(np.clip(box[3], 0, 1) * h))
        roi = f[:, y0:y1, x0:x1]
        d = np.abs(np.diff(roi, axis=0))
        activity = (d > thr).mean(axis=(1, 2))
        return np.concatenate([activity[:1], activity])


@register("camera_pan")
class CameraPan(Signal):
    """How fast an auto-tracking camera is moving — pan, tilt and zoom.

    An XbotGo Falcon follows the ball. When play breaks quickly — a long
    ball, a counter-attack, a breakaway down the wing — the camera has to
    swing hard to keep up, so its own speed is a cheap proxy for "play just
    changed direction fast". Measured in fractions of the frame width per
    analysis step, from the same estimate :mod:`hypecut.camera` uses to
    compensate the other signals (so it costs nothing extra when those run).

    Weight it low: the camera also swings for a goal kick nobody cares about.

    Params
    ------
    scales: zoom factors searched per step (default 0.98, 1.0, 1.02).
    """

    description = "Auto-tracking camera speed (pan/tilt/zoom) — the Falcon chasing a fast break."
    requires_video = True
    # 0.5% of the frame width per step is drift, not a swing.
    noise_floor = 0.005

    def compute(self, ctx: AnalysisContext) -> np.ndarray:
        if ctx.gray is None or ctx.gray.shape[0] < 2:
            return np.zeros(ctx.n)
        return _camera(ctx, self.params).speed


def _compensate(params: dict) -> bool:
    value = params.get("compensate_camera", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _camera(ctx: AnalysisContext, params: dict):
    from ..camera import DEFAULT_SCALES, camera_motion_for

    return camera_motion_for(
        ctx,
        scales=tuple(params.get("scales", DEFAULT_SCALES)),
        threshold=float(params.get("threshold", 6.0)),
    )
