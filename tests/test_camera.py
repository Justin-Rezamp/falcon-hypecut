"""Camera-motion compensation on synthetic auto-tracking footage.

The frames are crops sliding across a larger textured "field", which is
exactly what an XbotGo Falcon pan looks like at analysis resolution.
"""

from __future__ import annotations

import numpy as np
import pytest

from hypecut.camera import _zoom, camera_motion_for, estimate_camera_motion, phase_correlate
from hypecut.signals.visual import CameraPan, Motion, SceneChange
from hypecut.snapping import drop_camera_moves, find_boundaries
from hypecut.types import AnalysisContext, VideoInfo

H, W = 54, 96


def _field(seed: int = 0, smooth: float = 100.0, shape=(400, 900)) -> np.ndarray:
    """A band-limited random texture in 0-255 — grass, lines, crowd."""
    rng = np.random.default_rng(seed)
    noise = rng.random(shape)
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.fftfreq(shape[1])[None, :]
    tex = np.real(np.fft.ifft2(np.fft.fft2(noise) * np.exp(-(fy**2 + fx**2) * smooth)))
    return (tex - tex.min()) / (tex.max() - tex.min()) * 255.0


def _pan(field: np.ndarray, steps: int, dx: int = 3, dy: int = 0, noise: float = 1.0, seed=1):
    rng = np.random.default_rng(seed)
    frames = np.stack(
        [field[50 + dy * i : 50 + dy * i + H, 20 + dx * i : 20 + dx * i + W] for i in range(steps)]
    )
    return np.clip(frames + rng.normal(0, noise, frames.shape), 0, 255).astype(np.uint8)


def _ctx(gray: np.ndarray, fps: float = 10.0) -> AnalysisContext:
    n = gray.shape[0]
    info = VideoInfo(path="x.mp4", duration=n / fps, fps=30.0, width=W, height=H, has_audio=False)
    return AnalysisContext(info=info, grid_fps=fps, times=np.arange(n) / fps, gray=gray)


def test_phase_correlation_recovers_a_known_shift():
    field = _field()
    a = field[100 : 100 + H, 100 : 100 + W]
    b = field[100 - 2 : 100 - 2 + H, 100 - 5 : 100 - 5 + W]  # content moves +5 x, +2 y
    dy, dx = phase_correlate(a, b)
    assert dx == pytest.approx(5.0, abs=0.3)
    assert dy == pytest.approx(2.0, abs=0.3)


@pytest.mark.parametrize("smooth", [0.0, 100.0, 800.0])
def test_pure_pan_leaves_almost_no_residual(smooth):
    frames = _pan(_field(smooth=smooth), 40, dx=3, dy=1)
    cm = estimate_camera_motion(frames)
    # The camera moved the content left by 3 px and up by 1 px every step...
    assert np.median(cm.dx[1:]) == pytest.approx(-3.0, abs=0.3)
    assert np.median(cm.dy[1:]) == pytest.approx(-1.0, abs=0.3)
    # ...which the raw difference reads as motion nearly everywhere,
    assert cm.raw_coverage[1:].mean() > 0.3
    # and the compensated residual reads as almost nothing.
    assert cm.residual_coverage[1:].mean() < 0.03
    assert cm.residual_mean[1:].mean() < 0.4 * cm.raw_mean[1:].mean()
    assert np.median(cm.explained()[1:]) > 0.6


def test_motion_signal_ignores_the_pan_only_when_asked():
    frames = _pan(_field(), 40, dx=4)
    ctx = _ctx(frames)
    raw = Motion().compute(ctx)
    comp = Motion(compensate_camera=True).compute(ctx)
    assert raw[1:].mean() > 0.3  # default behaviour unchanged: the pan is "motion"
    assert comp[1:].mean() < 0.03  # compensated: nothing is happening
    assert SceneChange(compensate_camera=True).compute(ctx)[1:].mean() < (
        0.4 * SceneChange().compute(ctx)[1:].mean()
    )


def test_a_moving_player_survives_compensation():
    field = _field()
    frames = _pan(field, 30, dx=3).astype(np.float32)
    for i in range(10, 20):  # a bright player running against the pan
        x = 30 + 2 * (i - 10)
        frames[i, 20:32, x : x + 6] += 70
    frames = np.clip(frames, 0, 255).astype(np.uint8)
    cm = estimate_camera_motion(frames)
    quiet = cm.residual_coverage[2:9].mean()
    busy = cm.residual_coverage[11:20].mean()
    assert busy > 0.01
    assert busy > 4 * max(quiet, 1e-3)


def test_zoom_is_estimated_and_compensated():
    field = _field(smooth=100.0)
    ys = np.arange(H, dtype=np.float32)[:, None]
    xs = np.arange(W, dtype=np.float32)[None, :]
    frames = [field[100 : 100 + H, 100 : 100 + W].astype(np.float32)]
    for _ in range(15):
        z, _ = _zoom(frames[-1][None], 1.02, ys, xs)
        frames.append(z[0])
    cm = estimate_camera_motion(np.stack(frames))
    assert np.median(cm.scale[1:]) == pytest.approx(1.02)
    assert cm.residual_mean[1:].mean() < 0.6 * cm.raw_mean[1:].mean()


def test_camera_pan_signal_tracks_camera_speed():
    slow = CameraPan().compute(_ctx(_pan(_field(), 30, dx=1)))
    fast = CameraPan().compute(_ctx(_pan(_field(), 30, dx=5)))
    assert fast[1:].mean() > 3 * slow[1:].mean()


def test_estimate_is_cached_on_the_context():
    ctx = _ctx(_pan(_field(), 20))
    first = camera_motion_for(ctx)
    assert camera_motion_for(ctx) is first


def test_pans_are_not_shot_boundaries_but_cuts_still_are():
    field = _field(seed=3)
    other = _field(seed=9)
    # Steady pan, then a sudden whip pan (12 px in one step), then a real cut.
    a = _pan(field, 30, dx=1)
    b = np.stack([field[50 : 50 + H, 20 + 29 + 12 + i : 20 + 29 + 12 + i + W] for i in range(30)])
    c = _pan(other, 30, dx=1)
    gray = np.concatenate([a, b.astype(np.uint8), c]).astype(np.uint8)
    ctx = _ctx(gray)
    cuts = find_boundaries(gray, 10.0)
    cut_time = 60 / 10.0
    whip_time = 30 / 10.0
    # The uncompensated detector calls the whip pan a cut — the false positive.
    assert any(abs(t - whip_time) < 0.15 for t in cuts), cuts
    kept, _, _ = drop_camera_moves(ctx, cuts, [], [])
    assert any(abs(t - cut_time) < 0.15 for t in kept), (cuts, kept)
    assert not any(abs(t - whip_time) < 0.15 for t in kept), (cuts, kept)


def test_default_profiles_do_not_compensate():
    from hypecut.config import load_config

    cfg = load_config()
    assert cfg.segments.snap_compensate_camera is False
    assert not cfg.signals.params.get("motion", {}).get("compensate_camera")
    falcon = load_config("configs/falcon-soccer.yaml")
    assert falcon.signals.params["motion"]["compensate_camera"] is True
    assert falcon.segments.snap_compensate_camera is True
