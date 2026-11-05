"""Tasarım örneği görselleri (OpenAI görsel modeli). Aynı tarif tekrar gelirse önbellekten döner."""
from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
from typing import Optional

import openai

from .config import CFG, DATA_DIR
from .llm import client

log = logging.getLogger(__name__)
IMG_DIR = DATA_DIR / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def full_prompt(prompt: str, brief: str = "", brand_name: str = "") -> str:
    v = CFG["visuals"]
    rules = v["rules"]
    if brand_name:
        # Müşteri çalışması: hayali isim kuralı yerine verilen marka adı
        rules = (
            f'Create an ORIGINAL design for the brand "{brand_name}" and spell the brand name exactly as written, '
            "including any special characters. Do not reproduce other companies' logos or trademarks. "
            "Keep any other in-image text minimal and spelled exactly. No watermarks, no UI chrome."
        )
    head = f"Brand brief: {brief.strip()}\n\n" if brief.strip() else ""
    framing = "Framing: keep the logo and key subject inside the upper-middle area; leave the bottom 20% calm and uncluttered."
    return f"{head}{prompt.strip()}\n{framing}\n\nStyle: {v['style']}\n\nRules: {rules}"


def generate(prompt: str, brief: str = "", reference: Optional[Path] = None, brand_name: str = "") -> Path:
    """reference verilirse o görsel (ör. projenin logo sayfası) marka referansı olarak kullanılır."""
    v = CFG["visuals"]
    fp = full_prompt(prompt, brief, brand_name)
    if reference:
        fp = (
            "Use the attached image as the brand reference. Keep the exact same logo, brand name spelling, "
            "colour palette, typography and graphic language, applied to the new item described below.\n\n" + fp
        )
    ref_key = reference.name if reference else ""
    key = hashlib.sha1(f"{v['model']}|{v['size']}|{v['quality']}|{ref_key}|{fp}".encode()).hexdigest()[:16]
    out = IMG_DIR / f"{key}.png"
    if out.exists():
        return out
    log.info("Görsel üretiliyor (%s%s): %s", v["model"], ", referanslı" if reference else "", prompt[:120])
    common = dict(model=v["model"], prompt=fp, size=v["size"], quality=v["quality"], n=1)
    if reference:
        try:
            with open(reference, "rb") as f:
                r = client().images.edit(image=f, input_fidelity="high", **common)
        except openai.BadRequestError as ex:
            if "input_fidelity" not in str(ex):
                raise
            with open(reference, "rb") as f:
                r = client().images.edit(image=f, **common)
    else:
        r = client().images.generate(**common)
    out.write_bytes(base64.b64decode(r.data[0].b64_json))
    return out
