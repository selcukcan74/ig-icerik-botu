from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RENDER_DIR = DATA_DIR / "renders"
INSPIRATION_DIR = DATA_DIR / "inspiration"
TEMPLATE_DIR = ROOT / "templates"
DB_PATH = DATA_DIR / "igbot.sqlite3"

for d in (DATA_DIR, RENDER_DIR, INSPIRATION_DIR):
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def _load_yaml() -> Dict[str, Any]:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG: Dict[str, Any] = _load_yaml()
TZ = ZoneInfo(CFG.get("timezone", "Europe/Istanbul"))


def env(name: str, required: bool = True) -> str:
    val = os.getenv(name, "").strip()
    if required and not val:
        raise RuntimeError(f".env içinde {name} eksik")
    return val
