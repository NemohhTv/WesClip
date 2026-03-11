"""FFprobe / FFmpeg helpers for preview, thumbnail, clip, and remux operations."""

import asyncio
import hashlib
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path

from app.config import PREVIEW_DIR, THUMBNAILS_DIR

log = logging.getLogger("webclipper")

# Codecs that modern browsers can generally play inside MP4
BROWSER_VIDEO_CODECS = {"h264", "h265", "hevc", "vp8", "vp9", "av1"}
BROWSER_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac"}
BROWSER_CONTAINERS = {".mp4", ".webm", ".mov", ".m4v"}


# ── probe helpers ────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


async def _arun(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout.decode(), stderr.decode())


def probe(path: str) -> dict:
    """Return ffprobe JSON for a media file."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr[:500]}")
    return json.loads(r.stdout)


def get_duration(path: str) -> float:
    info = probe(path)
    fmt = info.get("format", {})
    dur = fmt.get("duration")
    if dur:
        return float(dur)
    for s in info.get("streams", []):
        if s.get("duration"):
            return float(s["duration"])
    return 0.0


def get_streams(path: str) -> list[dict]:
    info = probe(path)
    return info.get("streams", [])


def get_file_info(path: str) -> dict:
    info = probe(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    return {
        "path": path,
        "duration": float(fmt.get("duration", 0)),
        "size": int(fmt.get("size", 0)),
        "format": fmt.get("format_name", ""),
        "video_streams": [
            {
                "index": s["index"],
                "codec": s.get("codec_name", ""),
                "width": s.get("width", 0),
                "height": s.get("height", 0),
                "fps": _parse_fps(s.get("r_frame_rate", "0/1")),
            }
            for s in video_streams
        ],
        "audio_streams": [
            {
                "index": s["index"],
                "codec": s.get("codec_name", ""),
                "channels": s.get("channels", 0),
                "sample_rate": s.get("sample_rate", ""),
                "title": s.get("tags", {}).get("title", f"Track {s['index']}"),
            }
            for s in audio_streams
        ],
    }


def _parse_fps(rate: str) -> float:
    try:
        parts = rate.split("/")
        if len(parts) == 2 and int(parts[1]) != 0:
            return round(int(parts[0]) / int(parts[1]), 2)
        return float(parts[0])
    except Exception:
        return 0.0


# ── preview strategy ─────────────────────────────────────────────────────────

def _file_hash(path: str) -> str:
    stat = os.stat(path)
    raw = f"{path}:{stat.st_size}:{stat.st_mtime}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _preview_strategy(path: str) -> str:
    """Return 'direct', 'remux', or 'transcode'."""
    ext = Path(path).suffix.lower()
    try:
        streams = get_streams(path)
    except Exception:
        return "transcode"

    video_codec = ""
    audio_codec = ""
    for s in streams:
        if s.get("codec_type") == "video" and not video_codec:
            video_codec = s.get("codec_name", "").lower()
        if s.get("codec_type") == "audio" and not audio_codec:
            audio_codec = s.get("codec_name", "").lower()

    video_ok = video_codec in BROWSER_VIDEO_CODECS
    audio_ok = audio_codec in BROWSER_AUDIO_CODECS or not audio_codec
    container_ok = ext in BROWSER_CONTAINERS

    if video_ok and audio_ok and container_ok:
        return "direct"
    if video_ok and audio_ok and not container_ok:
        return "remux"
    return "transcode"


async def ensure_preview(path: str) -> tuple[str, str]:
    """Return (preview_file_path, strategy). Creates cached preview if needed."""
    strategy = _preview_strategy(path)

    if strategy == "direct":
        return path, "direct"

    h = _file_hash(path)
    cached = PREVIEW_DIR / f"{h}.mp4"
    if cached.exists():
        return str(cached), strategy

    if strategy == "remux":
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-c", "copy",
            "-movflags", "+faststart",
            str(cached),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-vf", "scale='min(1920,iw)':-2",
            str(cached),
        ]

    r = await _arun(cmd, timeout=600)
    if r.returncode != 0:
        # If remux failed, fall back to transcode
        if strategy == "remux":
            cmd = [
                "ffmpeg", "-y", "-i", path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-vf", "scale='min(1920,iw)':-2",
                str(cached),
            ]
            r = await _arun(cmd, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(f"Preview transcode failed: {r.stderr[:500]}")
            return str(cached), "transcode"
        raise RuntimeError(f"Preview generation failed: {r.stderr[:500]}")
    return str(cached), strategy


# ── thumbnails ───────────────────────────────────────────────────────────────

def _thumb_path(path: str) -> Path:
    h = _file_hash(path)
    return THUMBNAILS_DIR / f"{h}.jpg"


async def ensure_thumbnail(path: str) -> str:
    """Return path to a JPEG thumbnail, generating if needed."""
    out = _thumb_path(path)
    if out.exists():
        return str(out)

    dur = get_duration(path)
    seek = min(dur * 0.1, 10) if dur > 0 else 0

    cmd = [
        "ffmpeg", "-y", "-ss", str(seek), "-i", path,
        "-vframes", "1", "-q:v", "6",
        "-vf", "scale=480:-2",
        str(out),
    ]
    r = await _arun(cmd, timeout=30)
    if r.returncode != 0:
        # Try without seek
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-vframes", "1", "-q:v", "6",
            "-vf", "scale=480:-2",
            str(out),
        ]
        r = await _arun(cmd, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"Thumbnail failed: {r.stderr[:300]}")
    return str(out)


# ── clip export ──────────────────────────────────────────────────────────────

async def create_clip(
    source_path: str,
    output_path: str,
    start: float,
    end: float,
    mode: str = "fast",
    container: str = "mp4",
    audio_mode: str = "keep",
    selected_tracks: list[int] | None = None,
    gains: dict[int, float] | None = None,
) -> str:
    """Create a clip and return the output file path."""
    duration = end - start
    if duration <= 0:
        raise ValueError("End time must be after start time")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    if mode == "fast":
        cmd = _build_fast_clip_cmd(source_path, str(output), start, duration, selected_tracks)
    else:
        cmd = _build_accurate_clip_cmd(
            source_path, str(output), start, duration,
            audio_mode, selected_tracks, gains or {}
        )

    r = await _arun(cmd, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"Clip export failed: {r.stderr[:500]}")
    return str(output)


def _build_fast_clip_cmd(src, out, start, duration, tracks):
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-i", src,
        "-t", str(duration),
        "-c", "copy",
        "-movflags", "+faststart",
    ]
    if tracks:
        cmd += ["-map", "0:v:0"]
        for t in tracks:
            cmd += ["-map", f"0:{t}"]
    cmd.append(out)
    return cmd


def _build_accurate_clip_cmd(src, out, start, duration, audio_mode, tracks, gains):
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-i", src,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-movflags", "+faststart",
    ]

    if not tracks:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd.append(out)
        return cmd

    if audio_mode == "mix" and len(tracks) > 1:
        # Build amix filter
        filter_parts = []
        inputs = []
        for i, t in enumerate(tracks):
            gain = gains.get(t, 1.0)
            inputs.append(f"[0:{t}]volume={gain}[a{i}]")
        filter_parts.extend(inputs)
        mix_inputs = "".join(f"[a{i}]" for i in range(len(tracks)))
        filter_parts.append(f"{mix_inputs}amix=inputs={len(tracks)}:duration=longest[aout]")
        filter_str = ";".join(filter_parts)
        cmd += [
            "-filter_complex", filter_str,
            "-map", "0:v:0", "-map", "[aout]",
            "-c:a", "aac", "-b:a", "192k",
        ]
    else:
        cmd += ["-map", "0:v:0"]
        for t in tracks:
            gain = gains.get(t, 1.0)
            if gain != 1.0:
                cmd += ["-map", f"0:{t}"]
            else:
                cmd += ["-map", f"0:{t}"]
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        # Apply per-track gain via filter
        audio_filters = []
        for idx, t in enumerate(tracks):
            gain = gains.get(t, 1.0)
            if gain != 1.0:
                audio_filters.append(f"-filter:a:{idx}")
                audio_filters.append(f"volume={gain}")
        cmd.extend(audio_filters)

    cmd.append(out)
    return cmd


# ── remux ────────────────────────────────────────────────────────────────────

async def remux_file(source_path: str, output_path: str) -> str:
    """Remux MKV to MP4 via stream copy. Falls back to transcode on failure."""
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-map", "0:v", "-map", "0:a",
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    r = await _arun(cmd, timeout=1800)
    if r.returncode != 0:
        # Fallback: transcode
        cmd = [
            "ffmpeg", "-y", "-i", source_path,
            "-map", "0:v:0", "-map", "0:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]
        r = await _arun(cmd, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"Remux failed: {r.stderr[:500]}")
    return output_path
