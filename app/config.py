import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "config.json"
CLIPS_DIR = DATA_DIR / "clips"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
PREVIEW_DIR = DATA_DIR / "preview"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts"}

DEFAULT_CONFIG = {
    "sources": [],
    "auto_refresh": False,
    "refresh_interval": 30,
    "clips_folder": str(CLIPS_DIR),
}


def _ensure_dirs():
    for d in [DATA_DIR, CLIPS_DIR, THUMBNAILS_DIR, PREVIEW_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    _ensure_dirs()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    _ensure_dirs()
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
