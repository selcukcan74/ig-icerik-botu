"""Slayt içeriğini HTML şablonundan yerelde PNG'ye dönüştürür (Playwright/Chromium)."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from playwright.async_api import async_playwright

from .config import CFG, RENDER_DIR, TEMPLATE_DIR

_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "j2"]))

# Taşan metni küçülterek sığdıran script
FIT_JS = """
() => {
  const frame = document.querySelector('.frame');
  const h1 = document.querySelector('h1');
  const texts = [...document.querySelectorAll('.body, li')];
  const fits = () => frame.scrollHeight <= frame.clientHeight + 1;
  let guard = 0;
  while (!fits() && guard++ < 60) {
    const hs = parseFloat(getComputedStyle(h1).fontSize);
    if (hs > 56) h1.style.fontSize = (hs - 4) + 'px';
    texts.forEach(t => {
      const s = parseFloat(getComputedStyle(t).fontSize);
      if (s > 28) t.style.fontSize = (s - 1) + 'px';
    });
  }
}
"""


def _title_html(title: str, highlight: str) -> Markup:
    t = escape(title)
    if highlight and highlight.strip() and highlight in title:
        h = escape(highlight)
        return Markup(str(t).replace(str(h), f'<span class="hl">{h}</span>', 1))
    return t


def _title_size(title: str) -> int:
    n = len(title)
    if n <= 18:
        return 128
    if n <= 32:
        return 108
    if n <= 50:
        return 92
    return 78


def build_html(slides: List[Dict], index: int, tag_text: str = "Konsept tasarım") -> str:
    s = slides[index]
    d = CFG["design"]
    image_src = ""
    if s.get("image_path") and Path(s["image_path"]).exists():
        image_src = "data:image/png;base64," + base64.b64encode(Path(s["image_path"]).read_bytes()).decode()
    return _env.get_template("slide.html.j2").render(
        s=s,
        d=d,
        c=d["colors"],
        index=index,
        total=len(slides),
        title_html=_title_html(s["title"], s.get("highlight", "")),
        title_size=_title_size(s["title"]),
        brand_name=CFG["brand"]["name"],
        handle=CFG["brand"]["handle"],
        image_src=image_src,
        tag_text=tag_text,
    )


async def render_slides(draft_id: int, slides: List[Dict], tag_text: str = "Konsept tasarım") -> List[Path]:
    d = CFG["design"]
    out_dir = RENDER_DIR / f"draft_{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    paths: List[Path] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": d["width"], "height": d["height"]})
        for i in range(len(slides)):
            await page.set_content(build_html(slides, i, tag_text), wait_until="networkidle")
            await page.evaluate("document.fonts.ready")
            await page.evaluate(FIT_JS)
            path = out_dir / f"slide_{i + 1:02d}.png"
            await page.screenshot(path=str(path), type="png")
            paths.append(path)
        await browser.close()
    return paths
