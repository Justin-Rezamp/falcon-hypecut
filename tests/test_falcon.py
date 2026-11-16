"""End-to-end Falcon runs on synthetic auto-tracking footage.

The fixture is what an XbotGo Falcon recording looks like to the pipeline:
one continuous shot that pans back and forth across a textured field the
whole time (``crop`` sliding over ``testsrc2``), quiet ambient audio, and one
burst of real action — a player-sized box moving against the pan plus a
cheer — at 74-77 s. The ``.mark`` file next to it has taps at 0:12, 0:40,
0:51 and 1:20, and one stray tap after the end of the video.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hypecut.cli import main
from hypecut.falcon import FalconOptions, load_plan, plan_falcon, probe_source, render_falcon
from tests.conftest import HAS_FFMPEG, requires_ffmpeg

CLICKS = [12.0, 40.0, 51.0, 80.0]
ACTION = (74.0, 77.0)
FALCON_SAMPLE = [{"type": "start", "time": 0}, {"type": "click", "time": 549.0}]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _make_game(dest: Path, *, seconds: int = 90, codec: list[str] | None = None, size="320x180"):
    w, h = (int(v) for v in size.split("x"))
    lo, hi = ACTION
    vf = (
        f"[0:v]crop={w}:{h}:x='abs(mod(t*{w // 10},{2 * w})-{w})':y=0,"
        f"drawbox=x='{w // 6}+mod(t*{w},{w // 2})':y={h // 4}:w={w // 8}:h={h // 3}:"
        f"color=white:t=fill:enable='between(t,{lo},{hi})',format=yuv420p[v];"
        f"[2:a]volume=enable='not(between(t,{lo},{hi}))':volume=0[burst];"
        "[1:a][burst]amix=inputs=2:normalize=0[a]"
    )
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=s={2 * w}x{h}:r=15:d={seconds}",
        "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:a=0.02:r=48000:seed=7",
        "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=white:a=0.6:r=48000:seed=11",
        "-filter_complex", vf, "-map", "[v]", "-map", "[a]",
        *(codec or ["-c:v", "libx264", "-preset", "ultrafast", "-g", "30"]),
        "-c:a", "aac", "-b:a", "64k", str(dest),
    )  # fmt: skip
    return dest


def _write_marks(video: Path, clicks: list[float], extra_after_end: bool = True) -> Path:
    data = [{"type": "start", "time": 0}] + [{"type": "click", "time": t} for t in clicks]
    if extra_after_end:
        data.append({"type": "click", "time": 5000.0})
    mark = video.with_suffix(".mark")
    mark.write_text(json.dumps(data), encoding="utf-8")
    return mark


@pytest.fixture(scope="module")
def falcon_game(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("falcon")
    video = _make_game(d / "game.mp4")
    _write_marks(video, CLICKS)
    return video


def _duration(path: Path) -> float:
    return probe_source(path).duration


EXPECTED = ["clip01_mark_0-12.mp4", "clip02_mark_0-40_merged.mp4", "clip03_mark_1-20.mp4"]


@requires_ffmpeg
def test_marks_mode_end_to_end(falcon_game, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["falcon", str(falcon_game), "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "after the video ends" in printed  # the stray tap is reported, not cut

    reel = out / "highlights.mp4"
    assert reel.exists()
    assert sorted(p.name for p in (out / "clips").iterdir()) == EXPECTED

    plan = json.loads((out / "falcon_plan.json").read_text(encoding="utf-8"))
    assert plan["mode"] == "marks"
    assert plan["clicks"] == CLICKS
    windows = [(c["start"], c["end"]) for c in plan["clips"]]
    assert windows == [(2.0, 22.0), (30.0, 61.0), (70.0, 90.0)]

    # Stream copy lands on keyframes (every 2 s here), so allow that slack.
    planned = sum(e - s for s, e in windows)
    assert planned - 1 <= _duration(reel) <= planned + 3 * 2.5
    assert probe_source(reel).video_codec == "h264"

    # Running again says it replaced the reel rather than doing so silently.
    assert main(["falcon", str(falcon_game), "-o", str(out)]) == 0
    assert "Replaced the existing highlights.mp4" in capsys.readouterr().out


@requires_ffmpeg
def test_plan_only_then_render_an_edited_plan(falcon_game, tmp_path):
    out = tmp_path / "out"
    assert main(["falcon", str(falcon_game), "-o", str(out), "--plan-only", "-q"]) == 0
    plan_path = out / "falcon_plan.json"
    assert plan_path.exists()
    assert not (out / "highlights.mp4").exists()
    assert not (out / "clips").exists()

    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["clips"][1]["keep"] = False  # drop clip02
    data["clips"][0]["start"] = 5.0  # recut clip01
    plan_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["falcon", "--from-plan", str(plan_path), "-o", str(out), "-q"]) == 0
    names = sorted(p.name for p in (out / "clips").iterdir())
    assert names == ["clip01_mark_0-12.mp4", "clip03_mark_1-20.mp4"]
    assert load_plan(out / "falcon_plan.json").clips[0].start == 5.0


@requires_ffmpeg
def test_hybrid_slides_toward_the_action_but_keeps_the_tap(falcon_game):
    plan = plan_falcon(falcon_game, options=FalconOptions(mode="hybrid"))
    assert [c.filename for c in plan.clips] == EXPECTED  # names still follow the taps
    last = plan.clips[-1]
    assert last.marks == [80.0]
    shift = last.meta["shift"][0]
    # The burst peaks ~3-6 s before the tap; the window slides that way...
    assert -6.0 <= shift <= -2.0, plan.notes
    assert last.start < 70.0
    # ...but never so far that the tap itself falls out of the clip.
    assert last.start < 80.0 < last.end
    assert any("1:20" in n and "inference" in n for n in plan.notes)
    assert all(c.kind == "mark" for c in plan.clips)


@requires_ffmpeg
def test_detect_mode_names_clips_auto(falcon_game, tmp_path):
    video = tmp_path / "nomarks.mp4"
    shutil.copy(falcon_game, video)
    plan = plan_falcon(video)  # no .mark -> detect
    assert plan.mode == "detect"
    assert plan.clicks == []
    assert plan.clips
    assert all(c.kind == "auto" and "_auto_" in c.filename for c in plan.clips)
    assert any(c.start <= ACTION[0] + 1 and c.end >= ACTION[1] - 1 for c in plan.clips), [
        (c.start, c.end) for c in plan.clips
    ]


@requires_ffmpeg
def test_encode_mode_with_phone_and_vertical_exports(falcon_game, tmp_path):
    opts = FalconOptions(cut="encode", phone=True, vertical=True, loudnorm=True)
    plan = plan_falcon(falcon_game, options=opts)
    plan.clips = plan.clips[:2]
    out = render_falcon(plan, tmp_path / "out", opts)
    assert out.cut_mode == "encode"
    assert [p.name for p in out.clips] == EXPECTED[:2]
    clip = probe_source(out.clips[0])
    assert clip.video_codec == "h264" and clip.duration == pytest.approx(20.0, abs=0.3)
    phone = probe_source(out.phone)
    assert phone.video_codec == "h264" and phone.height <= 1080
    vertical = probe_source(out.vertical)
    assert (vertical.width, vertical.height) == (1080, 1920)


def _has_encoder(name: str) -> bool:
    if not HAS_FFMPEG:
        return False
    listing = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    )
    return f" {name} " in listing.stdout


@requires_ffmpeg
@pytest.mark.skipif(not _has_encoder("libx265"), reason="ffmpeg built without libx265")
def test_hevc_source_is_analysed_on_a_proxy_and_reencoded(tmp_path):
    video = _make_game(
        tmp_path / "hevc.mp4",
        seconds=30,
        size="640x360",
        codec=["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error"],
    )
    _write_marks(video, [12.0], extra_after_end=False)
    opts = FalconOptions(mode="hybrid", cache_dir=str(tmp_path / "proxies"))
    plan = plan_falcon(video, options=opts)
    assert plan.analysis_source and plan.analysis_source != plan.source
    assert any("proxy" in n for n in plan.notes)
    out = render_falcon(plan, tmp_path / "out", opts)
    assert out.cut_mode == "encode"
    assert probe_source(out.highlights).video_codec == "h264"
    assert probe_source(plan.analysis_source).height == 360


@requires_ffmpeg
def test_mode_needs_marks(tmp_path):
    video = tmp_path / "g.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=3", "-c:v", "libx264", str(video))
    with pytest.raises(FileNotFoundError, match="needs a .mark file"):
        plan_falcon(video, options=FalconOptions(mode="marks"))


def test_options_are_validated():
    with pytest.raises(ValueError):
        FalconOptions(mode="magic").validate()
    with pytest.raises(ValueError):
        FalconOptions(cut="lossless").validate()
    with pytest.raises(ValueError):
        FalconOptions(before=0, after=0).validate()


def test_cli_marks_flag_enables_the_signal(tmp_path):
    from hypecut.cli import _config_from_args, build_parser

    mark = tmp_path / "g.mark"
    mark.write_text(json.dumps(FALCON_SAMPLE), encoding="utf-8")
    args = build_parser().parse_args(["analyze", "g.mp4", "--marks", str(mark)])
    cfg = _config_from_args(args)
    assert "falcon_marks" in cfg.signals.enabled
    assert cfg.signals.params["falcon_marks"]["file"] == str(mark)
    # Bare --marks means "find the sibling".
    args = build_parser().parse_args(["analyze", "g.mp4", "--marks"])
    assert _config_from_args(args).signals.params["falcon_marks"]["file"] == ""


@requires_ffmpeg
def test_generic_pipeline_fuses_marks(falcon_game):
    """`hypecut analyze --marks` fuses taps in as a strong signal (not a guarantee)."""
    import numpy as np

    from hypecut.cli import _config_from_args, build_parser
    from hypecut.pipeline import analyze

    def run(*extra: str):
        argv = ["analyze", str(falcon_game), "-p", "configs/falcon-soccer.yaml", *extra]
        return analyze(falcon_game, _config_from_args(build_parser().parse_args(argv)))

    with_marks, without = run("--marks"), run()
    assert "falcon_marks" in [t.name for t in with_marks.tracks]

    def covered(plan) -> int:
        return sum(any(s.start <= t <= s.end for s in plan.segments) for t in CLICKS)

    def lift(plan) -> float:
        """Mean curve in the 8 s before each tap, relative to the whole game."""
        near = np.zeros_like(plan.times, dtype=bool)
        for t in CLICKS:
            near |= (plan.times >= t - 8) & (plan.times <= t)
        return float(plan.curve[near].mean() / max(plan.curve.mean(), 1e-9))

    assert covered(with_marks) >= covered(without)
    assert covered(with_marks) >= 3
    assert lift(with_marks) > lift(without) * 1.2
