"""In-memory background job manager for remux operations."""

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("webclipper")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class FileResult:
    source: str
    output: str = ""
    status: str = "pending"
    error: str = ""


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.PENDING
    files: list[FileResult] = field(default_factory=list)
    current_index: int = 0
    created: float = field(default_factory=time.time)
    error: str = ""

    @property
    def progress(self) -> float:
        if not self.files:
            return 0
        done = sum(1 for f in self.files if f.status in ("done", "failed"))
        return round(done / len(self.files) * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "progress": self.progress,
            "current_index": self.current_index,
            "total_files": len(self.files),
            "created": self.created,
            "error": self.error,
            "files": [
                {"source": f.source, "output": f.output, "status": f.status, "error": f.error}
                for f in self.files
            ],
        }


# Global job store
_jobs: dict[str, Job] = {}


def create_job(file_paths: list[str]) -> Job:
    job = Job(files=[FileResult(source=p) for p in file_paths])
    _jobs[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def get_all_jobs() -> list[dict]:
    return [j.to_dict() for j in sorted(_jobs.values(), key=lambda j: j.created, reverse=True)]


async def run_remux_job(job: Job):
    """Execute a remux job in the background."""
    from app.media import remux_file

    job.status = JobStatus.RUNNING
    try:
        for i, fr in enumerate(job.files):
            job.current_index = i
            fr.status = "running"

            src = Path(fr.source)
            if src.suffix.lower() != ".mkv":
                fr.status = "done"
                fr.output = fr.source
                continue

            out = src.with_suffix(".mp4")
            try:
                await remux_file(str(src), str(out))
                # Remove original MKV after successful remux
                if out.exists() and out.stat().st_size > 0:
                    os.remove(str(src))
                fr.output = str(out)
                fr.status = "done"
                log.info("Remuxed %s -> %s", src, out)
            except Exception as e:
                fr.status = "failed"
                fr.error = str(e)[:300]
                log.error("Remux failed for %s: %s", src, e)

        has_failures = any(f.status == "failed" for f in job.files)
        job.status = JobStatus.FAILED if all(f.status == "failed" for f in job.files) else JobStatus.DONE
        if has_failures and job.status == JobStatus.DONE:
            job.error = "Some files failed to remux"
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)[:300]
