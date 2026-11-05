"""Zernio API: medya yükleme + Instagram gönderisi oluşturma/zamanlama."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .config import CFG, env

log = logging.getLogger(__name__)
BASE = "https://zernio.com/api/v1"


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {env('ZERNIO_API_KEY')}", "Content-Type": "application/json"}


def upload_image(path: Path) -> str:
    """presign → PUT → publicUrl"""
    ctype = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    with httpx.Client(timeout=60) as c:
        r = c.post(
            f"{BASE}/media/presign",
            headers=_headers(),
            json={"filename": path.name, "contentType": ctype, "size": path.stat().st_size},
        )
        r.raise_for_status()
        pre = r.json()
        put = c.put(pre["uploadUrl"], content=path.read_bytes(), headers={"Content-Type": ctype})
        put.raise_for_status()
    return pre["publicUrl"]


def create_post(
    caption: str,
    image_paths: List[Path],
    alt_texts: Optional[List[str]] = None,
    scheduled_for: Optional[datetime] = None,
) -> Dict[str, Any]:
    """scheduled_for None ise hemen yayınlar; aksi halde Zernio tarafında zamanlar
    (yani planlanan saatte Mac'in açık olması gerekmez)."""
    alt_texts = alt_texts or []
    media = []
    for i, p in enumerate(image_paths):
        item = {"type": "image", "url": upload_image(p)}
        if i < len(alt_texts) and alt_texts[i]:
            item["altText"] = alt_texts[i][:1000]
        media.append(item)

    body: Dict[str, Any] = {
        "content": caption,
        "mediaItems": media,
        "platforms": [{"platform": "instagram", "accountId": env("ZERNIO_ACCOUNT_ID")}],
    }
    if scheduled_for is None:
        body["publishNow"] = True
    else:
        body["scheduledFor"] = scheduled_for.replace(tzinfo=None).isoformat(timespec="seconds")
        body["timezone"] = CFG.get("timezone", "Europe/Istanbul")

    r = httpx.post(f"{BASE}/posts", headers=_headers(), json=body, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Zernio {r.status_code}: {r.text[:500]}")
    data = r.json()
    log.info("Zernio yanıtı: %s", str(data)[:400])
    return data


def post_info(resp: Dict[str, Any]) -> Dict[str, Optional[str]]:
    post = resp.get("post", resp)
    url = None
    for p in post.get("platforms") or []:
        url = p.get("platformPostUrl") or url
    return {"id": post.get("_id"), "status": post.get("status"), "url": url}
