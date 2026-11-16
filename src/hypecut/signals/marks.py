"""The Falcon mark signal: parent taps as a scoring track.

An XbotGo Falcon ``.mark`` file is a list of moments a parent tapped during
the game (see :mod:`hypecut.marks`). Like a Twitch chat log it is a human
label that costs nothing to collect — but much sparser and much more
deliberate, so instead of a rate it becomes a smooth bump around each tap.

The bump is asymmetric on purpose. People tap *after* they see something:
the play happened in the seconds before the tap, not after it. So the
default bump reaches ``lead_seconds`` (8 s) back and only ``trail_seconds``
(2 s) forward.

The file is found from the video's own name unless configured: ``match.mp4``
looks for ``match.mark`` and ``match.mp4.mark`` next to it.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np

from ..types import AnalysisContext
from .base import Signal, register

__all__ = ["FalconMarks", "mark_bumps"]


def mark_bumps(
    times: np.ndarray, clicks: list[float], *, lead: float = 8.0, trail: float = 2.0
) -> np.ndarray:
    """Sum of raised-cosine bumps peaking at each click, ``lead`` back, ``trail`` forward."""
    out = np.zeros(times.shape[0], dtype=np.float64)
    lead = max(1e-3, float(lead))
    trail = max(1e-3, float(trail))
    for t in clicks:
        d = times - t
        before = (d < 0) & (d >= -lead)
        after = (d >= 0) & (d <= trail)
        out[before] += 0.5 * (1 + np.cos(np.pi * d[before] / lead))
        out[after] += 0.5 * (1 + np.cos(np.pi * d[after] / trail))
    return np.minimum(out, 1.5)


@register("falcon_marks")
class FalconMarks(Signal):
    """Parent taps from an XbotGo Falcon ``.mark`` file.

    Params
    ------
    file: path to the ``.mark`` file. Empty means "look next to the video".
    lead_seconds / trail_seconds: shape of the bump around each tap
        (default 8 / 2 — people tap after the moment, not before it).
    """

    description = (
        "Parent taps from an XbotGo Falcon .mark file (params: file) — "
        "someone at the game saying 'that one'."
    )
    # The bump is 0-1; anything under a quarter is the tail of a tap.
    noise_floor = 0.25

    def applicable(self, ctx: AnalysisContext) -> bool:
        if self._resolve(ctx) is None:
            warnings.warn(
                f"falcon_marks is enabled but no .mark file was found for "
                f"{Path(ctx.info.path).name} (looked for params: file, then <video>.mark)",
                stacklevel=2,
            )
            return False
        return True

    def compute(self, ctx: AnalysisContext) -> np.ndarray:
        from ..marks import read_marks

        path = self._resolve(ctx)
        if path is None:
            return np.zeros(ctx.n)
        clicks = read_marks(path, duration=ctx.info.duration).clicks
        return mark_bumps(
            ctx.times,
            clicks,
            lead=float(self.params.get("lead_seconds", 8.0)),
            trail=float(self.params.get("trail_seconds", 2.0)),
        )

    def _resolve(self, ctx: AnalysisContext) -> Path | None:
        from ..marks import find_mark_file

        explicit = str(self.params.get("file") or "").strip()
        if explicit:
            path = Path(explicit)
            return path if path.is_file() else None
        original = ctx.extras.get("source_path") or ctx.info.path
        return find_mark_file(original)
