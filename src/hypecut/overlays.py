"""Remotion title cards and lower-thirds for Falcon highlight reels.

The cards live in a small Remotion project (``overlays/`` at the repo root).
This module turns an :class:`OverlayConfig` into JSON props, asks the Remotion
CLI to render the cards, and composes them with a finished reel in a single
ffmpeg pass:

    intro.mp4 + (reel with lower-thirds / title cards / score bar) + outro.mp4

Everything is opt-in. Title text ("Goal!", "Save", ...) only ever comes from
the user's own instruction: a Falcon mark is a parent tap, not proof of what
happened, so nothing here labels a clip by itself.

Rendering runs ``node <overlays>/node_modules/@remotion/cli/remotion-cli.js``,
which is exactly what ``npx remotion`` runs, minus the shell wrapper (``npx``
is a ``.cmd`` file on Windows and would need ``shell=True``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ffmpeg as ff

__all__ = [
    "OverlayConfig",
    "ClipSlot",
    "RemotionRunner",
    "RenderedAssets",
    "parse_title_spec",
    "parse_score",
    "find_project",
    "render_assets",
    "build_compose_command",
    "compose_reel",
    "slots_from_plan",
    "add_overlays_parser",
    "add_falcon_overlay_args",
    "config_from_falcon_args",
    "run_overlays_cli",
]

CLIP_ID = re.compile(r"^clip\d{2,3}$")
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
MAX_TITLE = 24
MAX_TEXT = 48

#: Default card lengths in seconds (must match overlays/src/Root.tsx).
SECONDS = {"intro": 5.0, "outro": 4.0, "lower_third": 5.0, "title": 3.0, "score": 1.0}

#: Where Remotion compositions live inside the project.
ENTRY = "src/index.ts"

Progress = Callable[[float, str], None]


def _noop(_p: float, _m: str) -> None:
    pass


# ------------------------------------------------------------------- config


def parse_title_spec(spec: str) -> tuple[str, str, float | None]:
    """``clip05=Goal!`` or ``clip05=Goal!@4.5`` -> (clip id, text, seconds into clip)."""
    if "=" not in spec:
        raise ValueError(f"--title wants CLIP=TEXT (e.g. clip05=Goal!), got {spec!r}")
    clip, text = spec.split("=", 1)
    clip = clip.strip().lower()
    at: float | None = None
    m = re.match(r"^(.*)@(\d+(?:\.\d+)?)$", text)
    if m:
        text, at = m.group(1), float(m.group(2))
    text = text.strip()
    if not CLIP_ID.match(clip):
        raise ValueError(f"{clip!r} is not a clip ID like clip05")
    if not text:
        raise ValueError(f"{clip}: the title text is empty")
    if len(text) > MAX_TITLE:
        raise ValueError(f"{clip}: keep the title under {MAX_TITLE} characters ({text!r})")
    return clip, text, at


def parse_score(text: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*$", text or "")
    if not m:
        raise ValueError(f"--score wants HOME-AWAY like 2-1, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise ValueError(f"{text[:20]!r}...: keep overlay text under {limit} characters")
    return text


@dataclass
class OverlayConfig:
    """What to put on a reel. Every part is off unless asked for."""

    team: str = ""
    player: str = ""
    number: str = ""
    date: str = ""
    opponent: str = ""
    accent: str = "#FFC41F"
    intro: bool = False
    outro: bool = False
    outro_line: str = ""
    #: Clip IDs that get the player lower-third, or ["all"].
    lower_third: list[str] = field(default_factory=list)
    #: clip ID -> title text, only from the user's own instruction.
    titles: dict[str, str] = field(default_factory=dict)
    #: clip ID -> seconds into the clip where the title appears (default: at the tap).
    title_at: dict[str, float] = field(default_factory=dict)
    score: dict[str, Any] | None = None
    vertical: bool = False
    crf: int = 20

    def validate(self) -> None:
        for name in ("team", "player", "date", "opponent", "outro_line"):
            setattr(self, name, _clean(getattr(self, name)))
        self.number = _clean(self.number, 4).lstrip("#")
        if self.number and not re.match(r"^\d{1,3}$", self.number):
            raise ValueError(f"player number should be 1-3 digits, got {self.number!r}")
        if not HEX.match(self.accent or ""):
            raise ValueError(f"accent should be a hex colour like #FFC41F, got {self.accent!r}")
        self.lower_third = [c.strip().lower() for c in self.lower_third if c.strip()]
        for c in self.lower_third:
            if c != "all" and not CLIP_ID.match(c):
                raise ValueError(f"lower-third: {c!r} is not a clip ID like clip05 (or 'all')")
        clean: dict[str, str] = {}
        for clip, text in self.titles.items():
            cid, txt, _ = parse_title_spec(f"{clip}={text}")
            clean[cid] = txt
        self.titles = clean
        for clip, at in self.title_at.items():
            if clip not in self.titles:
                raise ValueError(f"title time given for {clip} but it has no title")
            if float(at) < 0:
                raise ValueError(f"{clip}: title time must be >= 0")
        if self.score is not None:
            s = dict(self.score)
            for k in ("home", "away"):
                v = int(s.get(k, 0))
                if not 0 <= v <= 99:
                    raise ValueError("score values must be 0-99")
                s[k] = v
            for k in ("homeLabel", "awayLabel"):
                s[k] = _clean(s.get(k, ""), 4)
            self.score = s
        if self.lower_third and not (self.player or self.number):
            raise ValueError("a lower-third needs --player and/or --number")

    @property
    def active(self) -> bool:
        return bool(
            self.intro or self.outro or self.lower_third or self.titles or self.score is not None
        )

    def props(self, **extra: Any) -> dict[str, Any]:
        """JSON props for the Remotion cards (camelCase, matching props.ts)."""
        p: dict[str, Any] = {
            "teamName": self.team,
            "playerName": self.player,
            "playerNumber": self.number,
            "gameDate": self.date,
            "opponent": self.opponent,
            "accent": self.accent,
            "outroLine": self.outro_line or "Thanks for watching",
        }
        if self.score is not None:
            p.update(
                homeScore=self.score["home"],
                awayScore=self.score["away"],
                homeLabel=self.score.get("homeLabel") or "HOME",
                awayLabel=self.score.get("awayLabel") or "AWAY",
            )
        p.update(extra)
        return p

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverlayConfig:
        if not isinstance(data, dict):
            raise ValueError("overlays config must be a JSON object")
        titles: dict[str, str] = {}
        title_at: dict[str, float] = {}
        raw_titles = data.get("titles") or {}
        if not isinstance(raw_titles, dict):
            raise ValueError('"titles" must map clip IDs to text, e.g. {"clip05": "Goal!"}')
        for clip, text in raw_titles.items():
            cid, txt, at = parse_title_spec(f"{clip}={text}")
            titles[cid] = txt
            if at is not None:
                title_at[cid] = at
        for clip, at in (data.get("titleAt") or {}).items():
            title_at[str(clip).lower()] = float(at)
        lt = data.get("lowerThird", [])
        if lt is True:
            lt = ["all"]
        elif lt in (False, None):
            lt = []
        elif isinstance(lt, str):
            lt = lt.split(",")
        score = data.get("score")
        if isinstance(score, str):
            h, a = parse_score(score)
            score = {"home": h, "away": a}
        fmt = str(data.get("format", "16:9")).replace("x", ":")
        if fmt not in ("16:9", "9:16"):
            raise ValueError('"format" must be "16:9" or "9:16"')
        cfg = cls(
            team=data.get("team", ""),
            player=data.get("player", ""),
            number=str(data.get("number", "") or ""),
            date=data.get("date", ""),
            opponent=data.get("opponent", ""),
            accent=data.get("accent", "#FFC41F"),
            intro=bool(data.get("intro", False)),
            outro=bool(data.get("outro", False)),
            outro_line=data.get("outroLine", ""),
            lower_third=list(lt),
            titles=titles,
            title_at=title_at,
            score=score,
            vertical=fmt == "9:16",
            crf=int(data.get("crf", 20)),
        )
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> OverlayConfig:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: not valid JSON ({exc})") from exc
        return cls.from_dict(data)


# ----------------------------------------------------------------- remotion


def find_project(explicit: str | Path | None = None) -> Path:
    """Locate the Remotion project: flag, $HYPECUT_OVERLAYS_DIR, repo root, cwd."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("HYPECUT_OVERLAYS_DIR"):
        candidates.append(Path(os.environ["HYPECUT_OVERLAYS_DIR"]))
    candidates.append(Path(__file__).resolve().parents[2] / "overlays")
    candidates.append(Path.cwd() / "overlays")
    for c in candidates:
        if (c / "package.json").is_file() and (c / ENTRY).is_file():
            return c.resolve()
    raise FileNotFoundError(
        "Could not find the overlays/ Remotion project. Pass --overlays-dir or set "
        "HYPECUT_OVERLAYS_DIR."
    )


_BROWSER_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _node_arch(node: str) -> str:
    try:
        out = subprocess.run(
            [node, "-p", "process.platform + '-' + process.arch"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class RemotionRunner:
    """Builds and runs ``remotion render`` / ``remotion still`` commands."""

    project: Path
    node: str = "node"
    browser: str | None = None
    binaries_dir: str | None = None
    platform: str = ""
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    log: list[str] = field(default_factory=list)

    @classmethod
    def detect(
        cls,
        project: str | Path | None = None,
        *,
        browser: str | None = None,
        node: str | None = None,
    ) -> RemotionRunner:
        proj = find_project(project)
        node = node or shutil.which("node") or "node"
        plat = _node_arch(node)
        r = cls(project=proj, node=node, platform=plat)
        r.browser = browser or os.environ.get("HYPECUT_REMOTION_BROWSER") or None
        if plat == "win32-arm64":
            # Remotion ships no Chrome Headless Shell and no compositor for
            # Windows on ARM. Use an installed Chrome/Edge, and the x64
            # compositor (runs under Windows' x64 emulation), fetched by
            # `hypecut overlays setup` into overlays/.remotion-bin/.
            if not r.browser:
                r.browser = next((p for p in _BROWSER_PATHS if Path(p).is_file()), None)
            bin_dir = proj / ".remotion-bin" / "win32-x64"
            if (bin_dir / "remotion.exe").is_file():
                r.binaries_dir = str(bin_dir)
        return r

    @property
    def cli_js(self) -> Path:
        return self.project / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"

    def _base(self, verb: str, composition: str, out: Path, props: Path) -> list[str]:
        args = [
            self.node,
            str(self.cli_js),
            verb,
            ENTRY,
            composition,
            # Remotion runs with cwd = the project, so relative paths would land there.
            str(Path(out).resolve()),
            f"--props={Path(props).resolve()}",
            "--log=error",
        ]
        if self.browser:
            args.append(f"--browser-executable={self.browser}")
        if self.binaries_dir:
            args.append(f"--binaries-directory={self.binaries_dir}")
        return args

    def render_args(
        self,
        composition: str,
        out: Path,
        props: Path,
        *,
        transparent: bool,
        scale: float = 1.0,
        crf: int = 16,
    ) -> list[str]:
        args = self._base("render", composition, out, props)
        if transparent:
            # ProRes 4444 keeps the alpha channel and decodes everywhere ffmpeg does.
            args += [
                "--codec=prores",
                "--prores-profile=4444",
                "--image-format=png",
                "--pixel-format=yuva444p10le",
            ]
        else:
            args += ["--codec=h264", f"--crf={crf}", "--pixel-format=yuv420p"]
        if scale != 1.0:
            args.append(f"--scale={scale:g}")
        return args

    def still_args(self, composition: str, out: Path, props: Path, frame: int) -> list[str]:
        return [
            *self._base("still", composition, out, props),
            f"--frame={frame}",
            "--image-format=png",
        ]

    def run(self, args: list[str]) -> float:
        if not self.cli_js.is_file():
            raise RuntimeError(
                f"Remotion is not installed in {self.project}. Run: hypecut overlays setup "
                "(or `npm ci` inside overlays/)."
            )
        if self.platform == "win32-arm64" and not self.browser:
            raise RuntimeError(
                "Windows on ARM needs an installed Chrome or Edge for Remotion. "
                "Pass --browser PATH or set HYPECUT_REMOTION_BROWSER."
            )
        self.log.append(" ".join(args))
        t0 = time.monotonic()
        proc = self.runner(args, cwd=str(self.project), capture_output=True, text=True)
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-15:]
            raise RuntimeError("Remotion render failed:\n" + "\n".join(tail))
        return time.monotonic() - t0


@dataclass
class RenderedAssets:
    out_dir: Path
    intro: Path | None = None
    outro: Path | None = None
    lower_third: Path | None = None
    titles: dict[str, Path] = field(default_factory=dict)  # text -> file
    score: Path | None = None
    stills: list[Path] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "title"


def render_assets(
    cfg: OverlayConfig,
    out_dir: str | Path,
    runner: RemotionRunner,
    *,
    only: Iterable[str] | None = None,
    scale: float = 1.0,
    stills: bool = False,
    progress: Progress | None = None,
) -> RenderedAssets:
    """Render every card the config asks for into ``out_dir``."""
    cfg.validate()
    progress = progress or _noop
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    want = set(only) if only else None
    suffix = "-Vertical" if cfg.vertical else ""
    assets = RenderedAssets(out_dir=out)

    jobs: list[tuple[str, str, str, dict[str, Any], bool]] = []  # key, comp, file, props, alpha
    if cfg.intro:
        jobs.append(("intro", "Intro", "intro.mp4", cfg.props(), False))
    if cfg.outro:
        jobs.append(("outro", "Outro", "outro.mp4", cfg.props(), False))
    if cfg.lower_third:
        jobs.append(("lower-third", "LowerThird", "lower_third.mov", cfg.props(), True))
    for i, text in enumerate(dict.fromkeys(cfg.titles.values()), start=1):
        jobs.append(
            (
                "title",
                "Title",
                f"title_{i:02d}_{_slug(text)}.mov",
                cfg.props(title=text, subtitle=""),
                True,
            )
        )
    if cfg.score is not None:
        jobs.append(("score", "ScoreBar", "score_bar.mov", cfg.props(), True))
    if want is not None:
        unknown = want - {"intro", "outro", "lower-third", "title", "score"}
        if unknown:
            raise ValueError(f"--only: unknown part(s) {sorted(unknown)}")
        jobs = [j for j in jobs if j[0] in want]
    if not jobs:
        raise ValueError("Nothing to render: turn on intro, outro, lower-third, titles or score.")

    still_frame = {"intro": 100, "outro": 60, "lower-third": 60, "title": 40, "score": 29}
    for n, (key, comp, fname, props, alpha) in enumerate(jobs):
        props_path = out / f".{Path(fname).stem}.props.json"
        props_path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
        dest = out / fname
        secs = runner.run(
            runner.render_args(comp + suffix, dest, props_path, transparent=alpha, scale=scale)
        )
        assets.timings[fname] = round(secs, 2)
        if key == "intro":
            assets.intro = dest
        elif key == "outro":
            assets.outro = dest
        elif key == "lower-third":
            assets.lower_third = dest
        elif key == "title":
            assets.titles[props["title"]] = dest
        else:
            assets.score = dest
        if stills:
            png = out / f"{Path(fname).stem}.png"
            runner.run(runner.still_args(comp + suffix, png, props_path, still_frame[key]))
            assets.stills.append(png)
        progress((n + 1) / len(jobs), f"rendered {fname}")
    return assets


# ------------------------------------------------------------------ compose


@dataclass
class ClipSlot:
    """Where one clip sits inside the joined reel."""

    id: str
    start: float  # seconds from the start of the reel
    duration: float
    #: seconds into the clip of the first parent tap (None for auto clips)
    tap: float | None = None


def slots_from_plan(plan: Any, clip_files: Sequence[Path] | None = None) -> list[ClipSlot]:
    """Clip positions in the reel, in order. Uses the real clip lengths when given.

    Stream-copied clips can start up to a couple of seconds early (at the
    previous keyframe); the extra lead-in shifts the tap later by the same
    amount, so we measure the real files when we have them.
    """
    kept = list(plan.kept)
    if clip_files is not None and len(clip_files) != len(kept):
        raise ValueError("clip files and kept clips do not line up")
    slots: list[ClipSlot] = []
    t = 0.0
    for i, clip in enumerate(kept):
        planned = float(clip.duration)
        actual = planned
        if clip_files is not None and Path(clip_files[i]).exists():
            actual = ff.probe(clip_files[i]).duration or planned
        lead = max(0.0, actual - planned)
        tap = None
        if clip.marks:
            tap = lead + (float(clip.marks[0]) - float(clip.start))
        slots.append(ClipSlot(id=clip.id, start=t, duration=actual, tap=tap))
        t += actual
    return slots


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


def build_compose_command(
    reel: Path,
    out: Path,
    cfg: OverlayConfig,
    assets: RenderedAssets,
    slots: Sequence[ClipSlot],
    *,
    width: int,
    height: int,
    fps: float,
    duration: float,
    has_audio: bool,
) -> tuple[list[str], list[str]]:
    """One ffmpeg command: overlays on the reel, then intro + reel + outro.

    Returns (argv, notes). Pure: no files are touched, so it is easy to test.
    """
    notes: list[str] = []
    by_id = {s.id: s for s in slots}
    inputs: list[str] = ["-i", str(reel)]
    filters: list[str] = []
    n_in = 1
    base = "[0:v]"
    W, H = _even(width), _even(height)
    fps_s = f"{fps:.3f}".rstrip("0").rstrip(".") if fps > 0 else "30"
    filters.append(f"{base}scale={W}:{H},setsar=1,format=yuv420p[m0]")
    cur = "[m0]"
    step = 0

    def add_overlay(path: Path, at: float, length: float | None, hold: bool = False) -> None:
        nonlocal n_in, cur, step
        inputs.extend(["-i", str(path)])
        idx = n_in
        n_in += 1
        step += 1
        ov = f"[ov{step}]"
        filters.append(
            f"[{idx}:v]scale={W}:{H}:flags=lanczos,format=yuva420p,"
            f"setpts=PTS-STARTPTS+{at:.3f}/TB{ov}"
        )
        nxt = f"[m{step}]"
        eof = "repeat" if hold else "pass"
        enable = "" if length is None else f":enable='between(t,{at:.3f},{at + length:.3f})'"
        filters.append(f"{cur}{ov}overlay=0:0:eof_action={eof}:format=auto{enable}{nxt}")
        cur = nxt

    if assets.score is not None:
        add_overlay(assets.score, 0.0, None, hold=True)

    if cfg.lower_third and assets.lower_third is not None:
        targets = [s.id for s in slots] if "all" in cfg.lower_third else cfg.lower_third
        for cid in targets:
            slot = by_id.get(cid)
            if slot is None:
                raise ValueError(f"lower-third: {cid} is not a kept clip in this reel")
            at = slot.start + min(0.6, max(0.0, slot.duration - 1.0))
            length = min(SECONDS["lower_third"], max(1.0, slot.start + slot.duration - at))
            add_overlay(assets.lower_third, at, length)

    for cid, text in cfg.titles.items():
        slot = by_id.get(cid)
        if slot is None:
            raise ValueError(f"title: {cid} is not a kept clip in this reel")
        path = assets.titles.get(text)
        if path is None:
            raise ValueError(f"title {text!r} was not rendered")
        if cid in cfg.title_at:
            into = float(cfg.title_at[cid])
        elif slot.tap is not None:
            into = slot.tap - 0.5
        else:
            into = slot.duration / 2 - SECONDS["title"] / 2
            notes.append(f"{cid} has no tap; its title is centred in the clip.")
        into = min(max(0.0, into), max(0.0, slot.duration - SECONDS["title"]))
        add_overlay(path, slot.start + into, SECONDS["title"])

    segments: list[tuple[str, str | None]] = []
    main_a: str | None = None
    if has_audio:
        filters.append(
            "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a_main]"
        )
        main_a = "[a_main]"

    def card(path: Path, key: str) -> None:
        nonlocal n_in
        inputs.extend(["-i", str(path)])
        idx = n_in
        n_in += 1
        v = f"[{key}v]"
        filters.append(
            f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps_s},format=yuv420p{v}"
        )
        a = None
        if has_audio:
            a = f"[{key}a]"
            secs = SECONDS[key]
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={secs:.3f},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo{a}"
            )
        segments.append((v, a))

    if assets.intro is not None and cfg.intro:
        card(assets.intro, "intro")
    filters.append(f"{cur}fps={fps_s}[main]")
    segments.append(("[main]", main_a))
    if assets.outro is not None and cfg.outro:
        card(assets.outro, "outro")

    maps: list[str]
    if len(segments) == 1:
        maps = ["-map", "[main]"] + (["-map", main_a] if main_a else [])
    else:
        chain = "".join(v + (a or "") for v, a in segments)
        a_n = 1 if has_audio else 0
        filters.append(
            f"{chain}concat=n={len(segments)}:v=1:a={a_n}[outv]" + ("[outa]" if a_n else "")
        )
        maps = ["-map", "[outv]"] + (["-map", "[outa]"] if a_n else [])

    argv = ["ffmpeg", "-v", "error", "-nostdin", "-y", *inputs]
    argv += ["-filter_complex", ";".join(filters), *maps]
    argv += ff.cmd("-c:v libx264 -preset veryfast -crf {crf} -pix_fmt yuv420p", crf=str(cfg.crf))
    if has_audio:
        argv += ff.cmd("-c:a aac -b:a 192k -ar 48000 -ac 2")
    argv += ["-movflags", "+faststart", str(out)]
    return argv, notes


def compose_reel(
    reel: str | Path,
    cfg: OverlayConfig,
    assets: RenderedAssets,
    slots: Sequence[ClipSlot],
    out: str | Path | None = None,
) -> tuple[Path, list[str]]:
    """Write ``<reel>_overlays.mp4`` (never overwrites the reel itself)."""
    reel = Path(reel)
    dest = Path(out) if out else reel.with_name(f"{reel.stem}_overlays.mp4")
    if dest.resolve() == reel.resolve():
        raise ValueError("refusing to overwrite the source reel; pick another output name")
    info = ff.probe(reel)
    notes: list[str] = []
    if (info.height > info.width) != cfg.vertical:
        notes.append(
            "The reel's shape does not match the card format (16:9 vs 9:16); cards are fitted."
        )
    if dest.exists():
        notes.append(f"Replaced the existing {dest.name}.")
    tmp = dest.with_name(f".{dest.stem}.part.mp4")
    argv, more = build_compose_command(
        reel,
        tmp,
        cfg,
        assets,
        slots,
        width=info.width,
        height=info.height,
        fps=info.fps,
        duration=info.duration,
        has_audio=info.has_audio,
    )
    try:
        ff.run(argv)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest, notes + more


def card_scale(width: int, height: int) -> float:
    """Render cards at the reel's native size (e.g. 2 for a 4K reel)."""
    short = min(width, height) or 1080
    return max(1.0, round(short / 1080, 3))


# ------------------------------------------------------------------- setup


def setup_project(project: Path, *, node_arch: str, npm: str | None = None) -> list[str]:
    """`npm ci` in overlays/ and, on Windows ARM64, fetch the x64 compositor.

    Nothing is installed globally. Returns the commands it ran.
    """
    npm = npm or shutil.which("npm") or shutil.which("npm.cmd") or "npm"
    ran: list[str] = []

    def call(args: list[str], cwd: Path) -> None:
        ran.append(" ".join(args))
        # npm is npm.cmd on Windows; CreateProcess runs .cmd files directly and
        # these arguments are fixed strings, not user input.
        subprocess.run(args, cwd=str(cwd), check=True)

    lock = project / "package-lock.json"
    call([npm, "ci" if lock.exists() else "install", "--no-audit", "--no-fund"], project)
    if node_arch == "win32-arm64":
        version = json.loads(
            (project / "node_modules" / "remotion" / "package.json").read_text(encoding="utf-8")
        )["version"]
        dest = project / ".remotion-bin" / "win32-x64"
        with tempfile.TemporaryDirectory() as tmp:
            call(
                [
                    npm,
                    "pack",
                    f"@remotion/compositor-win32-x64-msvc@{version}",
                    "--pack-destination",
                    tmp,
                ],
                project,
            )
            tgz = next(Path(tmp).glob("*.tgz"))
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            with tarfile.open(tgz) as tf:
                for member in tf.getmembers():
                    name = Path(member.name)
                    if not member.isfile() or name.parts[0] != "package" or ".." in name.parts:
                        continue
                    member.name = str(Path(*name.parts[1:]))
                    tf.extract(member, dest)
        ran.append(f"extracted compositor-win32-x64-msvc@{version} -> {dest}")
    return ran


# --------------------------------------------------------------------- CLI


def add_overlays_parser(sub) -> None:
    ov = sub.add_parser(
        "overlays",
        help="Remotion intro/outro, lower-thirds, title cards and score bar",
        description="Render overlay cards with Remotion and compose them with a reel.",
    )
    osub = ov.add_subparsers(dest="overlays_command", required=True)

    def common(p) -> None:
        p.add_argument("--config", required=True, help="overlays.json (see docs/overlays.md)")
        p.add_argument("--overlays-dir", help="the Remotion project (default: repo overlays/)")
        p.add_argument("--browser", help="Chrome/Edge to render with (needed on Windows ARM64)")
        p.add_argument("-q", "--quiet", action="store_true")

    r = osub.add_parser("render", help="render the cards in a config to a folder")
    common(r)
    r.add_argument("-o", "--output-dir", default="overlay-cards")
    r.add_argument("--only", help="comma list: intro,outro,lower-third,title,score")
    r.add_argument("--stills", action="store_true", help="also save a PNG preview of each card")
    r.add_argument("--scale", type=float, default=1.0, help="2 = render cards for a 4K reel")

    c = osub.add_parser("compose", help="put the cards on a finished reel (one ffmpeg pass)")
    common(c)
    c.add_argument("reel", help="the joined reel, e.g. highlights.mp4")
    c.add_argument("--plan", help="falcon_plan.json (needed for lower-thirds and titles)")
    c.add_argument("--clips-dir", help="the clips/ folder, to measure real clip lengths")
    c.add_argument("-o", "--output", help="default: <reel>_overlays.mp4")

    s = osub.add_parser("setup", help="npm ci in overlays/ (+ Windows ARM64 compositor)")
    s.add_argument("--overlays-dir")
    s.add_argument("--browser")
    s.add_argument("-q", "--quiet", action="store_true")


def add_falcon_overlay_args(parser) -> None:
    g = parser.add_argument_group(
        "overlays (optional, off by default; writes highlights_overlays.mp4 via Remotion)"
    )
    g.add_argument("--overlays-config", metavar="JSON", help="overlays.json; flags below override")
    g.add_argument("--intro", action="store_true", help="prepend an intro card")
    g.add_argument("--outro", action="store_true", help="append an outro card")
    g.add_argument("--player", help="player name for the cards")
    g.add_argument("--number", help="jersey number")
    g.add_argument("--team", help="team name")
    g.add_argument("--opponent", help="opponent (optional)")
    g.add_argument("--game-date", help="shown on the intro, e.g. 'Apr 25, 2026'")
    g.add_argument("--accent", help="team colour as hex, e.g. #FFC41F")
    g.add_argument(
        "--lower-third",
        metavar="CLIPS",
        help="player name strip on these clips: clip03,clip07 or 'all'",
    )
    g.add_argument(
        "--title",
        action="append",
        metavar="CLIP=TEXT",
        help="title card you assign, e.g. clip05=Goal! (repeatable; add @SEC to time it)",
    )
    g.add_argument("--score", metavar="H-A", help="score bar for the whole reel, e.g. 2-1")
    g.add_argument("--score-labels", metavar="HOME-AWAY", help="e.g. TFC-OPP")
    g.add_argument("--overlays-dir", help=argparse.SUPPRESS)
    g.add_argument("--browser", help="Chrome/Edge for Remotion (Windows ARM64)")


_FALCON_FLAGS = (
    "overlays_config",
    "intro",
    "outro",
    "player",
    "number",
    "team",
    "opponent",
    "game_date",
    "accent",
    "lower_third",
    "title",
    "score",
)


def config_from_falcon_args(args: Any) -> OverlayConfig | None:
    """None unless at least one overlay flag was given."""
    if not any(getattr(args, k, None) for k in _FALCON_FLAGS):
        return None
    data: dict[str, Any] = {}
    if getattr(args, "overlays_config", None):
        data = json.loads(Path(args.overlays_config).read_text(encoding="utf-8-sig"))
    for flag, key in (
        ("player", "player"),
        ("number", "number"),
        ("team", "team"),
        ("opponent", "opponent"),
        ("game_date", "date"),
        ("accent", "accent"),
    ):
        if getattr(args, flag, None):
            data[key] = getattr(args, flag)
    if getattr(args, "intro", False):
        data["intro"] = True
    if getattr(args, "outro", False):
        data["outro"] = True
    if getattr(args, "lower_third", None):
        data["lowerThird"] = args.lower_third
    if getattr(args, "title", None):
        titles = dict(data.get("titles") or {})
        for spec in args.title:
            cid, text, at = parse_title_spec(spec)
            titles[cid] = text if at is None else f"{text}@{at:g}"
        data["titles"] = titles
    if getattr(args, "score", None):
        h, a = parse_score(args.score)
        score: dict[str, Any] = {"home": h, "away": a}
        if getattr(args, "score_labels", None):
            parts = args.score_labels.split("-", 1)
            score["homeLabel"] = parts[0]
            score["awayLabel"] = parts[1] if len(parts) > 1 else ""
        data["score"] = score
    cfg = OverlayConfig.from_dict(data)
    if not cfg.active:
        raise ValueError(
            "--player/--team/--number only fill in the cards; also pick --intro, --outro, "
            "--lower-third, --title or --score"
        )
    return cfg


def check_against_plan(cfg: OverlayConfig, plan: Any) -> None:
    """Fail before any cutting if a title or lower-third names a dropped/unknown clip."""
    kept = {c.id for c in plan.kept}
    every = {c.id for c in plan.clips}
    for cid in [*cfg.titles, *[c for c in cfg.lower_third if c != "all"]]:
        if cid not in every:
            raise ValueError(f"{cid} is not in this plan (clips: {', '.join(sorted(every))})")
        if cid not in kept:
            raise ValueError(f"{cid} is dropped from the reel; keep it or remove its overlay")


def apply_to_falcon_reel(
    cfg: OverlayConfig,
    plan: Any,
    reel: Path,
    clip_files: Sequence[Path],
    *,
    browser: str | None = None,
    overlays_dir: str | None = None,
    runner: RemotionRunner | None = None,
    progress: Progress | None = None,
) -> tuple[Path, list[str]]:
    progress = progress or _noop
    runner = runner or RemotionRunner.detect(overlays_dir, browser=browser)
    info = ff.probe(reel)
    cfg.vertical = info.height > info.width
    assets = render_assets(
        cfg,
        reel.parent / "overlays",
        runner,
        scale=card_scale(info.width, info.height),
        progress=lambda p, m: progress(0.6 * p, m),
    )
    slots = slots_from_plan(plan, clip_files)
    progress(0.7, "composing overlays")
    return compose_reel(reel, cfg, assets, slots)


def run_overlays_cli(args: Any) -> int:
    cmd = args.overlays_command
    if cmd == "setup":
        project = find_project(args.overlays_dir)
        node = shutil.which("node") or "node"
        arch = _node_arch(node)
        for line in setup_project(project, node_arch=arch):
            print(f"ran: {line}")
        r = RemotionRunner.detect(project, browser=args.browser)
        print(f"node: {arch or 'not found'}")
        print(
            f"browser: {r.browser or 'Remotion Chrome Headless Shell (downloaded on first render)'}"
        )
        if r.binaries_dir:
            print(f"compositor: {r.binaries_dir}")
        return 0

    cfg = OverlayConfig.load(args.config)
    runner = RemotionRunner.detect(args.overlays_dir, browser=args.browser)
    report: Progress = (
        _noop if args.quiet else lambda p, m: print(f"[{p:4.0%}] {m}", file=sys.stderr)
    )

    if cmd == "render":
        only = [s.strip() for s in args.only.split(",")] if args.only else None
        assets = render_assets(
            cfg,
            args.output_dir,
            runner,
            only=only,
            scale=args.scale,
            stills=args.stills,
            progress=report,
        )
        if not args.quiet:
            for name, secs in assets.timings.items():
                print(f"{assets.out_dir / name}  ({secs:.1f} s)")
            for png in assets.stills:
                print(f"preview: {png}")
            if runner.browser:
                print(f"browser: {runner.browser}")
        return 0

    # compose
    from .falcon import load_plan

    reel = Path(args.reel)
    plan = load_plan(args.plan) if args.plan else None
    if (cfg.titles or cfg.lower_third) and plan is None:
        raise ValueError("titles and lower-thirds need --plan falcon_plan.json to find the clips")
    slots: list[ClipSlot] = []
    if plan is not None:
        check_against_plan(cfg, plan)
        files = None
        if args.clips_dir:
            files = [Path(args.clips_dir) / c.filename for c in plan.kept]
            if not all(f.exists() for f in files):
                files = None
        slots = slots_from_plan(plan, files)
    info = ff.probe(reel)
    cfg.vertical = info.height > info.width
    assets = render_assets(
        cfg,
        reel.parent / "overlays",
        runner,
        scale=card_scale(info.width, info.height),
        progress=report,
    )
    dest, notes = compose_reel(reel, cfg, assets, slots, args.output)
    if not args.quiet:
        for n in notes:
            print(f"note: {n}")
        print(f"Reel with overlays: {dest}")
    return 0
