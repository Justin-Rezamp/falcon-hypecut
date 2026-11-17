"""Web API: pick a game already in the games folder, review the plan, then build."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import HAS_FFMPEG, requires_ffmpeg  # noqa: E402
from tests.test_falcon import CLICKS, EXPECTED, _make_game  # noqa: E402

MARKS = [{"type": "start", "time": 0}] + [{"type": "click", "time": t} for t in CLICKS]


@pytest.fixture(scope="module")
def game_video(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    return _make_game(tmp_path_factory.mktemp("games-src") / "game.mp4")


def _games_tree(root: Path, video: Path | None = None) -> Path:
    """games/20260425/Your_Video_File.mp4 with its mark in .data under a camera name."""
    day = root / "games" / "20260425"
    (day / ".data").mkdir(parents=True)
    target = day / "Your_Video_File.mp4"
    if video is not None:
        shutil.copy(video, target)
    else:
        target.write_bytes(b"\x00" * 64)
    (day / ".data" / "VID_20260425_133314_01_01.mark").write_text(json.dumps(MARKS))
    (day / "notes.mark").write_text("not json")  # unreadable marks are skipped, not fatal
    return root / "games"


@pytest.fixture()
def app_with_games(tmp_path, monkeypatch):
    def make(video: Path | None = None):
        games = _games_tree(tmp_path, video)
        monkeypatch.setenv("HYPECUT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("HYPECUT_GAMES_DIR", str(games))
        import importlib

        from hypecut.web import app as app_module

        importlib.reload(app_module)
        return app_module, games

    yield make


def _wait(client, job_id, until=("done", "failed")):
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in until:
            return job
        time.sleep(0.2)
    raise AssertionError(job)


def test_lists_games_with_marks_from_the_data_folder(app_with_games):
    app_module, _ = app_with_games()
    with TestClient(app_module.app) as c:
        data = c.get("/api/games").json()
    app_module.store.shutdown(timeout=2)
    assert data["root"] == "games"
    [game] = data["games"]
    assert game["path"] == "20260425/Your_Video_File.mp4"
    assert game["folder"] == "20260425"
    assert [m["path"] for m in game["marks"]] == ["20260425/.data/VID_20260425_133314_01_01.mark"]
    assert game["marks"][0]["taps"] == len(CLICKS)
    assert "/" not in data["root"] and "\\" not in data["root"]  # no absolute server path


def test_missing_games_folder_lists_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPECUT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HYPECUT_GAMES_DIR", str(tmp_path / "nope"))
    import importlib

    from hypecut.web import app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        assert c.get("/api/games").json()["games"] == []
    app_module.store.shutdown(timeout=2)


@pytest.mark.parametrize(
    "game",
    [
        "../secret.mp4",
        "20260425/../../secret.mp4",
        "..\\secret.mp4",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "20260425/missing.mp4",
        "20260425/.data/VID_20260425_133314_01_01.mark",  # right folder, wrong kind
        "",
    ],
)
def test_plan_refuses_paths_outside_the_games_folder(app_with_games, tmp_path, game):
    (tmp_path / "secret.mp4").write_bytes(b"\x00" * 64)
    app_module, _ = app_with_games()
    with TestClient(app_module.app) as c:
        res = c.post("/api/games/plan", json={"game": game, "mark": None, "mode": "detect"})
    app_module.store.shutdown(timeout=2)
    assert res.status_code in (404, 422), res.text
    assert str(tmp_path) not in res.text


def test_plan_refuses_a_mark_outside_the_games_folder(app_with_games, tmp_path):
    (tmp_path / "evil.mark").write_text(json.dumps(MARKS))
    app_module, _ = app_with_games()
    with TestClient(app_module.app) as c:
        res = c.post(
            "/api/games/plan", json={"game": "20260425/Your_Video_File.mp4", "mark": "../evil.mark"}
        )
    app_module.store.shutdown(timeout=2)
    assert res.status_code == 404


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin rights on Windows")
def test_symlink_out_of_the_games_folder_is_not_listed_or_planned(app_with_games, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00" * 64)
    app_module, games = app_with_games()
    (games / "20260425" / "link.mp4").symlink_to(outside)
    with TestClient(app_module.app) as c:
        paths = [g["path"] for g in c.get("/api/games").json()["games"]]
        res = c.post("/api/games/plan", json={"game": "20260425/link.mp4", "mode": "detect"})
    app_module.store.shutdown(timeout=2)
    assert "20260425/link.mp4" not in paths
    assert res.status_code == 404


def test_marks_mode_needs_a_mark(app_with_games):
    app_module, _ = app_with_games()
    with TestClient(app_module.app) as c:
        res = c.post("/api/games/plan", json={"game": "20260425/Your_Video_File.mp4"})
    app_module.store.shutdown(timeout=2)
    assert res.status_code == 422


def test_render_unknown_job_is_404(app_with_games):
    app_module, _ = app_with_games()
    with TestClient(app_module.app) as c:
        assert c.post("/api/jobs/nope/render", json={"keep": ["clip01"]}).status_code == 404
    app_module.store.shutdown(timeout=2)


@requires_ffmpeg
def test_pick_review_build_never_touches_the_source(app_with_games, game_video):
    app_module, games = app_with_games(game_video)
    video = games / "20260425" / "Your_Video_File.mp4"
    mark = games / "20260425" / ".data" / "VID_20260425_133314_01_01.mark"
    before = (video.stat().st_size, mark.read_bytes())
    with TestClient(app_module.app) as c:
        res = c.post(
            "/api/games/plan",
            json={
                "game": "20260425/Your_Video_File.mp4",
                "mark": "20260425/.data/VID_20260425_133314_01_01.mark",
                "mode": "marks",
            },
        )
        assert res.status_code == 202, res.text
        job_id = res.json()["id"]
        job = _wait(c, job_id)
        assert job["status"] == "done" and job["stage"] == "planned"
        assert job["output"] is False and job["clips"] == []  # nothing cut yet
        plan = job["plan"]
        assert "source" not in plan and "mark_file" not in plan
        ids = [cl["id"] for cl in plan["clips"]]
        assert [cl["filename"] for cl in plan["clips"]] == EXPECTED

        thumb = c.get(f"/api/jobs/{job_id}/thumb/{ids[0]}")
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
        assert c.get(f"/api/jobs/{job_id}/thumb/clip99").status_code == 404

        assert c.post(f"/api/jobs/{job_id}/render", json={"keep": []}).status_code == 409
        bad = c.post(f"/api/jobs/{job_id}/render", json={"keep": ["clip77"]})
        assert bad.status_code == 409

        keep = [i for i in ids if i != "clip02"]
        res = c.post(f"/api/jobs/{job_id}/render", json={"keep": keep, "loudnorm": False})
        assert res.status_code == 202, res.text
        job = _wait(c, job_id)
        assert job["status"] == "done", job
        assert job["stage"] == "built"
        assert job["clips"] == [EXPECTED[0], EXPECTED[2]]
        assert c.get(f"/api/jobs/{job_id}/reel").status_code == 200
        # A second build of the same plan is refused rather than silently overwriting.
        again = c.post(f"/api/jobs/{job_id}/render", json={"keep": keep})
        assert again.status_code == 409
        c.delete(f"/api/jobs/{job_id}")
    app_module.store.shutdown(timeout=2)
    assert video.exists() and mark.exists()
    assert (video.stat().st_size, mark.read_bytes()) == before


@requires_ffmpeg
def test_upload_plan_only_keeps_the_upload_until_build(game_video, tmp_path, monkeypatch):
    monkeypatch.setenv("HYPECUT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HYPECUT_KEEP_UPLOADS", raising=False)
    import importlib

    from hypecut.web import app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as c, open(game_video, "rb") as fh:
        res = c.post(
            "/api/jobs",
            files={
                "file": ("game.mp4", fh, "video/mp4"),
                "marks": ("game.mark", json.dumps(MARKS).encode()),
            },
            data={"falcon_mode": "marks", "plan_only": "true"},
        )
        job_id = res.json()["id"]
        job = _wait(c, job_id)
        assert job["stage"] == "planned" and job["clips"] == []
        assert len(list((tmp_path / "data" / "uploads").glob("*.mp4"))) == 1
        ids = [cl["id"] for cl in job["plan"]["clips"]]
        assert c.post(f"/api/jobs/{job_id}/render", json={"keep": ids[:1]}).status_code == 202
        job = _wait(c, job_id)
        assert job["status"] == "done" and job["clips"] == [EXPECTED[0]]
    app_module.store.shutdown(timeout=2)
    # After the build the uploaded copy (not anything of the user's) is cleaned up.
    assert list((tmp_path / "data" / "uploads").glob("*.mp4")) == []
