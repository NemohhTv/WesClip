"""WebClipper – FastAPI backend."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.config import (
    CLIPS_DIR,
    DATA_DIR,
    VIDEO_EXTENSIONS,
    load_config,
    save_config,
)
from app.jobs import create_job, get_all_jobs, get_job, run_remux_job
from app.media import create_clip, ensure_preview, ensure_thumbnail, get_file_info

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("webclipper")

app = FastAPI(title="WebClipper", version="1.0.0")


# ── Pydantic models ─────────────────────────────────────────────────────────

class SourceIn(BaseModel):
    label: str
    path: str


class SettingsIn(BaseModel):
    auto_refresh: Optional[bool] = None
    refresh_interval: Optional[int] = None
    clips_folder: Optional[str] = None


class ClipRequest(BaseModel):
    source_path: str
    title: str = "clip"
    game: str = ""
    start: float
    end: float
    mode: str = "fast"          # fast | accurate
    container: str = "mp4"
    audio_mode: str = "keep"    # keep | mix
    selected_tracks: list[int] = []
    gains: dict[str, float] = {}


class DeleteRequest(BaseModel):
    paths: list[str]


class RemuxRequest(BaseModel):
    paths: list[str]


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path(__file__).parent.parent / "frontend" / "index.html"
    if not html.exists():
        raise HTTPException(500, "Frontend not found")
    return HTMLResponse(html.read_text())


# ── Sources ──────────────────────────────────────────────────────────────────

@app.get("/api/sources")
async def get_sources():
    cfg = load_config()
    return cfg.get("sources", [])


@app.post("/api/sources")
async def add_source(src: SourceIn):
    cfg = load_config()
    sources = cfg.get("sources", [])
    if not Path(src.path).is_dir():
        raise HTTPException(400, f"Path not found or not a directory: {src.path}")
    sources.append({"label": src.label, "path": src.path})
    cfg["sources"] = sources
    save_config(cfg)
    return {"ok": True, "sources": sources}


@app.delete("/api/sources")
async def delete_source(path: str = Query(...)):
    cfg = load_config()
    sources = [s for s in cfg.get("sources", []) if s["path"] != path]
    cfg["sources"] = sources
    save_config(cfg)
    return {"ok": True, "sources": sources}


# ── Settings ─────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    cfg = load_config()
    return {
        "auto_refresh": cfg.get("auto_refresh", False),
        "refresh_interval": cfg.get("refresh_interval", 30),
        "clips_folder": cfg.get("clips_folder", str(CLIPS_DIR)),
    }


@app.put("/api/settings")
async def update_settings(s: SettingsIn):
    cfg = load_config()
    if s.auto_refresh is not None:
        cfg["auto_refresh"] = s.auto_refresh
    if s.refresh_interval is not None:
        cfg["refresh_interval"] = s.refresh_interval
    if s.clips_folder is not None:
        cfg["clips_folder"] = s.clips_folder
    save_config(cfg)
    return {"ok": True}


# ── Folder browser ───────────────────────────────────────────────────────────

@app.get("/api/browse")
async def browse(path: str = Query("/")):
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(400, "Not a directory")
    try:
        dirs = sorted(
            [{"name": d.name, "path": str(d)} for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")],
            key=lambda x: x["name"].lower(),
        )
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    return {"current": str(p), "parent": str(p.parent), "directories": dirs}


# ── Recordings ───────────────────────────────────────────────────────────────

@app.get("/api/recordings")
async def list_recordings(source: Optional[str] = None):
    cfg = load_config()
    sources = cfg.get("sources", [])
    if source:
        sources = [s for s in sources if s["path"] == source]

    recordings = []
    for src in sources:
        src_path = Path(src["path"])
        if not src_path.is_dir():
            continue
        for f in src_path.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                stat = f.stat()
                recordings.append({
                    "name": f.name,
                    "path": str(f),
                    "source": src["label"],
                    "source_path": src["path"],
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "extension": f.suffix.lower(),
                    "is_mkv": f.suffix.lower() == ".mkv",
                })

    recordings.sort(key=lambda r: r["modified"], reverse=True)
    return recordings


@app.get("/api/recordings/info")
async def recording_info(path: str = Query(...)):
    if not Path(path).exists():
        raise HTTPException(404, "File not found")
    try:
        return get_file_info(path)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/recordings/thumbnail")
async def recording_thumbnail(path: str = Query(...)):
    if not Path(path).exists():
        raise HTTPException(404, "File not found")
    try:
        thumb = await ensure_thumbnail(path)
        return FileResponse(thumb, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(500, f"Thumbnail error: {e}")


@app.get("/api/recordings/preview")
async def recording_preview(path: str = Query(...)):
    if not Path(path).exists():
        raise HTTPException(404, "File not found")
    try:
        preview_path, strategy = await ensure_preview(path)
        return {"path": preview_path, "strategy": strategy}
    except Exception as e:
        raise HTTPException(500, f"Preview error: {e}")


@app.post("/api/recordings/delete")
async def delete_recordings(req: DeleteRequest):
    deleted = []
    errors = []
    for p in req.paths:
        try:
            fp = Path(p)
            if fp.exists():
                fp.unlink()
                deleted.append(p)
            else:
                errors.append({"path": p, "error": "Not found"})
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return {"deleted": deleted, "errors": errors}


# ── Remux ────────────────────────────────────────────────────────────────────

@app.post("/api/recordings/remux")
async def remux_recordings(req: RemuxRequest):
    mkv_paths = [p for p in req.paths if Path(p).suffix.lower() == ".mkv"]
    if not mkv_paths:
        raise HTTPException(400, "No MKV files provided")
    for p in mkv_paths:
        if not Path(p).exists():
            raise HTTPException(404, f"File not found: {p}")

    job = create_job(mkv_paths)
    asyncio.create_task(run_remux_job(job))
    return {"job_id": job.id}


# ── Jobs ─────────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs():
    return get_all_jobs()


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j.to_dict()


# ── Streaming ────────────────────────────────────────────────────────────────

@app.get("/api/stream")
async def stream_file(path: str = Query(...)):
    """Stream a file directly (for browser video playback)."""
    fp = Path(path)
    if not fp.exists():
        raise HTTPException(404, "File not found")
    media_type = "video/mp4"
    ext = fp.suffix.lower()
    if ext == ".webm":
        media_type = "video/webm"
    elif ext == ".mov":
        media_type = "video/quicktime"
    elif ext == ".mkv":
        media_type = "video/x-matroska"
    return FileResponse(str(fp), media_type=media_type)


# ── Clips ────────────────────────────────────────────────────────────────────

@app.post("/api/clips")
async def create_clip_endpoint(req: ClipRequest):
    cfg = load_config()
    clips_dir = Path(cfg.get("clips_folder", str(CLIPS_DIR)))
    clips_dir.mkdir(parents=True, exist_ok=True)

    # Build output filename
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in req.title).strip() or "clip"
    ts = int(time.time())
    ext = f".{req.container}" if not req.container.startswith(".") else req.container
    output_name = f"{safe_title}_{ts}{ext}"
    output_path = clips_dir / output_name

    gains = {int(k): v for k, v in req.gains.items()}

    try:
        result = await create_clip(
            source_path=req.source_path,
            output_path=str(output_path),
            start=req.start,
            end=req.end,
            mode=req.mode,
            container=req.container,
            audio_mode=req.audio_mode,
            selected_tracks=req.selected_tracks or None,
            gains=gains or None,
        )

        # Write metadata sidecar
        meta = {
            "title": req.title,
            "game": req.game,
            "source": req.source_path,
            "start": req.start,
            "end": req.end,
            "mode": req.mode,
            "container": req.container,
            "audio_mode": req.audio_mode,
            "selected_tracks": req.selected_tracks,
            "gains": req.gains,
            "created": time.time(),
        }
        meta_path = output_path.with_suffix(output_path.suffix + ".json")
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        return {"ok": True, "path": result, "name": output_name}
    except Exception as e:
        raise HTTPException(500, f"Clip export failed: {e}")


@app.get("/api/clips")
async def list_clips():
    cfg = load_config()
    clips_dir = Path(cfg.get("clips_folder", str(CLIPS_DIR)))
    if not clips_dir.is_dir():
        return []

    clips = []
    for f in clips_dir.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            stat = f.stat()
            meta = {}
            meta_path = f.with_suffix(f.suffix + ".json")
            if meta_path.exists():
                try:
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                except Exception:
                    pass
            clips.append({
                "name": f.name,
                "path": str(f),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "meta": meta,
            })

    clips.sort(key=lambda c: c["modified"], reverse=True)
    return clips


@app.post("/api/clips/delete")
async def delete_clips(req: DeleteRequest):
    deleted = []
    errors = []
    for p in req.paths:
        try:
            fp = Path(p)
            if fp.exists():
                fp.unlink()
                # Also remove metadata sidecar
                meta = fp.with_suffix(fp.suffix + ".json")
                if meta.exists():
                    meta.unlink()
                deleted.append(p)
            else:
                errors.append({"path": p, "error": "Not found"})
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return {"deleted": deleted, "errors": errors}


@app.get("/api/clips/download")
async def download_clip(path: str = Query(...)):
    fp = Path(path)
    if not fp.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(fp), filename=fp.name)
