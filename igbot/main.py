"""Giriş noktası: python -m igbot.main"""
from __future__ import annotations

import logging
import sys

from . import db
from .config import DATA_DIR


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(DATA_DIR / "igbot.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    # Yarıda kalmış tasarımları kurtar
    for d in db.drafts_by_status(db.DESIGNING):
        db.update_draft(d["id"], status=db.DESIGN_PENDING if d["images_json"] else db.IDEA_PENDING)

    from .bot import build_app

    app = build_app()
    logging.info("Bot çalışıyor. Durdurmak için Ctrl+C.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
