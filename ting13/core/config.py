import json
import os
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path.home() / ".ting13"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "output": ".",
    "proxy": None,
    "rotate": 0,
    "headless": True,
    "download_workers": 4,
    "url_fetch_workers": 5,
    "delay": 0.0,
    "fast": False,
    "batch": False,
    "proxy_pool_url": None,
    "proxy_pool_key": None,
}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)

    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save_config(settings: dict[str, Any]) -> None:
    ensure_config_dir()
    cleaned = {}
    for key, value in settings.items():
        if key in DEFAULTS and value is not None and value != DEFAULTS.get(key):
            cleaned[key] = value
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2, default=str)


def get(key: str, default: Any = None) -> Any:
    cfg = load_config()
    return cfg.get(key, default)