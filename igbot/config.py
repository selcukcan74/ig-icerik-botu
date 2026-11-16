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
CONFIG_PATH = ROOT / "config.yaml"

for d in (DATA_DIR, RENDER_DIR, INSPIRATION_DIR):
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def _load_yaml() -> Dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config.yaml bir sözlük (anahtar: değer) yapısı olmalı")
    return data


CFG: Dict[str, Any] = _load_yaml()
TZ = ZoneInfo(CFG.get("timezone", "Europe/Istanbul"))


_cfg_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0


def reload_config() -> Dict[str, Any]:
    """config.yaml'ı yeniden okur. CFG yerinde güncellenir; böylece `from .config import CFG`
    ile içe aktaran tüm modüller yeni değerleri görür. (Saat dilimi değişikliği yeniden başlatma ister.)"""
    global _cfg_mtime
    fresh = _load_yaml()
    CFG.clear()
    CFG.update(fresh)
    _cfg_mtime = CONFIG_PATH.stat().st_mtime
    return CFG


def reload_if_changed() -> bool:
    """Dosya diskte değiştiyse yeniden yükler. Bozuk YAML'da eski ayarlarla devam eder."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return False
    if mtime == _cfg_mtime:
        return False
    try:
        reload_config()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("config.yaml okunamadı; önceki ayarlarla devam ediliyor")
        return False
    return True


def env(name: str, required: bool = True) -> str:
    val = os.getenv(name, "").strip()
    if required and not val:
        raise RuntimeError(f".env içinde {name} eksik")
    return val
