"""XbotGo Falcon ``.mark`` files, and the plain mark-driven cut plan.

During a game a parent taps a button in the XbotGo app; the app exports a
``.mark`` file next to the recording. It is a JSON list of small objects::

    [{"type": "start", "time": 0}, {"type": "click", "time": 549.0}, ...]

``time`` is seconds from the start of the recording. Only ``click`` entries
mean anything — the ``start`` entry at 0 is the recording starting — and a
click is *not* proof that anything in particular happened: it is a parent
saying "something worth seeing happened near here". Everything below treats
it that way. Nothing here ever invents a mark time; marks outside the video
are reported and dropped, never moved.

:func:`plan_from_marks` reproduces the Falcon Highlights runner rules exactly,
so a reel built here matches one built with ``falcon_runner.py``:

* pad ``before`` / ``after`` seconds around each click (default 10 / 10),
* merge windows that overlap or sit within ``merge_gap`` seconds (default 2),
* name clips ``clip01_mark_9-09.mp4`` after the first mark they contain,
  with ``_merged`` appended when a clip holds more than one mark.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MarkFileError",
    "MarkSet",
    "FalconClip",
    "read_marks",
    "parse_marks",
    "find_mark_file",
    "plan_from_marks",
    "merge_windows",
    "slug_clock",
    "fmt_clock",
    "number_clips",
]

DEFAULT_PAD = 10.0
DEFAULT_MERGE_GAP = 2.0


class MarkFileError(ValueError):
    """The .mark file could not be read as a Falcon mark list."""


@dataclass
class MarkSet:
    """The click marks of one recording, plus what was ignored and why."""

    clicks: list[float]
    source: str = ""
    ignored: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.clicks)


@dataclass
class FalconClip:
    """One clip of a Falcon highlight plan.

    ``kind`` is ``"mark"`` when the clip exists because of one or more parent
    taps, ``"auto"`` when a detector proposed it with no tap nearby.
    """

    id: str
    kind: str
    start: float
    end: float
    marks: list[float] = field(default_factory=list)
    filename: str = ""
    keep: bool = True
    score: float = 0.0
    peak_time: float | None = None
    note: str = ""
    reasons: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def merged(self) -> bool:
        return len(self.marks) > 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        data["duration"] = round(self.duration, 3)
        data["reasons"] = {k: round(v, 4) for k, v in self.reasons.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FalconClip:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# ------------------------------------------------------------------- parsing


def find_mark_file(video: str | Path) -> Path | None:
    """The ``.mark`` file that belongs to ``video``, if one sits next to it.

    Tries ``match.mark`` and ``match.mp4.mark`` (both seen in the wild), then a
    case-insensitive match of either in the same folder.
    """
    video = Path(video)
    candidates = [video.with_suffix(".mark"), video.with_name(video.name + ".mark")]
    for c in candidates:
        if c.is_file():
            return c
    if video.parent.is_dir():
        wanted = {c.name.lower() for c in candidates}
        for entry in video.parent.iterdir():
            if entry.is_file() and entry.name.lower() in wanted:
                return entry
    return None


def read_marks(path: str | Path, *, duration: float | None = None) -> MarkSet:
    """Read and parse a ``.mark`` file. See :func:`parse_marks`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise MarkFileError(f"Could not read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MarkFileError(
            f"{path.name} is not valid JSON ({exc.msg} at line {exc.lineno})"
        ) from exc
    marks = parse_marks(data, duration=duration)
    marks.source = str(path)
    return marks


def parse_marks(data: Any, *, duration: float | None = None) -> MarkSet:
    """Click times (seconds, sorted, de-duplicated) from parsed ``.mark`` JSON.

    Accepts the Falcon list format; a ``{"marks": [...]}`` wrapper is tolerated.
    Entries that are not clicks are skipped silently (``start`` is expected);
    clicks without a usable time, negative times, and — when ``duration`` is
    given — times past the end of the video are skipped *and reported* in
    ``ignored``, because a parent will want to know a tap went missing.
    """
    if isinstance(data, dict) and isinstance(data.get("marks"), list):
        data = data["marks"]
    if not isinstance(data, list):
        raise MarkFileError("A .mark file should be a JSON list of {type, time} objects")

    clicks: list[float] = []
    ignored: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            ignored.append(f"entry {index}: not an object")
            continue
        if str(item.get("type", "")).strip().lower() != "click":
            continue
        raw = item.get("time")
        try:
            t = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ignored.append(f"entry {index}: click without a usable time ({raw!r})")
            continue
        if not math.isfinite(t) or t < 0:
            ignored.append(f"entry {index}: click at invalid time {raw!r}")
            continue
        if duration is not None and t > duration:
            ignored.append(
                f"entry {index}: click at {fmt_clock(t)} is after the video ends ({fmt_clock(duration)})"
            )
            continue
        clicks.append(t)
    return MarkSet(clicks=sorted(set(clicks)), ignored=ignored)


# ------------------------------------------------------------------ planning


def merge_windows(
    windows: list[tuple[float, float, list[float]]], merge_gap: float = DEFAULT_MERGE_GAP
) -> list[tuple[float, float, list[float]]]:
    """Merge windows that overlap or sit within ``merge_gap`` seconds.

    Same rule as the runner: a window joins the previous one when it starts at
    or before ``previous end + merge_gap``.
    """
    merged: list[list[Any]] = []
    for start, end, marks in sorted(windows, key=lambda w: (w[0], w[1])):
        if merged and start <= merged[-1][1] + merge_gap:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2].extend(marks)
        else:
            merged.append([start, end, list(marks)])
    return [(float(s), float(e), sorted(m)) for s, e, m in merged]


def plan_from_marks(
    marks: MarkSet | list[float],
    duration: float,
    *,
    before: float = DEFAULT_PAD,
    after: float = DEFAULT_PAD,
    merge_gap: float = DEFAULT_MERGE_GAP,
) -> list[FalconClip]:
    """Marks-only plan: the runner's pad / merge / naming rules, exactly."""
    clicks = marks.clicks if isinstance(marks, MarkSet) else sorted({float(m) for m in marks})
    windows = [(max(0.0, t - before), min(duration, t + after), [t]) for t in clicks]
    clips = [
        FalconClip(
            id="",
            kind="mark",
            start=round(s, 3),
            end=round(e, 3),
            marks=m,
            note=" + ".join(fmt_clock(x) for x in m),
        )
        for s, e, m in merge_windows(windows, merge_gap)
    ]
    return number_clips(clips)


def number_clips(clips: list[FalconClip]) -> list[FalconClip]:
    """Give clips their IDs and filenames, in time order.

    ``clip01_mark_9-09.mp4`` (``_merged`` when it holds several marks) for mark
    clips; ``clip02_auto_23-10.mp4`` for detector-only clips, named after the
    detector's peak.
    """
    ordered = sorted(clips, key=lambda c: (c.start, c.end))
    for i, clip in enumerate(ordered, start=1):
        clip.id = f"clip{i:02d}"
        if clip.kind == "mark" and clip.marks:
            name = f"{clip.id}_mark_{slug_clock(clip.marks[0])}"
            if clip.merged:
                name += "_merged"
        else:
            anchor = clip.peak_time if clip.peak_time is not None else clip.start
            name = f"{clip.id}_auto_{slug_clock(anchor)}"
        clip.filename = name + ".mp4"
    return ordered


# ------------------------------------------------------------------ clocks


def slug_clock(seconds: float) -> str:
    """``549`` -> ``9-09``; ``3725`` -> ``1-02-05``. Safe in a filename.

    Rounds to the whole second first, so 59.6 s reads ``1-00`` rather than the
    runner's ``0-60``; every other value matches the runner.
    """
    total = int(round(max(0.0, seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}-{m:02d}-{s:02d}" if h else f"{m}-{s:02d}"


def fmt_clock(seconds: float) -> str:
    """``549.5`` -> ``9:09.5`` — for people, not filenames."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    body = f"{s:05.2f}".rstrip("0").rstrip(".")
    if len(body.split(".")[0]) < 2:
        body = "0" + body
    return f"{h}:{m:02d}:{body}" if h else f"{m}:{body}"
