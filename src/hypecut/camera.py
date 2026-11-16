"""Camera-motion compensation for auto-tracking cameras.

Every frame signal in :mod:`hypecut.signals.visual` measures *how much the
picture changed*. On a locked-off tripod that is the same question as *how
much happened*. On an auto-tracking camera such as the XbotGo Falcon it is
not: the camera pans, tilts and zooms continuously to follow play, so the
whole frame changes all the time and the raw difference mostly measures the
camera, not the players.

The fix is the classic one from video stabilisation. For each pair of
consecutive analysis frames, estimate the *global* motion — the part every
pixel shares — and warp the previous frame onto the current one before
differencing. What is left, the residual, is the motion the camera did not
explain: players running, the ball, a goalmouth scramble.

The model is a similarity without rotation (zoom ``s`` about the centre plus a
translation ``dx, dy``), which is what a pan/tilt/zoom head can physically do.
Translation comes from phase correlation, which is exact for a pure shift and
robust to the brightness drift of auto-exposure; zoom comes from a small
search over candidate scales, each re-checked with phase correlation, keeping
whichever leaves the smallest residual. Everything is vectorised over chunks
of frames with numpy only — no OpenCV — because the frames are the tiny
grayscale planes the pipeline already decoded (96x54 by default).

Results are cached on ``ctx.extras["camera_motion"]`` so the motion, scene
change and camera-pan signals and the shot snapper share one estimate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

__all__ = [
    "CameraMotion",
    "estimate_camera_motion",
    "camera_motion_for",
    "phase_correlate",
    "DEFAULT_SCALES",
]

#: Per-step zoom factors tried. Falcon zooms are slow, so at 10 Hz one step
#: rarely changes scale by more than a couple of percent.
DEFAULT_SCALES: tuple[float, ...] = (0.98, 1.0, 1.02)

_CACHE_KEY = "camera_motion"
#: Low-pass width on the cross-power spectrum, in cycles per pixel.
_LOWPASS_SIGMA = 0.15
#: Half-size of the neighbourhood cleared around each correlation peak.
_SUPPRESS = 1


@dataclass
class CameraMotion:
    """Per-frame camera motion and the residual it leaves behind.

    Every array has one value per analysis frame, aligned to ``ctx.times``.
    Index 0 has no predecessor and copies index 1, the same convention the
    frame-difference signals use.
    """

    dx: np.ndarray  # px per step, positive = content moved right
    dy: np.ndarray  # px per step, positive = content moved down
    scale: np.ndarray  # zoom per step, >1 = zooming in
    raw_mean: np.ndarray  # mean |cur - prev| (luma levels), what scene_change sees
    residual_mean: np.ndarray  # mean |cur - aligned prev| over the valid area
    residual_coverage: np.ndarray  # fraction of valid pixels whose residual > threshold
    raw_coverage: np.ndarray  # same fraction without compensation (what motion sees)
    width: int
    height: int
    threshold: float

    @property
    def speed(self) -> np.ndarray:
        """Camera speed as a fraction of frame width per step (pan + tilt + zoom)."""
        zoom_px = np.abs(self.scale - 1.0) * (self.width / 2.0)
        return (np.hypot(self.dx, self.dy) + zoom_px) / max(1, self.width)

    def explained(self) -> np.ndarray:
        """Share of the raw frame difference the camera accounts for (0-1)."""
        raw = np.maximum(self.raw_mean, 1e-6)
        return np.clip(1.0 - self.residual_mean / raw, 0.0, 1.0)


def camera_motion_for(ctx, **params) -> CameraMotion:
    """The (cached) camera-motion estimate for an analysis context."""
    key = (
        _CACHE_KEY,
        tuple(params.get("scales", DEFAULT_SCALES)),
        float(params.get("threshold", 6.0)),
    )
    cached = ctx.extras.get(_CACHE_KEY)
    if isinstance(cached, tuple) and cached[0] == key:
        return cached[1]
    assert ctx.gray is not None, "camera motion needs decoded frames"
    result = estimate_camera_motion(
        ctx.gray,
        scales=params.get("scales", DEFAULT_SCALES),
        threshold=float(params.get("threshold", 6.0)),
    )
    ctx.extras[_CACHE_KEY] = (key, result)
    return result


def estimate_camera_motion(
    gray: np.ndarray,
    *,
    scales: Sequence[float] = DEFAULT_SCALES,
    threshold: float = 6.0,
    peaks: int = 2,
    tiles: tuple[int, int] | None = (3, 4),
    prior_seconds_steps: int = 15,
    chunk: int = 256,
) -> CameraMotion:
    """Estimate global motion between consecutive frames and the residual.

    ``gray`` is ``(T, H, W)`` uint8 or float. ``threshold`` is the per-pixel
    residual (luma levels) that counts as "something moved", matching the
    ``motion`` signal's default so the two coverages are comparable.
    """
    f = np.asarray(gray, dtype=np.float32)
    t, h, w = f.shape if f.ndim == 3 else (0, 0, 0)
    if t < 2:
        z = np.zeros(max(t, 0))
        return CameraMotion(z, z, z + 1.0, z, z, z, z, w, h, threshold)

    scales = tuple(float(s) for s in scales) or (1.0,)
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = np.arange(w, dtype=np.float32)[None, :]
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]

    n = t - 1
    out = {
        k: np.zeros(n, dtype=np.float64)
        for k in ("dx", "dy", "scale", "raw", "res", "cov", "rawcov")
    }
    for lo in range(0, n, chunk):
        hi = min(n, lo + chunk)
        prev = f[lo:hi]
        cur = f[lo + 1 : hi + 1]
        raw = np.abs(cur - prev)
        out["raw"][lo:hi] = raw.mean(axis=(1, 2))
        out["rawcov"][lo:hi] = (raw > threshold).mean(axis=(1, 2))

        fc = np.fft.fft2((cur - cur.mean(axis=(1, 2), keepdims=True)) * win)
        # The identity is always a candidate, so compensation can never report
        # more residual than a plain exposure-corrected difference would.
        full = np.ones((hi - lo, h, w), dtype=bool)
        best_res, best_cov = _residual(cur, prev, full, threshold)
        out["res"][lo:hi] = best_res
        out["cov"][lo:hi] = best_cov
        out["scale"][lo:hi] = 1.0
        for s in scales:
            warped, valid_s = _zoom(prev, s, ys, xs) if s != 1.0 else (prev, None)
            fp_plain = np.fft.fft2(warped)
            fp = np.fft.fft2((warped - warped.mean(axis=(1, 2), keepdims=True)) * win)
            # At unit zoom, several global correlation peaks plus the median of
            # per-tile estimates: a bright, sharp player can steal the global
            # peak on a 96x54 frame, but not the majority of a 3x4 tile grid.
            # Only the best global peak at the other scales, to stay cheap.
            dys, dxs = _peak_shifts(fc, fp, peaks=peaks if s == 1.0 else 1)
            if s == 1.0 and tiles:
                ty, tx = _tile_median_shift(cur, prev, tiles)
                dys.append(ty)
                dxs.append(tx)
            for dy, dx in zip(dys, dxs, strict=True):
                # Shift the warped previous frame by (dy, dx) in the Fourier
                # domain (sub-pixel exact; the wrapped border is masked out).
                ramp = np.exp(
                    -2j * np.pi * (fy[None] * dy[:, None, None] + fx[None] * dx[:, None, None])
                )
                aligned = np.real(np.fft.ifft2(fp_plain * ramp)).astype(np.float32)
                mask = _shift_mask(dx, dy, h, w, ys, xs)
                if valid_s is not None:
                    mask &= _shift_bool(valid_s, dy, dx)
                res, cov = _residual(cur, aligned, mask, threshold)
                better = res < best_res
                best_res = np.where(better, res, best_res)
                out["res"][lo:hi] = np.where(better, res, out["res"][lo:hi])
                out["cov"][lo:hi] = np.where(better, cov, out["cov"][lo:hi])
                out["dx"][lo:hi] = np.where(better, dx, out["dx"][lo:hi])
                out["dy"][lo:hi] = np.where(better, dy, out["dy"][lo:hi])
                out["scale"][lo:hi] = np.where(better, s, out["scale"][lo:hi])

    # Second pass: a temporal prior. A pan/tilt head moves smoothly, so the
    # running median of the per-step estimates is a strong guess for any one
    # step. Where a bright subject stole the correlation peak for a few
    # frames, that guess explains the background better and wins on residual.
    if prior_seconds_steps > 1 and n >= 3:
        my = _running_median(out["dy"], prior_seconds_steps)
        mx = _running_median(out["dx"], prior_seconds_steps)
        ms = _running_median(out["scale"], prior_seconds_steps)
        for lo in range(0, n, chunk):
            hi = min(n, lo + chunk)
            prev = f[lo:hi]
            cur = f[lo + 1 : hi + 1]
            for s in sorted(set(np.round(ms[lo:hi], 4).tolist())):
                sel = np.flatnonzero(np.isclose(ms[lo:hi], s))
                p_s, valid_s = _zoom(prev[sel], s, ys, xs) if s != 1.0 else (prev[sel], None)
                dy = my[lo:hi][sel]
                dx = mx[lo:hi][sel]
                ramp = np.exp(
                    -2j * np.pi * (fy[None] * dy[:, None, None] + fx[None] * dx[:, None, None])
                )
                aligned = np.real(np.fft.ifft2(np.fft.fft2(p_s) * ramp)).astype(np.float32)
                mask = _shift_mask(dx, dy, h, w, ys, xs)
                if valid_s is not None:
                    mask &= _shift_bool(valid_s, dy, dx)
                res, cov = _residual(cur[sel], aligned, mask, threshold)
                idx = lo + sel
                better = res < out["res"][idx]
                out["res"][idx] = np.where(better, res, out["res"][idx])
                out["cov"][idx] = np.where(better, cov, out["cov"][idx])
                out["dx"][idx] = np.where(better, dx, out["dx"][idx])
                out["dy"][idx] = np.where(better, dy, out["dy"][idx])
                out["scale"][idx] = np.where(better, s, out["scale"][idx])

    # Final pass: a couple of Lucas-Kanade steps from the winning estimate.
    # Phase correlation is exact for sharp, textured frames but biased by a
    # fraction of a pixel on soft, low-detail ones (a wide shot of grass); a
    # gradient step is the opposite, so together they cover both. A step is
    # only kept where it lowers the residual.
    for lo in range(0, n, chunk):
        hi = min(n, lo + chunk)
        _refine_lk(f[lo:hi], f[lo + 1 : hi + 1], out, lo, hi, threshold, ys, xs, fy, fx)

    def pad(a: np.ndarray) -> np.ndarray:
        return np.concatenate([a[:1], a])

    return CameraMotion(
        dx=pad(out["dx"]),
        dy=pad(out["dy"]),
        scale=pad(out["scale"]),
        raw_mean=pad(out["raw"]),
        residual_mean=pad(out["res"]),
        residual_coverage=pad(out["cov"]),
        raw_coverage=pad(out["rawcov"]),
        width=w,
        height=h,
        threshold=threshold,
    )


def phase_correlate(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Shift ``(dy, dx)`` that moves ``a`` onto ``b`` (``b ≈ a`` shifted by it)."""
    a = np.asarray(a, dtype=np.float32)[None]
    b = np.asarray(b, dtype=np.float32)[None]
    h, w = a.shape[1:]
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    fa = np.fft.fft2((a - a.mean()) * win)
    fb = np.fft.fft2((b - b.mean()) * win)
    dy, dx = _peak_shift(fb, fa)
    return float(dy[0]), float(dx[0])


# --------------------------------------------------------------------- helpers


def _peak_shift(f_cur: np.ndarray, f_prev: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-frame sub-pixel shift taking ``prev`` to ``cur`` via phase correlation."""
    dys, dxs = _peak_shifts(f_cur, f_prev, peaks=1)
    return dys[0], dxs[0]


def _peak_shifts(
    f_cur: np.ndarray, f_prev: np.ndarray, *, peaks: int = 3
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """The ``peaks`` strongest candidate shifts per frame, strongest first.

    The strongest correlation peak is usually the background, but a bright,
    sharp-edged subject (a player in a white kit against grass) can win it on
    a low-resolution frame. Returning a few candidates and letting the caller
    keep whichever leaves the least residual makes the estimate robust to that
    without a feature tracker: the background always explains more pixels.
    """
    cross = f_cur * np.conj(f_prev)
    k, h, w = cross.shape
    # Plain phase correlation divides by |cross| and so whitens the spectrum,
    # which hands the high frequencies — on grass at 96x54 that is mostly
    # sensor and codec noise — the same vote as the field lines and players.
    # Half-whitening plus a gentle low-pass keeps the sharp peak for textured
    # scenes and still locks on for soft, low-detail ones.
    cross = cross / np.sqrt(np.maximum(np.abs(cross), 1e-9))
    cross = cross * _lowpass(h, w)
    corr = np.real(np.fft.ifft2(cross))
    rows = np.arange(k)
    out_y: list[np.ndarray] = []
    out_x: list[np.ndarray] = []
    work = corr.copy()
    for _ in range(max(1, peaks)):
        flat = work.reshape(k, -1).argmax(axis=1)
        py, px = np.divmod(flat, w)
        c0 = corr[rows, py, px]
        oy = _subpixel(corr[rows, (py - 1) % h, px], c0, corr[rows, (py + 1) % h, px])
        ox = _subpixel(corr[rows, py, (px - 1) % w], c0, corr[rows, py, (px + 1) % w])
        out_y.append((np.where(py > h // 2, py - h, py) + oy).astype(np.float64))
        out_x.append((np.where(px > w // 2, px - w, px) + ox).astype(np.float64))
        # Suppress a 5x5 neighbourhood so the next pick is a different peak.
        for ddy in range(-_SUPPRESS, _SUPPRESS + 1):
            for ddx in range(-_SUPPRESS, _SUPPRESS + 1):
                work[rows, (py + ddy) % h, (px + ddx) % w] = -np.inf
    return out_y, out_x


def _align(prev, s_arr, dy, dx, ys, xs, fy, fx):
    """Warp each previous frame by its own zoom and sub-pixel shift; returns frames + mask."""
    k, h, w = prev.shape
    aligned = np.empty_like(prev)
    mask = _shift_mask(dx, dy, h, w, ys, xs)
    for s in np.unique(s_arr):
        sel = np.flatnonzero(s_arr == s)
        p_s, valid_s = _zoom(prev[sel], float(s), ys, xs) if s != 1.0 else (prev[sel], None)
        ramp = np.exp(
            -2j * np.pi * (fy[None] * dy[sel, None, None] + fx[None] * dx[sel, None, None])
        )
        aligned[sel] = np.real(np.fft.ifft2(np.fft.fft2(p_s) * ramp)).astype(np.float32)
        if valid_s is not None:
            mask[sel] &= _shift_bool(valid_s, dy[sel], dx[sel])
    return aligned, mask


def _refine_lk(prev, cur, out, lo, hi, threshold, ys, xs, fy, fx, iterations: int = 2) -> None:
    for _ in range(iterations):
        s_arr = out["scale"][lo:hi]
        dy = out["dy"][lo:hi].copy()
        dx = out["dx"][lo:hi].copy()
        aligned, mask = _align(prev, s_arr, dy, dx, ys, xs, fy, fx)
        count = np.maximum(mask.sum(axis=(1, 2)), 1)
        a_n = aligned - ((aligned * mask).sum(axis=(1, 2)) / count)[:, None, None]
        c_n = cur - ((cur * mask).sum(axis=(1, 2)) / count)[:, None, None]
        gy, gx = np.gradient(aligned, axis=(1, 2))
        err = (a_n - c_n) * mask
        gxx = (gx * gx * mask).sum(axis=(1, 2))
        gyy = (gy * gy * mask).sum(axis=(1, 2))
        gxy = (gx * gy * mask).sum(axis=(1, 2))
        bx = (gx * err).sum(axis=(1, 2))
        by = (gy * err).sum(axis=(1, 2))
        det = gxx * gyy - gxy * gxy
        ok = det > 1e-6 * np.maximum(gxx + gyy, 1e-9) ** 2
        safe = np.where(ok, det, 1.0)
        step_x = np.where(ok, (gyy * bx - gxy * by) / safe, 0.0)
        step_y = np.where(ok, (gxx * by - gxy * bx) / safe, 0.0)
        step_x = np.clip(step_x, -1.0, 1.0)
        step_y = np.clip(step_y, -1.0, 1.0)
        new_dx, new_dy = dx + step_x, dy + step_y
        aligned2, mask2 = _align(prev, s_arr, new_dy, new_dx, ys, xs, fy, fx)
        res, cov = _residual(cur, aligned2, mask2, threshold)
        better = ok & (res < out["res"][lo:hi])
        if not better.any():
            return
        out["res"][lo:hi] = np.where(better, res, out["res"][lo:hi])
        out["cov"][lo:hi] = np.where(better, cov, out["cov"][lo:hi])
        out["dx"][lo:hi] = np.where(better, new_dx, dx)
        out["dy"][lo:hi] = np.where(better, new_dy, dy)


def _tile_median_shift(
    cur: np.ndarray, prev: np.ndarray, grid: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Median over a tile grid of each tile's own phase-correlation shift."""
    _, h, w = cur.shape
    rows, cols = grid
    th, tw = h // rows, w // cols
    if th < 8 or tw < 8:
        return _peak_shift(np.fft.fft2(cur), np.fft.fft2(prev))
    win = np.outer(np.hanning(th), np.hanning(tw)).astype(np.float32)
    ys_: list[np.ndarray] = []
    xs_: list[np.ndarray] = []
    for r in range(rows):
        for c in range(cols):
            a = prev[:, r * th : (r + 1) * th, c * tw : (c + 1) * tw]
            b = cur[:, r * th : (r + 1) * th, c * tw : (c + 1) * tw]
            fa = np.fft.fft2((a - a.mean(axis=(1, 2), keepdims=True)) * win)
            fb = np.fft.fft2((b - b.mean(axis=(1, 2), keepdims=True)) * win)
            dy, dx = _peak_shift(fb, fa)
            ys_.append(dy)
            xs_.append(dx)
    return np.median(np.stack(ys_), axis=0), np.median(np.stack(xs_), axis=0)


def _running_median(values: np.ndarray, window: int) -> np.ndarray:
    window = max(1, min(int(window) | 1, values.size if values.size % 2 else values.size - 1))
    if window <= 1:
        return values.copy()
    pad = window // 2
    padded = np.pad(values, (pad, pad), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def _subpixel(c_m: np.ndarray, c_0: np.ndarray, c_p: np.ndarray) -> np.ndarray:
    denom = c_m - 2 * c_0 + c_p
    with np.errstate(divide="ignore", invalid="ignore"):
        off = np.where(np.abs(denom) > 1e-12, 0.5 * (c_m - c_p) / denom, 0.0)
    return np.clip(np.nan_to_num(off), -0.5, 0.5)


def _lowpass(h: int, w: int, sigma: float | None = None) -> np.ndarray:
    sigma = _LOWPASS_SIGMA if sigma is None else sigma
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    return np.exp(-(fy**2 + fx**2) / (2 * sigma**2))


def _zoom(frames: np.ndarray, s: float, ys: np.ndarray, xs: np.ndarray):
    """Zoom every frame by ``s`` about its centre (bilinear); returns frames + valid mask."""
    _, h, w = frames.shape
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    sy = cy + (ys - cy) / s + 0 * xs
    sx = cx + (xs - cx) / s + 0 * ys
    valid = (sy >= 0) & (sy <= h - 1) & (sx >= 0) & (sx <= w - 1)
    sy = np.clip(sy, 0, h - 1)
    sx = np.clip(sx, 0, w - 1)
    y0 = np.floor(sy).astype(np.int64)
    x0 = np.floor(sx).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (sy - y0).astype(np.float32)
    wx = (sx - x0).astype(np.float32)
    top = frames[:, y0, x0] * (1 - wx) + frames[:, y0, x1] * wx
    bot = frames[:, y1, x0] * (1 - wx) + frames[:, y1, x1] * wx
    return top * (1 - wy) + bot * wy, valid


def _shift_mask(dx, dy, h, w, ys, xs) -> np.ndarray:
    """Pixels of the aligned frame that came from real (not wrapped) content."""
    margin = 1.0
    x_lo = np.maximum(dx, 0)[:, None, None] + margin
    x_hi = (w - 1) + np.minimum(dx, 0)[:, None, None] - margin
    y_lo = np.maximum(dy, 0)[:, None, None] + margin
    y_hi = (h - 1) + np.minimum(dy, 0)[:, None, None] - margin
    return (xs[None] >= x_lo) & (xs[None] <= x_hi) & (ys[None] >= y_lo) & (ys[None] <= y_hi)


def _shift_bool(mask: np.ndarray, dy: np.ndarray, dx: np.ndarray) -> np.ndarray:
    """Move a single (H, W) validity mask by each frame's integer-rounded shift."""
    out = np.empty((dy.size, *mask.shape), dtype=bool)
    for i, (sy, sx) in enumerate(
        zip(np.rint(dy).astype(int), np.rint(dx).astype(int), strict=True)
    ):
        out[i] = np.roll(mask, (sy, sx), axis=(0, 1))
    return out


def _residual(cur: np.ndarray, aligned: np.ndarray, mask: np.ndarray, threshold: float):
    """Exposure-robust mean residual and coverage over the valid area of each frame."""
    count = np.maximum(mask.sum(axis=(1, 2)), 1)
    mean_c = (cur * mask).sum(axis=(1, 2)) / count
    mean_a = (aligned * mask).sum(axis=(1, 2)) / count
    diff = np.abs((cur - mean_c[:, None, None]) - (aligned - mean_a[:, None, None])) * mask
    res = diff.sum(axis=(1, 2)) / count
    cov = ((diff > threshold) & mask).sum(axis=(1, 2)) / count
    # A frame with almost no valid area (a huge jump) cannot be compensated;
    # report it as fully unexplained rather than as perfectly still.
    tiny = mask.sum(axis=(1, 2)) < 0.25 * mask.shape[1] * mask.shape[2]
    raw = np.abs(cur - aligned).mean(axis=(1, 2))
    res = np.where(tiny, np.maximum(res, raw), res)
    return res.astype(np.float64), cov.astype(np.float64)
