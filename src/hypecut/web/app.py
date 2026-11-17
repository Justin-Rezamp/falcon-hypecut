"""FastAPI application: upload a VOD, watch it process, download the reel.

Endpoints
---------
``POST /api/jobs``            multipart upload, returns a job id
``GET  /api/jobs``            recent jobs
``GET  /api/jobs/{id}``       status, progress and the cut list
``GET  /api/jobs/{id}/reel``  the rendered mp4
``GET  /api/jobs/{id}/plan``  the JSON cut list
``GET  /api/jobs/{id}/clip/{n}``  clip ``n`` (1-based) of a Falcon job
``GET  /api/games``           games already on this computer (under the games folder)
``POST /api/games/plan``      plan a game from the games folder (cuts nothing)
``POST /api/jobs/{id}/render``  build the reel from a reviewed plan (keep/drop per clip)
``GET  /api/jobs/{id}/thumb/{clip}``  a small JPEG frame at a clip's first tap
``GET  /api/meta``            registered signals, refiners and limits
``GET  /login?token=...``     sets the session cookie when HYPECUT_TOKEN is configured

Security
--------
The server binds to localhost by default. If you expose it on a network, set
``HYPECUT_TOKEN``: every endpoint except ``/api/meta`` and ``/login`` then
requires ``Authorization: Bearer <token>`` or the cookie set by
``/login?token=<token>`` (open that URL once in the browser).
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from .. import ffmpeg as ff
from ..cli import VARIANT_PRESETS
from ..config import describe_profiles
from ..jobs import JobStore
from ..marks import MarkFileError, parse_marks
from ..reframe import MODES as REFRAME_MODES

DATA_DIR = Path(os.environ.get("HYPECUT_DATA_DIR", "./hypecut-data")).resolve()
#: A 4K XbotGo Falcon game is ~22 GB per hour at 50 Mbps, so the default
#: allows a full 90-minute 4K match. Lower it for shared/public deployments.
MAX_UPLOAD_MB = int(os.environ.get("HYPECUT_MAX_UPLOAD_MB", "65536"))
WORKERS = int(os.environ.get("HYPECUT_WORKERS", "1"))
# Finished jobs (and their uploads / renders) are deleted after this many hours.
JOB_TTL_HOURS = float(os.environ.get("HYPECUT_JOB_TTL_HOURS", "24"))
# Keep the uploaded source after processing (default: delete it to save disk).
KEEP_UPLOADS = os.environ.get("HYPECUT_KEEP_UPLOADS", "").lower() in {"1", "true", "yes"}
_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def _default_profiles_dir() -> Path:
    """``./configs`` when run from the repo, else the repo's configs next to the package."""
    cwd_configs = Path("configs")
    return cwd_configs if cwd_configs.is_dir() else _REPO_CONFIGS


PROFILES_DIR = Path(os.environ.get("HYPECUT_PROFILES_DIR") or _default_profiles_dir())
#: The only folder "Pick a game already on this computer" may read from.
GAMES_DIR = Path(os.environ.get("HYPECUT_GAMES_DIR", "./games")).resolve()
#: How deep under GAMES_DIR to look (games/<date>/video.mp4 is depth 2).
GAMES_MAX_DEPTH = 3
TOKEN = os.environ.get("HYPECUT_TOKEN", "").strip()
TOKEN_COOKIE = "hypecut_token"
PUBLIC_PATHS = {"/api/meta", "/login"}
ALLOWED_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v"}

STATIC_DIR = Path(__file__).parent / "static"
FALCON_MODES = ("marks", "hybrid", "detect")
#: A real .mark file is a few hundred bytes; anything this big is not one.
MAX_MARK_BYTES = 1024 * 1024

store = JobStore(
    DATA_DIR,
    workers=WORKERS,
    ttl_seconds=JOB_TTL_HOURS * 3600 if JOB_TTL_HOURS > 0 else None,
    keep_uploads=KEEP_UPLOADS,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    store.shutdown()


app = FastAPI(title="HypeCut", version=__version__, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _token_ok(candidate: str | None) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), TOKEN.encode())


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Optional shared-secret auth for deployments reachable from a network."""
    if not TOKEN or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    bearer = header[7:] if header.lower().startswith("bearer ") else None
    if _token_ok(bearer) or _token_ok(request.cookies.get(TOKEN_COOKIE)):
        return await call_next(request)
    return JSONResponse(
        {"detail": "Unauthorized: open /login?token=<HYPECUT_TOKEN> first"}, status_code=401
    )


@app.get("/login")
def login(token: str = "") -> Response:
    if not TOKEN:
        return RedirectResponse("/", status_code=303)
    if not _token_ok(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        TOKEN_COOKIE, TOKEN, httponly=True, samesite="strict", max_age=30 * 24 * 3600
    )
    return response


def _resolve_profile(value: str) -> str | None:
    """Map the form value to a shipped profile file; never accept arbitrary paths."""
    if not value:
        return None
    profiles = describe_profiles(PROFILES_DIR) if PROFILES_DIR.is_dir() else []
    for profile in profiles:
        if value in (profile["name"], profile["path"]):
            return profile["path"]
    raise HTTPException(
        status_code=422,
        detail=f"Unknown profile {value!r}; available: {sorted(p['name'] for p in profiles)}",
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    from ..refine import available_refiners
    from ..refine import load_plugins as lrp
    from ..signals import available_signals
    from ..signals import load_plugins as lsp

    lsp()
    lrp()
    return {
        "version": __version__,
        "signals": available_signals(),
        "refiners": available_refiners(),
        "max_upload_mb": MAX_UPLOAD_MB,
        "allowed_suffixes": sorted(ALLOWED_SUFFIXES),
        "reframe_modes": list(REFRAME_MODES),
        "variants": sorted(VARIANT_PRESETS),
        # Name + the profile's own first comment line, so the picker can offer
        # "football, basketball, broadcast coverage" instead of a filename.
        "profiles": describe_profiles(PROFILES_DIR) if PROFILES_DIR.is_dir() else [],
        "falcon_modes": list(FALCON_MODES),
        "games_folder": GAMES_DIR.name,
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    target_duration: float = Form(120.0, ge=0, le=36000),
    max_clips: int = Form(0, ge=0, le=1000),
    clips_per_reel: int = Form(10, ge=1, le=200),
    percentile: float = Form(92.0, ge=0, le=100),
    min_duration: float = Form(4.0, gt=0, le=600),
    max_duration: float = Form(20.0, gt=0, le=3600),
    refiners: str = Form("diversity,pacing,similarity"),
    profile: str = Form(""),
    reframe: str = Form("off"),
    reframe_track: bool = Form(False),
    snap_to_shots: bool = Form(True),
    trim_to_silence: bool = Form(True),
    react_to_facecam: bool = Form(False),
    also: str = Form(""),
    # XbotGo Falcon: an optional .mark file (parent taps) and how to use it.
    marks: UploadFile | None = File(None),
    falcon_mode: str = Form(""),
    falcon_pad: float = Form(10.0, ge=0, le=120),
    falcon_extras: int = Form(0, ge=0, le=20),
    falcon_phone: bool = Form(False),
    falcon_vertical: bool = Form(False),
    # Review flow: plan the Falcon clips and stop, so the parent can keep/drop first.
    plan_only: bool = Form(False),
) -> JSONResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if marks is not None and not (marks.filename or ""):
        marks = None  # an empty file field from a form that left it blank
    falcon_mode = falcon_mode.strip().lower()
    if falcon_mode and falcon_mode not in FALCON_MODES:
        raise HTTPException(
            status_code=422, detail=f"Unknown falcon_mode {falcon_mode!r}; expected {FALCON_MODES}"
        )
    if falcon_mode in ("marks", "hybrid") and marks is None:
        raise HTTPException(
            status_code=422, detail=f"falcon_mode {falcon_mode!r} needs a .mark file"
        )
    mark_bytes: bytes | None = None
    if marks is not None:
        if Path(marks.filename or "").suffix.lower() != ".mark":
            raise HTTPException(
                status_code=415, detail="The marks file must be a Falcon .mark file"
            )
        mark_bytes = await marks.read(MAX_MARK_BYTES + 1)
        if len(mark_bytes) > MAX_MARK_BYTES:
            raise HTTPException(status_code=413, detail="The .mark file is unexpectedly large")
        try:
            parse_marks(json.loads(mark_bytes.decode("utf-8-sig")))
        except (UnicodeDecodeError, json.JSONDecodeError, MarkFileError) as exc:
            raise HTTPException(
                status_code=422, detail=f"Could not read the .mark file: {exc}"
            ) from exc
    if min_duration > max_duration:
        raise HTTPException(status_code=422, detail="min_duration must not exceed max_duration")
    profile_path = _resolve_profile(profile)
    if reframe not in REFRAME_MODES:
        raise HTTPException(
            status_code=422, detail=f"Unknown reframe mode {reframe!r}; expected {REFRAME_MODES}"
        )
    wanted = [name.strip() for name in also.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in VARIANT_PRESETS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown variant(s) {unknown}; available: {sorted(VARIANT_PRESETS)}",
        )
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {suffix!r}. Allowed: {sorted(ALLOWED_SUFFIXES)}",
        )

    safe_name = Path(file.filename or "upload").name
    dest = store.uploads / f"{os.urandom(6).hex()}{suffix}"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB limit"
                )
            # Disk writes off the event loop so one big upload does not stall other requests.
            await run_in_threadpool(fh.write, chunk)

    options = {
        "target_duration": target_duration,
        "max_clips": max_clips,
        "clips_per_reel": clips_per_reel,
        "percentile": percentile,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "refiners": [r.strip() for r in refiners.split(",") if r.strip()],
        "profile": profile_path,
        "reframe": reframe,
        "reframe_track": reframe_track,
        "snap_to_shots": snap_to_shots,
        "trim_to_silence": trim_to_silence,
        "react_to_facecam": react_to_facecam,
        "variants": {name: VARIANT_PRESETS[name] for name in dict.fromkeys(wanted)},
    }
    if mark_bytes is not None or falcon_mode:
        mark_path = None
        if mark_bytes is not None:
            mark_path = dest.with_suffix(".mark")
            mark_path.write_bytes(mark_bytes)
        options["falcon"] = {
            "mode": falcon_mode or ("marks" if mark_path else "detect"),
            "marks": str(mark_path) if mark_path else None,
            "pad": falcon_pad,
            "extras": falcon_extras,
            "phone": falcon_phone,
            "vertical": falcon_vertical,
            # The profile picker still applies; empty means the Falcon default.
            "profile": profile_path
            if profile_path and "falcon" in Path(profile_path).stem
            else None,
        }
    stage = "plan" if plan_only and "falcon" in options else ""
    job = store.submit(safe_name, dest, options, stage=stage)
    return JSONResponse({"id": job.id, "status": job.status.value}, status_code=202)


# ------------------------------------------------------------- local games


class GamePlanRequest(BaseModel):
    game: str = Field(..., min_length=1, max_length=1024)
    mark: str | None = Field(None, max_length=1024)
    mode: str = "marks"
    pad: float = Field(10.0, ge=0, le=120)


class RenderRequest(BaseModel):
    keep: list[str] = Field(..., max_length=1000)
    phone: bool = False
    vertical: bool = False
    loudnorm: bool = False


def _inside_games(path: Path) -> bool:
    try:
        path.resolve().relative_to(GAMES_DIR)
    except (ValueError, OSError):
        return False
    return True


def _resolve_game_file(rel: str, suffixes: set[str]) -> Path:
    """Map a games-folder relative path from the browser to a real file, or refuse.

    Only paths the listing itself could have produced are accepted: relative,
    no ``..`` parts, inside GAMES_DIR after resolving symlinks, an existing file
    with an allowed suffix. Anything else is the same 404, so a probe learns nothing.
    """
    missing = HTTPException(status_code=404, detail="No such game in the games folder")
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or rel.startswith("/") or ":" in rel or any(p == ".." for p in parts):
        raise missing
    candidate = GAMES_DIR.joinpath(*parts)
    if not _inside_games(candidate) or not candidate.is_file():
        raise missing
    if candidate.suffix.lower() not in suffixes:
        raise missing
    return candidate.resolve()


def _rel(path: Path) -> str:
    return path.resolve().relative_to(GAMES_DIR).as_posix()


def _mark_summary(path: Path, video: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_MARK_BYTES:
            return None
        marks = parse_marks(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, MarkFileError):
        return None
    return {
        "path": _rel(path),
        "name": path.name,
        "taps": len(marks),
        "same_name": path.stem.lower() in (video.stem.lower(), video.name.lower()),
    }


def scan_games() -> list[dict[str, Any]]:
    """Videos under GAMES_DIR with the .mark files that could belong to each.

    A Falcon .mark is usually named after the camera's recording
    (VID_20260425_133314_01_01.mark), not the video the parent saved, so every
    readable .mark in the video's folder and its hidden .data folder is offered,
    same-name matches first. Nothing outside GAMES_DIR is ever listed.
    """
    if not GAMES_DIR.is_dir():
        return []
    games: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(GAMES_DIR, followlinks=False):
        here = Path(dirpath)
        depth = len(here.relative_to(GAMES_DIR).parts)
        # Skip hidden folders (except .data, read per video below) and our own outputs.
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and not d.endswith("_highlights")
        )
        if depth >= GAMES_MAX_DEPTH:
            dirnames[:] = []
        videos = sorted(
            here / f
            for f in filenames
            if Path(f).suffix.lower() in ALLOWED_SUFFIXES and not f.startswith(".")
        )
        if not videos:
            continue
        mark_files = [
            p
            for folder in (here, here / ".data")
            if folder.is_dir() and _inside_games(folder)
            for p in sorted(folder.iterdir())
            if p.suffix.lower() == ".mark" and p.is_file() and _inside_games(p)
        ]
        for video in videos:
            if not _inside_games(video):
                continue  # a symlink pointing out of the games folder
            marks = [m for m in (_mark_summary(p, video) for p in mark_files) if m]
            marks.sort(key=lambda m: (not m["same_name"], m["name"]))
            games.append(
                {
                    "path": _rel(video),
                    "name": video.name,
                    "folder": _rel(here) if here != GAMES_DIR else "",
                    "size": video.stat().st_size,
                    "marks": marks,
                }
            )
    return games


@app.get("/api/games")
def list_games() -> dict[str, Any]:
    return {"root": GAMES_DIR.name, "games": scan_games()}


@app.post("/api/games/plan")
def plan_local_game(req: GamePlanRequest) -> JSONResponse:
    mode = req.mode.strip().lower()
    if mode not in FALCON_MODES:
        raise HTTPException(
            status_code=422, detail=f"Unknown mode {mode!r}; expected {FALCON_MODES}"
        )
    video = _resolve_game_file(req.game, ALLOWED_SUFFIXES)
    mark_path = _resolve_game_file(req.mark, {".mark"}) if req.mark else None
    if mode in ("marks", "hybrid") and mark_path is None:
        raise HTTPException(status_code=422, detail=f"{mode!r} needs a .mark file")
    if mark_path is not None and _mark_summary(mark_path, video) is None:
        raise HTTPException(status_code=422, detail="Could not read that .mark file")
    options = {
        "falcon": {
            "mode": mode,
            "marks": str(mark_path) if mark_path else None,
            "pad": req.pad,
            "extras": 0,
            "phone": False,
            "vertical": False,
            "profile": None,
        }
    }
    job = store.submit(video.name, video, options, local_source=True, stage="plan")
    return JSONResponse({"id": job.id, "status": job.status.value}, status_code=202)


@app.post("/api/jobs/{job_id}/render")
def render_job(job_id: str, req: RenderRequest) -> JSONResponse:
    try:
        job = store.render(
            job_id,
            set(req.keep),
            {"phone": req.phone, "vertical": req.vertical, "loudnorm": req.loudnorm},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown job") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return JSONResponse({"id": job.id, "status": job.status.value}, status_code=202)


@app.get("/api/jobs/{job_id}/thumb/{clip_id}")
def clip_thumb(job_id: str, clip_id: str) -> FileResponse:
    """One small frame at the clip's first tap (or its middle), cached per job."""
    job = store.get(job_id)
    clips = (job.plan or {}).get("clips", []) if job else []
    clip = next((c for c in clips if c.get("id") == clip_id), None)
    if job is None or clip is None or not Path(job.source).is_file():
        raise HTTPException(status_code=404, detail="No such clip")
    folder = store.outputs / job.id / "thumbs"
    dest = folder / f"{clip['id']}.jpg"
    if not dest.exists():
        folder.mkdir(parents=True, exist_ok=True)
        at = clip["marks"][0] if clip.get("marks") else (clip["start"] + clip["end"]) / 2
        try:
            ff.run(
                ff.cmd(
                    "ffmpeg -hide_banner -loglevel error -y -ss {at} -i {src} -frames:v 1 "
                    "-vf scale=480:-2 -q:v 6 {dest}",
                    at=f"{max(0.0, float(at)):.3f}",
                    src=job.source,
                    dest=str(dest),
                )
            )
        except (ff.FFmpegError, OSError):
            raise HTTPException(status_code=404, detail="Could not read a frame") from None
    return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/jobs")
def list_jobs(limit: int = 25) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    return {"jobs": [j.public() for j in store.list(limit=limit)]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job.public()


@app.get("/api/jobs/{job_id}/plan")
def get_plan(job_id: str) -> JSONResponse:
    job = store.get(job_id)
    if job is None or job.plan is None:
        raise HTTPException(status_code=404, detail="No plan for this job yet")
    return JSONResponse(job.plan)


@app.get("/api/jobs/{job_id}/reel")
def get_reel(job_id: str) -> FileResponse:
    job = store.get(job_id)
    if job is None or not job.output or not Path(job.output).exists():
        raise HTTPException(status_code=404, detail="Reel not ready")
    stem = Path(job.filename).stem or "highlights"
    return FileResponse(job.output, media_type="video/mp4", filename=f"{stem}_highlights.mp4")


@app.get("/api/jobs/{job_id}/part/{index}")
def get_part(job_id: str, index: int) -> FileResponse:
    """Reel ``index`` (1-based) of a cut that was too long for a single file."""
    job = store.get(job_id)
    parts = (job.parts if job else None) or []
    if not 1 <= index <= len(parts) or not Path(parts[index - 1]).exists():
        raise HTTPException(status_code=404, detail=f"No part {index} for this job")
    stem = Path(job.filename).stem or "highlights"  # type: ignore[union-attr]
    return FileResponse(
        parts[index - 1], media_type="video/mp4", filename=f"{stem}_highlights_{index}.mp4"
    )


@app.get("/api/jobs/{job_id}/clip/{index}")
def get_clip(job_id: str, index: int) -> FileResponse:
    """Clip ``index`` (1-based) of a Falcon job, under its clip01_mark_... name."""
    job = store.get(job_id)
    clips = (job.clips if job else None) or []
    if not 1 <= index <= len(clips) or not Path(clips[index - 1]).exists():
        raise HTTPException(status_code=404, detail=f"No clip {index} for this job")
    path = Path(clips[index - 1])
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/reel/{variant}")
def get_variant_reel(job_id: str, variant: str) -> FileResponse:
    """One of the extra aspect ratios rendered from the same analysis."""
    job = store.get(job_id)
    path = (job.variants or {}).get(variant) if job else None
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"No {variant!r} render for this job")
    stem = Path(job.filename).stem or "highlights"  # type: ignore[union-attr]
    return FileResponse(path, media_type="video/mp4", filename=f"{stem}_{variant}.mp4")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    if store.cancel(job_id):
        return {"cancelled": True}
    return {"cancelled": False, "status": job.status.value}


def cleanup_data_dir() -> None:  # pragma: no cover - operational helper
    """Delete every upload and output. Used by tests and `make clean`."""
    shutil.rmtree(DATA_DIR, ignore_errors=True)


__all__ = ["app", "store", "cleanup_data_dir"]

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
    print(json.dumps({"data_dir": str(DATA_DIR)}))
