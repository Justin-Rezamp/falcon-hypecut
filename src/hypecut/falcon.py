"""XbotGo Falcon footage in, a parent-ready highlight reel out.

Three modes, chosen per run:

``marks``
    Parent taps only. Each ``click`` in the ``.mark`` file becomes a padded
    window; windows that overlap or sit within 2 s merge; clips are named
    ``clip01_mark_9-09.mp4``. Identical to the Falcon Highlights runner, so
    nothing here second-guesses the parent. Fast: no video analysis at all.

``hybrid``
    Taps are the strongest evidence, and the detectors only *refine* them.
    Every tap still yields a clip, but the window is slid (a few seconds at
    most, never so far that the tap falls out) to centre on the action peak
    the detectors found near it — people tap after the moment, so the peak is
    usually a little before the tap. Optionally, ``extras`` detector-only
    clips the parent did not tap are added, clearly labelled ``auto``.

``detect``
    No taps: HypeCut's own detectors with the Falcon profile, which
    compensates for the camera's pan/tilt/zoom (see :mod:`hypecut.camera`).

Analysis always runs on a small proxy when the source is HEVC or larger than
1080p; cutting always reads the original. Clips are stream-copied when that is
safe (H.264 video, AAC or no audio) and re-encoded to H.264 otherwise.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import ffmpeg as ff
from .config import Config, ReframeConfig, RenderConfig, load_config
from .marks import (
    DEFAULT_MERGE_GAP,
    DEFAULT_PAD,
    FalconClip,
    MarkSet,
    find_mark_file,
    fmt_clock,
    merge_windows,
    number_clips,
    plan_from_marks,
    read_marks,
)
from .types import AnalysisContext, Candidate, VideoInfo

__all__ = [
    "MODES",
    "SourceInfo",
    "FalconOptions",
    "FalconPlan",
    "FalconOutputs",
    "probe_source",
    "proxy_reason",
    "make_proxy",
    "falcon_config",
    "plan_falcon",
    "render_falcon",
    "load_plan",
    "plan_from_dict",
]

MODES = ("marks", "hybrid", "detect")
Progress = Callable[[float, str], None]

#: Codecs we will stream-copy into the reel. Everything else is re-encoded to
#: H.264 so the reel plays on any phone, TV or Windows PC without a codec pack.
COPY_SAFE_VIDEO = {"h264"}
COPY_SAFE_AUDIO = {"aac", None}


def _noop(_p: float, _m: str) -> None:
    return None


# ------------------------------------------------------------------- source


@dataclass
class SourceInfo:
    """What ffprobe says about the recording."""

    path: str
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None
    pix_fmt: str = ""

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def copy_safe(self) -> bool:
        return self.video_codec in COPY_SAFE_VIDEO and self.audio_codec in COPY_SAFE_AUDIO

    def video_info(self) -> VideoInfo:
        return VideoInfo(
            path=self.path,
            duration=self.duration,
            fps=self.fps,
            width=self.width,
            height=self.height,
            has_audio=self.has_audio,
        )


def probe_source(path: str | Path) -> SourceInfo:
    """Codec, geometry and duration of a recording."""
    info = ff.probe(path)
    raw = ff.run(
        ff.cmd("ffprobe -v error -print_format json -show_streams {src}", src=str(path)),
        capture=True,
    )
    streams = json.loads(raw or b"{}").get("streams", [])
    video: dict[str, Any] = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return SourceInfo(
        path=str(path),
        duration=info.duration,
        width=info.width,
        height=info.height,
        fps=info.fps,
        video_codec=str(video.get("codec_name") or "").lower(),
        audio_codec=str(audio.get("codec_name")).lower() if audio else None,
        pix_fmt=str(video.get("pix_fmt") or ""),
    )


def proxy_reason(src: SourceInfo) -> str:
    """Why this source should be analysed through a proxy, or ``""``."""
    reasons = []
    if src.video_codec in {"hevc", "h265"}:
        reasons.append("HEVC (H.265) is slow to decode")
    if src.height > 1080 or src.width > 1920:
        reasons.append(f"{src.width}x{src.height} is larger than 1080p")
    return " and ".join(reasons)


def make_proxy(src: SourceInfo, cache_dir: str | Path, *, progress: Progress | None = None) -> Path:
    """A small H.264 copy for analysis only. Cached by file identity.

    640x360 at the source frame rate keeps every frame the analysis will
    sample, decodes in a fraction of the time of 4K HEVC, and is thrown away
    afterwards — the reel is always cut from the original.
    """
    progress = progress or _noop
    source = Path(src.path)
    stat = source.stat()
    key = hashlib.sha1(
        f"{source.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode()
    ).hexdigest()[:12]
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{source.stem}.proxy-{key}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    progress(0.0, "making analysis proxy")
    tmp = dest.with_suffix(".part.mp4")
    args = ff.cmd(
        "ffmpeg -v error -nostdin -y -i {src} -map 0:v:0 -map 0:a:0? -vf {vf} "
        "-c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p "
        "-c:a aac -ac 1 -ar 16000 -b:a 64k {dst}",
        src=str(source),
        vf="scale=-2:360:flags=fast_bilinear",
        dst=str(tmp),
    )
    ff.run(args)
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------- config

#: Where shipped profiles live when running from a checkout.
_REPO_CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def falcon_config(profile: str | Path | None = None, sport: str = "soccer") -> Config:
    """The Falcon profile for ``sport``, or an explicit profile path."""
    if profile:
        return load_config(profile)
    name = f"falcon-{sport}.yaml"
    for root in (Path.cwd() / "configs", _REPO_CONFIGS):
        candidate = root / name
        if candidate.is_file():
            return load_config(candidate)
    raise FileNotFoundError(
        f"Could not find configs/{name}. Run from the HypeCut folder or pass --profile."
    )


# ------------------------------------------------------------------- options


@dataclass
class FalconOptions:
    """Every knob of a Falcon run. Defaults are the runner's rules."""

    mode: str = ""  # "" = marks when a .mark file is found, else detect
    before: float = DEFAULT_PAD
    after: float = DEFAULT_PAD
    merge_gap: float = DEFAULT_MERGE_GAP
    sport: str = "soccer"
    profile: str | None = None
    # hybrid: how far a window may slide toward the action peak around a tap.
    max_shift_before: float = 6.0
    max_shift_after: float = 3.0
    # hybrid: a peak only moves a window if it beats this percentile of the
    # whole game's detector curve — a clear moment, not the busiest jog.
    peak_percentile: float = 90.0
    # hybrid/detect: detector-only clips to add on top of the tapped ones.
    extras: int = 0
    cut: str = "auto"  # auto | copy | encode
    crf: int = 20
    phone: bool = False
    vertical: bool = False
    loudnorm: bool = False
    proxy: str = "auto"  # auto | always | never
    cache_dir: str | None = None

    def validate(self) -> None:
        if self.mode and self.mode not in MODES:
            raise ValueError(f"Unknown mode {self.mode!r}; expected one of {MODES}")
        if self.cut not in ("auto", "copy", "encode"):
            raise ValueError("cut must be auto, copy or encode")
        if self.proxy not in ("auto", "always", "never"):
            raise ValueError("proxy must be auto, always or never")
        if self.before < 0 or self.after < 0 or self.merge_gap < 0:
            raise ValueError("pad and merge gap must be zero or more")
        if self.before + self.after <= 0:
            raise ValueError("a clip needs some padding around its mark")


# ------------------------------------------------------------------- plan


@dataclass
class FalconPlan:
    """The whole decision for one game: which clips, from where, and why.

    ``facts`` are things read directly from the files (what the .mark holds,
    what ffprobe reports). ``notes`` are inferences and warnings — a detector
    thinks the action peaked earlier, a tap was past the end of the video.
    Keeping them apart is what lets a parent trust the first list.
    """

    source: str
    mode: str
    duration: float
    clips: list[FalconClip]
    mark_file: str | None = None
    clicks: list[float] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    source_info: dict[str, Any] = field(default_factory=dict)
    analysis_source: str | None = None
    facts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def kept(self) -> list[FalconClip]:
        return [c for c in self.clips if c.keep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "falcon_plan": 1,
            "source": self.source,
            "mark_file": self.mark_file,
            "mode": self.mode,
            "duration": round(self.duration, 3),
            "clicks": self.clicks,
            "settings": self.settings,
            "source_info": self.source_info,
            "analysis_source": self.analysis_source,
            "facts": self.facts,
            "notes": self.notes,
            "reel_duration": round(sum(c.duration for c in self.kept), 3),
            "clips": [c.to_dict() for c in self.clips],
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path


def load_plan(path: str | Path) -> FalconPlan:
    """Read a plan written by :meth:`FalconPlan.save` (edits to ``keep``/times welcome)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("falcon_plan") != 1:
        raise ValueError(f"{path} is not a Falcon plan (expected a falcon_plan.json)")
    return plan_from_dict(data)


def plan_from_dict(data: dict[str, Any]) -> FalconPlan:
    """Rebuild a plan from :meth:`FalconPlan.to_dict` output (the web UI keeps it in memory)."""
    if not isinstance(data, dict) or data.get("falcon_plan") != 1:
        raise ValueError("not a Falcon plan")
    clips = [FalconClip.from_dict(c) for c in data.get("clips", [])]
    for c in clips:
        if c.end <= c.start:
            raise ValueError(f"{c.id}: end ({c.end}) must be after start ({c.start})")
    return FalconPlan(
        source=data["source"],
        mode=data.get("mode", "marks"),
        duration=float(data.get("duration", 0.0)),
        clips=clips,
        mark_file=data.get("mark_file"),
        clicks=list(data.get("clicks", [])),
        settings=dict(data.get("settings", {})),
        source_info=dict(data.get("source_info", {})),
        analysis_source=data.get("analysis_source"),
        facts=list(data.get("facts", [])),
        notes=list(data.get("notes", [])),
    )


def plan_falcon(
    video: str | Path,
    *,
    marks: str | Path | None = None,
    options: FalconOptions | None = None,
    progress: Progress | None = None,
    _ctx_out: dict[str, Any] | None = None,
) -> FalconPlan:
    """Decide the clips for one Falcon recording. No encoding of the reel here."""
    opts = options or FalconOptions()
    opts.validate()
    progress = progress or _noop
    ff.require_ffmpeg()

    progress(0.02, "probing")
    src = probe_source(video)
    facts = [
        f"Video: {Path(src.path).name}, {fmt_clock(src.duration)} long, "
        f"{src.width}x{src.height} {src.video_codec or '?'}"
        + (f" with {src.audio_codec} audio" if src.audio_codec else ", no audio")
    ]
    notes: list[str] = []

    mark_path = Path(marks) if marks else find_mark_file(video)
    if marks and not Path(marks).is_file():
        raise FileNotFoundError(f"Mark file not found: {marks}")
    markset: MarkSet | None = None
    if mark_path is not None:
        markset = read_marks(mark_path, duration=src.duration)
        facts.append(
            f"Marks: {mark_path.name} has {len(markset)} click(s)"
            + (": " + ", ".join(fmt_clock(t) for t in markset.clicks) if markset.clicks else "")
        )
        notes.extend(f"Ignored in .mark — {why}" for why in markset.ignored)

    mode = opts.mode or ("marks" if markset is not None else "detect")
    if mode in ("marks", "hybrid") and markset is None:
        raise FileNotFoundError(
            f"Mode {mode!r} needs a .mark file: put {Path(video).with_suffix('.mark').name} next "
            "to the video or pass --marks"
        )
    if mode in ("marks", "hybrid") and markset is not None and not markset.clicks:
        raise ValueError(f"{mark_path.name if mark_path else '.mark'} has no click marks to cut")

    settings = {
        "mode": mode,
        "before": opts.before,
        "after": opts.after,
        "merge_gap": opts.merge_gap,
        "sport": opts.sport,
        "extras": opts.extras,
        "max_shift_before": opts.max_shift_before,
        "max_shift_after": opts.max_shift_after,
    }

    plan = FalconPlan(
        source=str(Path(src.path).resolve()),
        mode=mode,
        duration=src.duration,
        clips=[],
        mark_file=str(Path(mark_path).resolve()) if mark_path else None,
        clicks=list(markset.clicks) if markset else [],
        settings=settings,
        source_info=asdict(src),
        facts=facts,
        notes=notes,
    )

    if mode == "marks":
        plan.clips = plan_from_marks(
            markset or [],
            src.duration,
            before=opts.before,
            after=opts.after,
            merge_gap=opts.merge_gap,
        )
        progress(1.0, "planned")
        return plan

    # hybrid / detect need to look at the footage.
    ctx, cfg, analysis_path = _analysis_context(src, opts, progress, plan)
    if _ctx_out is not None:
        _ctx_out["ctx"] = ctx
    if mode == "hybrid":
        plan.clips = _hybrid_clips(ctx, cfg, src, markset, opts, plan)  # type: ignore[arg-type]
    else:
        plan.clips = _detect_clips(ctx, cfg, src, opts, plan)
    progress(1.0, "planned")
    return plan


def _analysis_context(
    src: SourceInfo, opts: FalconOptions, progress: Progress, plan: FalconPlan
) -> tuple[AnalysisContext, Config, str]:
    from .pipeline import _build_context

    cfg = falcon_config(opts.profile, opts.sport)
    # Marks are handled explicitly in hybrid mode; never double-count them.
    if "falcon_marks" in cfg.signals.enabled:
        cfg = cfg.merged(
            {"signals": {"enabled": [s for s in cfg.signals.enabled if s != "falcon_marks"]}}
        )
    analysis_path = src.path
    reason = proxy_reason(src)
    if opts.proxy == "always" or (opts.proxy == "auto" and reason):
        cache = opts.cache_dir or str(Path(tempfile.gettempdir()) / "hypecut-falcon-proxies")
        analysis_path = str(make_proxy(src, cache, progress=progress))
        plan.notes.append(
            f"Analysed a 360p proxy ({reason or 'requested'}); clips are cut from the original."
        )
    plan.analysis_source = analysis_path
    progress(0.1, "decoding for analysis")
    info = ff.probe(analysis_path)
    ctx = _build_context(info, cfg)
    ctx.extras["source_path"] = src.path
    return ctx, cfg, analysis_path


def _tracks_and_curve(ctx: AnalysisContext, cfg: Config):
    from .fusion import fuse
    from .pipeline import _run_signals

    tracks = _run_signals(ctx, cfg)
    curve = (
        fuse(tracks, grid_fps=cfg.signals.grid_fps, smooth_seconds=cfg.signals.smooth_seconds)
        if tracks
        else np.zeros(ctx.n)
    )
    return tracks, curve


def _hybrid_clips(
    ctx: AnalysisContext,
    cfg: Config,
    src: SourceInfo,
    markset: MarkSet,
    opts: FalconOptions,
    plan: FalconPlan,
) -> list[FalconClip]:
    from .fusion import explain

    tracks, curve = _tracks_and_curve(ctx, cfg)
    times = ctx.times
    n = curve.size
    if n == 0 or not tracks:
        plan.notes.append("No usable detector signal; fell back to plain mark windows.")
        return plan_from_marks(
            markset, src.duration, before=opts.before, after=opts.after, merge_gap=opts.merge_gap
        )

    # "Clear action" means the peak stands out from this game's ordinary
    # level, not merely from its neighbours — otherwise every tap would be
    # dragged toward whichever second of jogging happened to be busiest.
    bar = float(np.percentile(curve, opts.peak_percentile))

    def idx(t: float) -> int:
        return int(min(n - 1, max(0, round(t * ctx.grid_fps))))

    windows: list[tuple[float, float, list[float]]] = []
    shift_of: dict[float, tuple[float, float]] = {}
    for t in markset.clicks:
        lo, hi = idx(t - opts.max_shift_before), idx(t + opts.max_shift_after)
        seg = curve[lo : hi + 1]
        shift = 0.0
        peak_t = t
        if seg.size:
            p = lo + int(np.argmax(seg))
            if curve[p] >= bar and curve[p] - float(np.median(seg)) > 0.05:
                peak_t = float(times[p])
                shift = peak_t - t
        # The tap must always stay inside its own clip.
        shift = float(np.clip(shift, -opts.before + 0.5, opts.after - 0.5))
        shift_of[t] = (shift, peak_t)
        windows.append(
            (max(0.0, t + shift - opts.before), min(src.duration, t + shift + opts.after), [t])
        )

    clips: list[FalconClip] = []
    for start, end, ms in merge_windows(windows, opts.merge_gap):
        i0, i1 = idx(start), max(idx(start) + 1, idx(end))
        shifts = [shift_of[m][0] for m in ms]
        peaks = [shift_of[m][1] for m in ms]
        reasons = explain(tracks, i0, i1)
        reasons["falcon_marks"] = float(len(ms))
        clip = FalconClip(
            id="",
            kind="mark",
            start=round(start, 3),
            end=round(end, 3),
            marks=ms,
            score=round(1.0 + float(curve[i0:i1].mean()), 4),
            peak_time=round(peaks[0], 3),
            note=" + ".join(fmt_clock(m) for m in ms),
            reasons=reasons,
            meta={"shift": [round(s, 2) for s in shifts]},
        )
        for m, s in zip(ms, shifts, strict=True):
            if abs(s) >= 0.5:
                plan.notes.append(
                    f"Tap at {fmt_clock(m)}: detectors put the action peak {abs(s):.1f}s "
                    f"{'before' if s < 0 else 'after'} it, so its window slid that way "
                    "(an inference, not a fact)."
                )
        clips.append(clip)

    if opts.extras > 0:
        extras = _auto_candidates(ctx, cfg, tracks, curve, src)
        taken = [(c.start - opts.merge_gap, c.end + opts.merge_gap) for c in clips]
        added = 0
        for cand in sorted(extras, key=lambda c: c.score, reverse=True):
            if added >= opts.extras:
                break
            if any(cand.start < hi and lo < cand.end for lo, hi in taken):
                continue
            clips.append(_auto_clip(cand))
            taken.append((cand.start - opts.merge_gap, cand.end + opts.merge_gap))
            added += 1
        if added:
            plan.notes.append(
                f"Added {added} detector-only clip(s) nobody tapped (named _auto_). "
                "A detector cannot tell a goal from a scramble — review before sharing."
            )
    return number_clips(clips)


def _detect_clips(
    ctx: AnalysisContext, cfg: Config, src: SourceInfo, opts: FalconOptions, plan: FalconPlan
) -> list[FalconClip]:
    tracks, curve = _tracks_and_curve(ctx, cfg)
    cands = _auto_candidates(ctx, cfg, tracks, curve, src)
    if opts.extras > 0:
        cands = sorted(cands, key=lambda c: c.score, reverse=True)[: opts.extras]
    plan.notes.append(
        "No parent taps: every clip was proposed by the detectors (named _auto_). "
        "They find busy moments, not necessarily goals."
    )
    if not cands:
        plan.notes.append("The detectors found nothing that stands out in this video.")
    return number_clips([_auto_clip(c) for c in cands])


def _auto_candidates(ctx, cfg, tracks, curve, src: SourceInfo) -> list[Candidate]:
    from .fusion import prominence
    from .segments import build_candidates, merge, select

    if curve.size == 0 or not tracks:
        return []
    strength = prominence(
        tracks, grid_fps=cfg.signals.grid_fps, smooth_seconds=cfg.signals.smooth_seconds
    )
    if cfg.segments.min_prominence > 0 and strength < cfg.segments.min_prominence:
        return []
    cands = build_candidates(
        curve,
        ctx.times,
        cfg.segments,
        grid_fps=cfg.signals.grid_fps,
        duration=src.duration,
        tracks=tracks,
    )
    return select(merge(cands, cfg.segments), cfg.segments)


def _auto_clip(cand: Candidate) -> FalconClip:
    peak = float(cand.meta.get("peak_time", (cand.start + cand.end) / 2))
    return FalconClip(
        id="",
        kind="auto",
        start=round(cand.start, 3),
        end=round(cand.end, 3),
        score=round(float(cand.score), 4),
        peak_time=round(peak, 3),
        note=f"detector peak {fmt_clock(peak)}",
        reasons=dict(cand.reasons),
    )


# ------------------------------------------------------------------- render


@dataclass
class FalconOutputs:
    out_dir: Path
    highlights: Path
    clips: list[Path]
    plan: Path
    phone: Path | None = None
    vertical: Path | None = None
    cut_mode: str = "copy"
    notes: list[str] = field(default_factory=list)


def render_falcon(
    plan: FalconPlan,
    out_dir: str | Path,
    options: FalconOptions | None = None,
    *,
    progress: Progress | None = None,
    ctx: AnalysisContext | None = None,
) -> FalconOutputs:
    """Cut every kept clip, join them into ``highlights.mp4``, add exports."""
    opts = options or FalconOptions()
    opts.validate()
    progress = progress or _noop
    ff.require_ffmpeg()
    kept = plan.kept
    if not kept:
        raise ValueError("No clips are kept — nothing to cut.")

    out = Path(out_dir)
    clips_dir = out / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    src = probe_source(plan.source)
    notes: list[str] = []

    cut_mode = "copy" if opts.cut == "copy" or opts.cut == "auto" and src.copy_safe else "encode"
    if cut_mode == "encode" and opts.cut == "auto":
        notes.append(
            f"Re-encoding clips to H.264 because the source is {src.video_codec}"
            + (f"/{src.audio_codec}" if src.audio_codec else "")
            + " (stream copy is only used for H.264 with AAC audio)."
        )
    if cut_mode == "copy":
        notes.append(
            "Clips were stream-copied (no quality loss); each may start up to a couple of "
            "seconds early, at the nearest keyframe."
        )

    paths: list[Path] = []
    for i, clip in enumerate(kept):
        dest = clips_dir / clip.filename
        _cut_clip(src, clip, dest, cut_mode, opts.crf)
        paths.append(dest)
        progress(0.7 * (i + 1) / len(kept), f"cut {clip.id}")

    highlights = out / "highlights.mp4"
    if highlights.exists():
        notes.append(f"Replaced the existing {highlights.name}.")
    _join(paths, highlights, loudnorm=opts.loudnorm)
    progress(0.8, "joined")

    plan_path = plan.save(out / "falcon_plan.json")

    phone = vertical = None
    if opts.phone:
        phone = out / "highlights_phone.mp4"
        _phone_export(highlights, phone, loudnorm=opts.loudnorm)
        progress(0.88, "phone export")
    if opts.vertical:
        vertical = out / "highlights_vertical.mp4"
        _vertical_export(plan, src, kept, vertical, opts, ctx=ctx)
        progress(0.98, "vertical export")

    progress(1.0, "done")
    return FalconOutputs(
        out_dir=out,
        highlights=highlights,
        clips=paths,
        plan=plan_path,
        phone=phone,
        vertical=vertical,
        cut_mode=cut_mode,
        notes=notes,
    )


def _cut_clip(src: SourceInfo, clip: FalconClip, dest: Path, mode: str, crf: int) -> None:
    dur = f"{clip.duration:.3f}"
    start = f"{clip.start:.3f}"
    amap = ["-map", "0:a:0?"]
    if mode == "copy":
        args = ff.cmd(
            "ffmpeg -v error -nostdin -y -ss {start} -i {src} -t {dur}",
            start=start,
            src=src.path,
            dur=dur,
        )
        args += ["-map", "0:v:0", *amap, "-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        args = ff.cmd(
            "ffmpeg -v error -nostdin -y -accurate_seek -ss {start} -i {src} -t {dur}",
            start=start,
            src=src.path,
            dur=dur,
        )
        args += ["-map", "0:v:0", *amap]
        args += ff.cmd(
            "-c:v libx264 -preset veryfast -crf {crf} -pix_fmt yuv420p "
            "-c:a aac -b:a 192k -ar 48000 -ac 2",
            crf=str(crf),
        )
    args += ["-movflags", "+faststart", str(dest)]
    ff.run(args)


def _join(parts: list[Path], dest: Path, *, loudnorm: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.parent / f".{dest.stem}.concat.txt"
    lines = []
    for p in parts:
        escaped = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    listing.write_text("".join(lines), encoding="utf-8")
    tmp = dest.with_name(f".{dest.stem}.part.mp4")
    try:
        args = ff.cmd(
            "ffmpeg -v error -nostdin -y -f concat -safe 0 -fflags +genpts -i {listing}",
            listing=str(listing),
        )
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
        if loudnorm:
            args += [
                "-c:v",
                "copy",
                "-af",
                _LOUDNORM,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
            ]
        else:
            args += ["-c", "copy"]
        args += ["-movflags", "+faststart", str(tmp)]
        ff.run(args)
        tmp.replace(dest)
    finally:
        listing.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


#: Single-pass EBU R128 to -16 LUFS: the usual target for phones and the web.
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _phone_export(src: Path, dest: Path, *, loudnorm: bool) -> None:
    """1080p-or-smaller H.264/AAC with faststart: plays everywhere, shares fast."""
    vf = (
        "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )
    args = ff.cmd("ffmpeg -v error -nostdin -y -i {src}", src=str(src))
    args += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", vf]
    args += ff.cmd(
        "-c:v libx264 -preset veryfast -crf 21 -profile:v high -level 4.1 -pix_fmt yuv420p "
        "-c:a aac -b:a 160k -ar 48000 -ac 2"
    )
    if loudnorm:
        args += ["-af", _LOUDNORM]
    args += ["-movflags", "+faststart", str(dest)]
    ff.run(args)


def _vertical_export(
    plan: FalconPlan,
    src: SourceInfo,
    kept: list[FalconClip],
    dest: Path,
    opts: FalconOptions,
    *,
    ctx: AnalysisContext | None,
) -> None:
    """9:16 crop that pans with the action, using HypeCut's reframe planner."""
    from .pipeline import _build_context
    from .reframe import plan_reframe
    from .render import render_reel

    if ctx is None:
        analysis = plan.analysis_source
        if not analysis or not Path(analysis).exists():
            reason = proxy_reason(src)
            if opts.proxy == "always" or (opts.proxy == "auto" and reason):
                cache = opts.cache_dir or str(
                    Path(tempfile.gettempdir()) / "hypecut-falcon-proxies"
                )
                analysis = str(make_proxy(src, cache))
            else:
                analysis = src.path
        cfg = falcon_config(opts.profile, opts.sport)
        ctx = _build_context(ff.probe(analysis), cfg)

    cands = []
    for c in kept:
        anchor = c.peak_time if c.peak_time is not None else (c.marks[0] if c.marks else None)
        anchor = anchor if anchor is not None else (c.start + c.end) / 2
        anchor = float(min(max(anchor, c.start), c.end))
        cands.append(
            Candidate(
                start=c.start,
                end=c.end,
                score=c.score,
                meta={"peak_time": anchor, "event_start": anchor, "event_end": anchor},
            )
        )
    reframe = ReframeConfig(mode="crop", width=1080, height=1920, track=True)
    plan_reframe(ctx, cands, reframe)
    render_cfg = RenderConfig(
        crf=21,
        fade=0.2,
        normalize_audio=False,
        loudness_match=0.9 if opts.loudnorm else 0.0,
        reframe=reframe,
    )
    render_reel(src.video_info(), cands, dest, render_cfg)
