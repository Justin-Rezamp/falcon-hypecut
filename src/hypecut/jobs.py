"""A small, dependency-free job queue for the web UI.

Deliberately not Celery. A single background worker thread with a bounded
queue covers the self-hosted single-box case that open-source users
actually run, and keeps `docker run` to one container. The interface
(:class:`JobStore`) is narrow enough that swapping in Redis/RQ later is a
contained change.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["JobStatus", "Job", "JobStore"]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    filename: str
    source: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    output: str | None = None
    # Every reel this job produced, in order. A long video with many
    # highlights makes more than one; `output` is the first of them, and both
    # are empty when the video had nothing in it.
    parts: list[str] = field(default_factory=list)
    variants: dict[str, str] = field(default_factory=dict)
    #: Individual clips of a Falcon job, in reel order (clip01_mark_9-09.mp4 ...).
    clips: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    error: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    #: The source (and its .mark) belong to the user — a game picked from the
    #: games folder — so no cleanup path may ever delete them.
    local_source: bool = False
    #: Falcon review flow: "plan" (cut nothing yet), "planned" (waiting for the
    #: parent to press build), "render", or "" for the one-shot flow.
    stage: str = ""

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        # Never leak server-side paths to the browser.
        data.pop("source", None)
        data["output"] = bool(self.output)
        # A count, not paths. The browser fetches part N by index.
        data["parts"] = len(self.parts)
        # Names only — the browser fetches each by name, never by path.
        data["variants"] = sorted(self.variants)
        # File names (not paths) of a Falcon job's clips; fetched by index.
        data["clips"] = [Path(c).name for c in self.clips]
        data.pop("local_source", None)
        if self.plan:
            # The plan records server-side paths; the browser needs none of them.
            data["plan"] = {
                k: v
                for k, v in self.plan.items()
                if k not in ("source", "mark_file", "analysis_source")
            }
        falcon = (self.options or {}).get("falcon")
        if falcon:
            # The uploaded .mark lives at a server path; say only that it exists.
            data["options"] = {
                **self.options,
                "falcon": {**falcon, "marks": bool(falcon.get("marks"))},
            }
        return data


class JobStore:
    """Thread-safe job registry plus a worker pool."""

    def __init__(
        self,
        data_dir: Path,
        workers: int = 1,
        max_jobs: int = 200,
        ttl_seconds: float | None = None,
        keep_uploads: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.uploads = self.data_dir / "uploads"
        self.outputs = self.data_dir / "outputs"
        for d in (self.uploads, self.outputs):
            d.mkdir(parents=True, exist_ok=True)

        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._queue: Queue[str] = Queue()
        self._max_jobs = max_jobs
        # Finished jobs older than this are deleted together with their files.
        self._ttl = ttl_seconds
        # By default the uploaded source is removed once a job finishes; renders stay until eviction.
        self._keep_uploads = keep_uploads
        self._last_sweep = 0.0
        self._stopping = threading.Event()
        self._threads = [
            threading.Thread(target=self._worker, name=f"hypecut-worker-{i}", daemon=True)
            for i in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()

    # ---------------------------------------------------------------- public

    def submit(
        self,
        filename: str,
        source: Path,
        options: dict[str, Any],
        *,
        local_source: bool = False,
        stage: str = "",
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            filename=filename,
            source=str(source),
            options=options,
            local_source=local_source,
            stage=stage,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_locked()
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [self._jobs[i] for i in ids if i in self._jobs]

    def render(self, job_id: str, keep: set[str], exports: dict[str, bool]) -> Job:
        """Queue the cut for a reviewed Falcon plan: only ``keep`` clip IDs go in."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.stage != "planned" or job.status is not JobStatus.DONE or not job.plan:
                raise ValueError("This job has no reviewed plan waiting to be built")
            ids = {c["id"] for c in job.plan.get("clips", [])}
            unknown = sorted(keep - ids)
            if unknown:
                raise ValueError(f"Unknown clip id(s): {', '.join(unknown)}")
            if not keep:
                raise ValueError("Keep at least one clip to build a reel")
            if not Path(job.source).is_file():
                raise ValueError("The game video is no longer available; pick or upload it again")
            for clip in job.plan["clips"]:
                clip["keep"] = clip["id"] in keep
            job.plan["reel_duration"] = round(
                sum(c["end"] - c["start"] for c in job.plan["clips"] if c["keep"]), 3
            )
            job.options["falcon"] = {**job.options["falcon"], **exports}
            job.stage = "render"
            job.status = JobStatus.QUEUED
            job.progress = 0.0
            job.message = "queued to build"
            job.error = None
            job.finished_at = None
        self._queue.put(job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status is JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.message = "cancelled"
                return True
        return False

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        for t in self._threads:
            t.join(timeout=timeout)

    # ---------------------------------------------------------------- worker

    def _worker(self) -> None:
        while not self._stopping.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except Empty:
                self._maybe_sweep()
                continue
            job = self.get(job_id)
            if job is None or job.status is JobStatus.CANCELLED:
                if job is not None and self._may_delete_source(job):
                    Path(job.source).unlink(missing_ok=True)
                self._queue.task_done()
                continue
            try:
                self._process(job)
            except Exception:  # pragma: no cover - defensive
                # The traceback (with server paths) goes to the log, not to the browser.
                log.exception("job %s failed", job.id)
                job.status = JobStatus.FAILED
                job.error = job.error or "internal error"
                job.finished_at = time.time()
            finally:
                finished = job.status in (JobStatus.DONE, JobStatus.FAILED)
                # A planned job keeps its upload: the parent still has to press build.
                if finished and job.stage != "planned" and self._may_delete_source(job):
                    Path(job.source).unlink(missing_ok=True)
                self._queue.task_done()

    def _process(self, job: Job) -> None:
        from .config import load_config
        from .pipeline import analyze

        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.message = "starting"

        def progress(fraction: float, message: str) -> None:
            job.progress = round(max(0.0, min(1.0, fraction)), 4)
            job.message = message

        if job.options.get("falcon"):
            try:
                self._process_falcon(job, progress)
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.message = "failed"
                raise
            finally:
                job.finished_at = time.time()
            return

        try:
            cfg = load_config(job.options.get("profile") or None)
            overrides = _options_to_overrides(job.options)
            if overrides:
                cfg = cfg.merged(overrides)

            plan = analyze(job.source, cfg, progress=lambda p, m: progress(p * 0.55, m))
            job.plan = plan.to_dict()

            # An empty result is a finished job, not a failed one. Showing a
            # red error for "this video has nothing in it" tells the user
            # something is broken when the honest answer is that their footage
            # is quiet, and the difference matters to someone who is not going
            # to read a stack trace.
            if not plan.segments:
                job.status = JobStatus.DONE
                job.progress = 1.0
                job.message = plan.empty_reason
                return

            from .pipeline import render_reels

            reels = render_reels(
                plan,
                self.outputs / f"{job.id}.mp4",
                cfg,
                progress=lambda p, m: progress(0.55 + p * 0.45, m),
            )
            job.parts = [str(reel.path) for reel in reels]
            job.output = job.parts[0]
            job.variants = {k: str(v) for k, v in reels[0].variants.items()}
            job.plan = plan.to_dict()
            for reel in reels:
                if reel.sidecar:
                    Path(reel.sidecar).write_text(
                        json.dumps(job.plan, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
            job.status = JobStatus.DONE
            job.progress = 1.0
            job.message = f"{len(reels)} reels ready" if len(reels) > 1 else "done"
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.message = "failed"
            raise
        finally:
            job.finished_at = time.time()

    def _process_falcon(self, job: Job, progress) -> None:
        """XbotGo Falcon job: marks / hybrid / detect, clips plus a reel."""
        from .falcon import FalconOptions, plan_falcon, plan_from_dict, render_falcon

        opts_in = job.options["falcon"]
        pad = float(opts_in.get("pad", 10.0))
        opts = FalconOptions(
            mode=str(opts_in.get("mode") or ""),
            before=pad,
            after=pad,
            extras=int(opts_in.get("extras", 0)),
            phone=bool(opts_in.get("phone")),
            vertical=bool(opts_in.get("vertical")),
            loudnorm=bool(opts_in.get("phone") or opts_in.get("vertical")),
            profile=opts_in.get("profile"),
            cache_dir=str(self.data_dir / "proxies"),
        )
        holder: dict[str, Any] = {}
        if job.stage == "render" and job.plan:
            plan = plan_from_dict(job.plan)
            plan.source = job.source
            opts.loudnorm = bool(opts_in.get("loudnorm"))
            base = 0.0
        else:
            plan = plan_falcon(
                job.source,
                marks=opts_in.get("marks"),
                options=opts,
                progress=lambda p, m: progress(p * (1.0 if job.stage == "plan" else 0.4), m),
                _ctx_out=holder,
            )
            job.plan = plan.to_dict()
            base = 0.4
            if job.stage == "plan":
                job.stage = "planned"
                job.status = JobStatus.DONE
                job.progress = 1.0
                job.message = f"{len(plan.kept)} clips planned — nothing cut yet"
                return
        if not plan.kept:
            job.status = JobStatus.DONE
            job.progress = 1.0
            job.message = "no clips to cut"
            return
        out = render_falcon(
            plan,
            self.outputs / job.id,
            opts,
            progress=lambda p, m: progress(base + p * (1.0 - base), m),
            ctx=holder.get("ctx"),
        )
        job.output = str(out.highlights)
        job.parts = [str(out.highlights)]
        job.clips = [str(c) for c in out.clips]
        job.variants = {
            k: str(v) for k, v in (("phone", out.phone), ("vertical", out.vertical)) if v
        }
        plan_dict = plan.to_dict()
        plan_dict["render_notes"] = out.notes
        job.plan = plan_dict
        job.status = JobStatus.DONE
        job.progress = 1.0
        job.message = f"{len(out.clips)} clips ready"
        job.stage = "built" if job.stage == "render" else job.stage
        if opts_in.get("marks") and self._may_delete_source(job):
            Path(opts_in["marks"]).unlink(missing_ok=True)

    # ---------------------------------------------------------------- private

    def _may_delete_source(self, job: Job) -> bool:
        return not job.local_source and not self._keep_uploads

    def _maybe_sweep(self, every: float = 60.0) -> None:
        now = time.time()
        if now - self._last_sweep < every:
            return
        self._last_sweep = now
        with self._lock:
            self._evict_locked(now)

    def _evict_locked(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self._ttl is not None:
            for job_id in list(self._order):
                job = self._jobs.get(job_id)
                if job and job.finished_at and now - job.finished_at > self._ttl:
                    self._drop_locked(job_id)
        while len(self._order) > self._max_jobs:
            self._drop_locked(self._order[0])

    def _drop_locked(self, job_id: str) -> None:
        if job_id in self._order:
            self._order.remove(job_id)
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        # Every reel part (not only the first), variants, and sidecars named after the job id.
        paths = {job.output, *job.parts, *job.variants.values()}
        paths.update(str(p) for p in self.outputs.glob(f"{job.id}*") if p.is_file())
        falcon = (job.options or {}).get("falcon") or {}
        if not job.local_source:
            # A game picked from the games folder is the user's own file: never delete it.
            paths.add(job.source)
            if falcon.get("marks"):
                paths.add(str(falcon["marks"]))
        for path in paths:
            if path:
                Path(path).unlink(missing_ok=True)
        # A Falcon job writes a folder (clips/, reel, plan) rather than files.
        folder = self.outputs / job.id
        if folder.is_dir():
            import shutil

            shutil.rmtree(folder, ignore_errors=True)


def _options_to_overrides(options: dict[str, Any]) -> dict[str, Any]:
    seg: dict[str, Any] = {}
    for key in ("max_clips", "clips_per_reel", "min_duration", "max_duration", "percentile"):
        if options.get(key) is not None:
            seg[key] = options[key]
    if options.get("target_duration") is not None:
        target = float(options["target_duration"])
        seg["target_duration"] = target if target > 0 else None
    if options.get("snap_to_shots") is not None:
        seg["snap_to_shots"] = bool(options["snap_to_shots"])
    if options.get("trim_to_silence") is not None:
        seg["trim_to_silence"] = bool(options["trim_to_silence"])

    render: dict[str, Any] = {}
    reframe: dict[str, Any] = {}
    if options.get("reframe"):
        reframe["mode"] = str(options["reframe"])
    if options.get("reframe_track") is not None:
        reframe["track"] = bool(options["reframe_track"])
    if options.get("react_to_facecam") is not None:
        reframe["react_to_facecam"] = bool(options["react_to_facecam"])
        if options["react_to_facecam"]:
            # The web UI has no box input and never did — before auto
            # detection this silently used the default corner, which was
            # usually wrong. Locating it is strictly better here.
            reframe["facecam"] = "auto"
    if reframe:
        render["reframe"] = reframe

    out: dict[str, Any] = {}
    if options.get("variants"):
        out["variants"] = dict(options["variants"])
    if seg:
        out["segments"] = seg
    if render:
        out["render"] = render
    if options.get("refiners"):
        out["refiners"] = list(options["refiners"])
    if options.get("weights"):
        out["signals"] = {"weights": dict(options["weights"])}
    return out
