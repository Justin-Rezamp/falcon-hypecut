"""Web API: upload a Falcon game with its .mark file."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.conftest import HAS_FFMPEG, requires_ffmpeg
from tests.test_falcon import CLICKS, EXPECTED, _make_game, _write_marks


@pytest.fixture(scope="module")
def falcon_game(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    video = _make_game(tmp_path_factory.mktemp("falcon-web") / "game.mp4")
    _write_marks(video, CLICKS)
    return video


@requires_ffmpeg
def test_web_upload_with_mark_file(falcon_game, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("HYPECUT_DATA_DIR", str(tmp_path / "data"))
    from hypecut.web import app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as client:
        with open(falcon_game, "rb") as fh:
            res = client.post(
                "/api/jobs",
                files={
                    "file": ("game.mp4", fh, "video/mp4"),
                    "marks": ("game.mark", falcon_game.with_suffix(".mark").read_bytes()),
                },
                data={"falcon_mode": "marks"},
            )
        assert res.status_code == 202, res.text
        job_id = res.json()["id"]
        for _ in range(300):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert job["status"] == "done", job
        assert job["clips"] == EXPECTED
        assert job["options"]["falcon"]["marks"] is True  # no server path leaks
        assert job["plan"]["falcon_plan"] == 1
        clip = client.get(f"/api/jobs/{job_id}/clip/2")
        assert clip.status_code == 200
        assert "clip02_mark_0-40_merged.mp4" in clip.headers["content-disposition"]
        assert client.get(f"/api/jobs/{job_id}/reel").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/clip/9").status_code == 404

        bad = client.post(
            "/api/jobs", files={"file": ("g.mp4", b"x", "video/mp4"), "marks": ("g.mark", b"{nope")}
        )
        assert bad.status_code == 422
        wrong = client.post(
            "/api/jobs", files={"file": ("g.mp4", b"x", "video/mp4"), "marks": ("g.txt", b"[]")}
        )
        assert wrong.status_code == 415
        needs = client.post(
            "/api/jobs",
            files={"file": ("g.mp4", b"x", "video/mp4")},
            data={"falcon_mode": "hybrid"},
        )
        assert needs.status_code == 422
    app_module.store.shutdown(timeout=2)
