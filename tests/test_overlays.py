"""Remotion overlay cards: config, command building and the ffmpeg compose pass.

Remotion itself is mocked: a fake runner writes stand-in card files with
ffmpeg (an opaque MP4 for intro/outro, ProRes 4444 with alpha for the rest),
so these tests need Node only in the sense that they never call it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hypecut import overlays as ov
from hypecut.cli import build_parser, main
from hypecut.marks import FalconClip
from tests.conftest import HAS_FFMPEG, requires_ffmpeg

# ------------------------------------------------------------------ helpers


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _fake_card(dest: Path, seconds: float, alpha: bool, size="320x180") -> None:
    if alpha:
        _ffmpeg(
            "-f", "lavfi", "-i", f"color=c=red@0.5:s={size}:r=30:d={seconds},format=yuva444p10le",
            "-c:v", "prores_ks", "-profile:v", "4444", str(dest),
        )  # fmt: skip
    else:
        _ffmpeg(
            "-f", "lavfi", "-i", f"color=c=navy:s={size}:r=30:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(dest),
        )  # fmt: skip


class FakeRemotion:
    """Stands in for subprocess.run when the runner calls the Remotion CLI."""

    def __init__(self, make_files: bool = True):
        self.calls: list[list[str]] = []
        self.make_files = make_files

    def __call__(self, args, cwd=None, capture_output=True, text=True):
        self.calls.append(list(args))
        verb, comp, out = args[2], args[4], Path(args[5])
        if self.make_files:
            if verb == "still":
                out.write_bytes(b"\x89PNG fake")
            else:
                base = comp.replace("-Vertical", "")
                secs = {"Intro": 5, "Outro": 4, "LowerThird": 5, "Title": 3, "ScoreBar": 1}[base]
                _fake_card(out, secs, alpha="--codec=prores" in args)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def project(tmp_path) -> Path:
    p = tmp_path / "overlays"
    (p / "src").mkdir(parents=True)
    (p / "package.json").write_text("{}", encoding="utf-8")
    (p / "src" / "index.ts").write_text("", encoding="utf-8")
    cli = p / "node_modules" / "@remotion" / "cli"
    cli.mkdir(parents=True)
    (cli / "remotion-cli.js").write_text("", encoding="utf-8")
    return p


def _runner(project: Path, fake: FakeRemotion, **kw) -> ov.RemotionRunner:
    return ov.RemotionRunner(project=project, platform="linux-x64", runner=fake, **kw)


def _plan(clips: list[FalconClip]):
    return SimpleNamespace(clips=clips, kept=[c for c in clips if c.keep])


CLIPS = [
    FalconClip(id="clip01", kind="mark", start=2.0, end=22.0, marks=[12.0]),
    FalconClip(id="clip02", kind="mark", start=30.0, end=50.0, marks=[40.0]),
    FalconClip(id="clip03", kind="mark", start=60.0, end=80.0, marks=[70.0], keep=False),
    FalconClip(id="clip04", kind="auto", start=90.0, end=100.0),
]

# ------------------------------------------------------------------- config


def test_title_spec_parsing():
    assert ov.parse_title_spec("clip05=Goal!") == ("clip05", "Goal!", None)
    assert ov.parse_title_spec("CLIP12= Great save @4.5") == ("clip12", "Great save", 4.5)
    for bad in ("Goal!", "clipX=Goal", "clip05=", "clip05=" + "x" * 30):
        with pytest.raises(ValueError):
            ov.parse_title_spec(bad)


def test_config_validation():
    cfg = ov.OverlayConfig.from_dict(
        {
            "team": "Test FC",
            "player": "Test Player",
            "number": "#10",
            "intro": True,
            "lowerThird": "clip01, clip02",
            "titles": {"clip02": "Save@3"},
            "score": "2-1",
            "format": "9x16",
        }
    )
    assert cfg.number == "10" and cfg.vertical
    assert cfg.lower_third == ["clip01", "clip02"]
    assert cfg.titles == {"clip02": "Save"} and cfg.title_at == {"clip02": 3.0}
    assert cfg.score == {"home": 2, "away": 1, "homeLabel": "", "awayLabel": ""}
    props = cfg.props(title="Save")
    assert props["playerNumber"] == "10" and props["homeLabel"] == "HOME"

    with pytest.raises(ValueError, match="hex"):
        ov.OverlayConfig.from_dict({"accent": "red", "intro": True})
    with pytest.raises(ValueError, match="needs --player"):
        ov.OverlayConfig.from_dict({"lowerThird": "all"})
    with pytest.raises(ValueError, match="clip ID"):
        ov.OverlayConfig.from_dict({"player": "A", "lowerThird": "first"})
    with pytest.raises(ValueError, match="1-3 digits"):
        ov.OverlayConfig.from_dict({"number": "ten", "intro": True})


def test_falcon_flags_are_off_by_default():
    args = build_parser().parse_args(["falcon", "game.mp4"])
    assert ov.config_from_falcon_args(args) is None


def test_player_alone_asks_for_a_card():
    args = build_parser().parse_args(["falcon", "game.mp4", "--player", "Test Player"])
    with pytest.raises(ValueError, match="also pick"):
        ov.config_from_falcon_args(args)


def test_falcon_flags_build_a_config():
    args = build_parser().parse_args(
        [
            "falcon", "game.mp4", "--intro", "--outro", "--player", "Test Player",
            "--number", "10", "--team", "Test FC", "--lower-third", "clip01",
            "--title", "clip02=Goal!", "--title", "clip04=Assist@2", "--score", "3-2",
            "--score-labels", "TFC-OPP",
        ]
    )  # fmt: skip
    cfg = ov.config_from_falcon_args(args)
    assert cfg is not None and cfg.intro and cfg.outro
    assert cfg.titles == {"clip02": "Goal!", "clip04": "Assist"}
    assert cfg.title_at == {"clip04": 2.0}
    assert cfg.score["homeLabel"] == "TFC"


def test_titles_never_come_from_marks():
    """A plan full of taps adds no title on its own: labels are the user's call."""
    args = build_parser().parse_args(["falcon", "game.mp4", "--intro", "--player", "A"])
    cfg = ov.config_from_falcon_args(args)
    assert cfg is not None and cfg.titles == {}
    ov.check_against_plan(cfg, _plan(CLIPS))
    assert cfg.titles == {}


def test_overlays_on_dropped_or_unknown_clips_fail_early():
    cfg = ov.OverlayConfig(titles={"clip03": "Goal!"})
    with pytest.raises(ValueError, match="dropped"):
        ov.check_against_plan(cfg, _plan(CLIPS))
    cfg = ov.OverlayConfig(player="A", lower_third=["clip09"])
    with pytest.raises(ValueError, match="not in this plan"):
        ov.check_against_plan(cfg, _plan(CLIPS))


# ----------------------------------------------------------------- remotion


def test_render_args(project, tmp_path):
    r = _runner(project, FakeRemotion(), browser=r"C:\Chrome\chrome.exe", binaries_dir="bin")
    args = r.render_args("LowerThird", tmp_path / "lt.mov", tmp_path / "p.json", transparent=True)
    assert args[2:5] == ["render", "src/index.ts", "LowerThird"]
    assert "--codec=prores" in args and "--prores-profile=4444" in args
    assert "--pixel-format=yuva444p10le" in args
    assert r"--browser-executable=C:\Chrome\chrome.exe" in args
    assert "--binaries-directory=bin" in args
    opaque = r.render_args(
        "Intro", tmp_path / "i.mp4", tmp_path / "p.json", transparent=False, scale=2
    )
    assert "--codec=h264" in opaque and "--scale=2" in opaque


def test_detect_on_windows_arm(project, monkeypatch, tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    bin_dir = project / ".remotion-bin" / "win32-x64"
    bin_dir.mkdir(parents=True)
    (bin_dir / "remotion.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(ov, "_node_arch", lambda node: "win32-arm64")
    monkeypatch.setattr(ov, "_BROWSER_PATHS", (str(tmp_path / "missing.exe"), str(chrome)))
    monkeypatch.delenv("HYPECUT_REMOTION_BROWSER", raising=False)
    r = ov.RemotionRunner.detect(project)
    assert r.browser == str(chrome)
    assert r.binaries_dir == str(bin_dir)

    monkeypatch.setattr(ov, "_BROWSER_PATHS", ())
    r = ov.RemotionRunner.detect(project)
    with pytest.raises(RuntimeError, match="Chrome or Edge"):
        r.run(["node"])


def test_missing_install_is_explained(tmp_path):
    p = tmp_path / "ov"
    (p / "src").mkdir(parents=True)
    r = ov.RemotionRunner(project=p, runner=FakeRemotion(False))
    with pytest.raises(RuntimeError, match="overlays setup"):
        r.run(["node"])


def test_render_failure_surfaces_stderr(project):
    def boom(args, **kw):
        return subprocess.CompletedProcess(args, 1, "", "Error: composition not found")

    r = ov.RemotionRunner(project=project, runner=boom)
    with pytest.raises(RuntimeError, match="composition not found"):
        r.run(["node", "x"])


def test_render_assets_writes_props(project, tmp_path):
    fake = FakeRemotion(make_files=False)
    cfg = ov.OverlayConfig(
        team="Test FC",
        player="Test Player",
        number="10",
        intro=True,
        lower_third=["all"],
        titles={"clip01": "Goal!", "clip02": "Goal!", "clip04": "Save"},
        vertical=True,
    )
    assets = ov.render_assets(cfg, tmp_path / "cards", _runner(project, fake), stills=True)
    comps = [c[4] for c in fake.calls if c[2] == "render"]
    assert comps == ["Intro-Vertical", "LowerThird-Vertical", "Title-Vertical", "Title-Vertical"]
    assert set(assets.titles) == {"Goal!", "Save"}
    props = json.loads((tmp_path / "cards" / ".title_02_save.props.json").read_text("utf-8"))
    assert props["title"] == "Save" and props["playerName"] == "Test Player"
    assert len(assets.stills) == 4

    fake.calls.clear()
    ov.render_assets(cfg, tmp_path / "cards", _runner(project, fake), only=["intro"])
    assert [c[4] for c in fake.calls] == ["Intro-Vertical"]
    with pytest.raises(ValueError, match="Nothing to render"):
        ov.render_assets(ov.OverlayConfig(), tmp_path / "c2", _runner(project, fake))


# ------------------------------------------------------------------ compose


def test_slots_follow_kept_clips_and_taps():
    slots = ov.slots_from_plan(_plan(CLIPS))
    assert [s.id for s in slots] == ["clip01", "clip02", "clip04"]
    assert [s.start for s in slots] == [0.0, 20.0, 40.0]
    assert slots[0].tap == 10.0 and slots[2].tap is None


def test_compose_command_timing(tmp_path):
    cfg = ov.OverlayConfig(
        player="A",
        intro=True,
        outro=True,
        lower_third=["clip01"],
        titles={"clip02": "Goal!", "clip04": "Save"},
        score={"home": 1, "away": 0, "homeLabel": "", "awayLabel": ""},
    )
    assets = ov.RenderedAssets(
        out_dir=tmp_path,
        intro=tmp_path / "intro.mp4",
        outro=tmp_path / "outro.mp4",
        lower_third=tmp_path / "lt.mov",
        titles={"Goal!": tmp_path / "goal.mov", "Save": tmp_path / "save.mov"},
        score=tmp_path / "score.mov",
    )
    slots = ov.slots_from_plan(_plan(CLIPS))
    argv, notes = ov.build_compose_command(
        tmp_path / "reel.mp4", tmp_path / "out.mp4", cfg, assets, slots,
        width=1920, height=1080, fps=29.97, duration=50.0, has_audio=True,
    )  # fmt: skip
    graph = argv[argv.index("-filter_complex") + 1]
    assert "eof_action=repeat" in graph  # score bar holds
    assert "setpts=PTS-STARTPTS+0.600/TB" in graph  # lower-third on clip01
    assert "setpts=PTS-STARTPTS+29.500/TB" in graph  # Goal! 0.5 s before clip02's tap
    assert "between(t,29.500,32.500)" in graph
    assert "setpts=PTS-STARTPTS+43.500/TB" in graph  # Save centred in the tapless clip04
    assert "concat=n=3:v=1:a=1" in graph and "anullsrc" in graph
    assert any("no tap" in n for n in notes)
    assert argv[-1].endswith("out.mp4")

    with pytest.raises(ValueError, match="not a kept clip"):
        ov.build_compose_command(
            tmp_path / "reel.mp4", tmp_path / "o.mp4",
            ov.OverlayConfig(titles={"clip09": "Goal!"}), assets, slots,
            width=1920, height=1080, fps=30, duration=50, has_audio=False,
        )  # fmt: skip


def test_compose_refuses_to_overwrite_the_reel(tmp_path):
    reel = tmp_path / "highlights.mp4"
    reel.write_bytes(b"")
    with pytest.raises(ValueError, match="overwrite"):
        ov.compose_reel(reel, ov.OverlayConfig(intro=True), ov.RenderedAssets(tmp_path), [], reel)


@requires_ffmpeg
def test_compose_reel_end_to_end(project, tmp_path):
    reel = tmp_path / "highlights.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=8",
        "-f", "lavfi", "-i", "sine=frequency=440:r=48000:d=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(reel),
    )  # fmt: skip
    clips = [
        FalconClip(id="clip01", kind="mark", start=0.0, end=4.0, marks=[2.0]),
        FalconClip(id="clip02", kind="mark", start=10.0, end=14.0, marks=[13.0]),
    ]
    cfg = ov.OverlayConfig(
        player="Test Player", number="10", team="Test FC", intro=True, outro=True,
        lower_third=["clip01"], titles={"clip02": "Goal!"},
    )  # fmt: skip
    assets = ov.render_assets(cfg, tmp_path / "cards", _runner(project, FakeRemotion()))
    out, _notes = ov.compose_reel(reel, cfg, assets, ov.slots_from_plan(_plan(clips)))
    assert out.name == "highlights_overlays.mp4"
    info = ov.ff.probe(out)
    assert abs(info.duration - (5 + 8 + 4)) < 0.4
    assert (info.width, info.height) == (320, 180) and info.has_audio
    assert reel.exists()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_falcon_cli_writes_the_overlay_reel(project, tmp_path, monkeypatch):
    from tests.test_falcon import _make_game, _write_marks

    video = _make_game(tmp_path / "game.mp4", seconds=60)
    _write_marks(video, [12.0, 40.0], extra_after_end=False)
    fake = FakeRemotion()
    monkeypatch.setattr(
        ov.RemotionRunner, "detect", classmethod(lambda cls, *a, **k: _runner(project, fake))
    )
    out = tmp_path / "out"
    code = main(
        [
            "falcon", str(video), "-o", str(out), "-q", "--intro", "--player", "Test Player",
            "--number", "10", "--team", "Test FC", "--title", "clip02=Goal!",
        ]
    )  # fmt: skip
    assert code == 0
    assert (out / "highlights.mp4").exists()
    assert (out / "highlights_overlays.mp4").exists()
    assert [c[4] for c in fake.calls] == ["Intro", "Title"]
    gap = ov.ff.probe(out / "highlights_overlays.mp4").duration
    assert gap > ov.ff.probe(out / "highlights.mp4").duration + 4


def test_falcon_cli_rejects_titles_on_unknown_clips(tmp_path, monkeypatch):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    from tests.test_falcon import _make_game, _write_marks

    video = _make_game(tmp_path / "game.mp4", seconds=30)
    _write_marks(video, [12.0], extra_after_end=False)
    code = main(["falcon", str(video), "--plan-only", "-q", "--title", "clip07=Goal!"])
    assert code == 1


def test_paths_are_absolute_because_remotion_runs_in_the_project(project, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    r = _runner(project, FakeRemotion(False))
    args = r.render_args("Intro", Path("cards/intro.mp4"), Path("cards/p.json"), transparent=False)
    assert Path(args[5]).is_absolute() and args[5].endswith("intro.mp4")
    assert args[6] == f"--props={(tmp_path / 'cards' / 'p.json').resolve()}"


def test_config_file_with_bom(tmp_path):
    p = tmp_path / "overlays.json"
    p.write_text('{"intro": true, "team": "Test FC"}', encoding="utf-8-sig")
    assert ov.OverlayConfig.load(p).team == "Test FC"
