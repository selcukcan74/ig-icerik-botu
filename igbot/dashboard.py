"""Yerel yönetim paneli: python -m igbot.dashboard

Güvenlik:
- Varsayılan olarak yalnızca bu bilgisayardan erişilir (127.0.0.1).
- Her sayfa DASHBOARD_PASSWORD ile korunur (yoksa ilk açılışta üretilip .env'ye yazılır).
- Tüm POST istekleri CSRF jetonu ister.
- API anahtarları hiçbir zaman sayfaya basılmaz; yalnızca son 4 karakter gösterilir.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import db, slots
from .config import CONFIG_PATH, DATA_DIR, ROOT, TZ, reload_config

log = logging.getLogger(__name__)
ENV_PATH = ROOT / ".env"

# Panelden düzenlenebilen gizli anahtarlar (değerleri asla gösterilmez)
SECRET_KEYS: List[Tuple[str, str, str]] = [
    ("OPENAI_API_KEY", "OpenAI", "Metin ve görsel üretimi"),
    ("APIFY_TOKEN", "Apify", "İlham hesaplarını tarama"),
    ("TELEGRAM_BOT_TOKEN", "Telegram bot", "Onay botu bağlantısı"),
    ("TELEGRAM_CHAT_ID", "Telegram sohbet", "Onayların gideceği sohbet"),
    ("ZERNIO_API_KEY", "Zernio", "Planlama ve yayın"),
    ("ZERNIO_ACCOUNT_ID", "Zernio hesap", "Hedef Instagram hesabı"),
]
STATUS_LABELS = {
    db.IDEA_PENDING: ("Fikir onayı", "warn"),
    db.DESIGNING: ("Tasarlanıyor", "info"),
    db.DESIGN_PENDING: ("Paylaşım onayı", "warn"),
    db.PUBLISHING: ("Gönderiliyor", "info"),
    db.SCHEDULED: ("Planlandı", "accent"),
    db.PUBLISHED: ("Yayında", "ok"),
    db.REJECTED: ("Reddedildi", "muted"),
    db.FAILED: ("Hata", "danger"),
}
FORMAT_LABELS = {"carousel": "Carousel", "single": "Tek görsel", "portfolio": "Portföy"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
HEARTBEAT_STALE_SEC = 180


class FormError(ValueError):
    pass


# ---------------- .env ----------------
def read_env_file() -> Dict[str, str]:
    """Yalnızca .env dosyasını okur (işlem ortam değişkenlerini KARIŞTIRMAZ)."""
    out: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def write_env_values(values: Dict[str, str]) -> None:
    """Var olan satırları, yorumları ve sırayı koruyarak yalnızca verilen anahtarları günceller."""
    for k, v in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k) or "\n" in v or "\r" in v:
            raise FormError(f"Geçersiz değer: {k}")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pending = dict(values)
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key = s.split("=", 1)[0].strip()
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"{k}={v}" for k, v in pending.items()]
    _atomic_write(ENV_PATH, "\n".join(lines) + "\n", mode=0o600)
    for k, v in values.items():
        os.environ[k] = v


def _atomic_write(path: Path, text: str, mode: Optional[int] = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _mask(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def ensure_dashboard_secrets() -> Dict[str, str]:
    """Şifre ve oturum anahtarı yoksa üretip .env'ye yazar."""
    env = read_env_file()
    new: Dict[str, str] = {}
    if not (os.getenv("DASHBOARD_PASSWORD") or env.get("DASHBOARD_PASSWORD")):
        new["DASHBOARD_PASSWORD"] = secrets.token_urlsafe(12)
        log.warning("DASHBOARD_PASSWORD üretildi ve .env dosyasına yazıldı.")
    if not (os.getenv("DASHBOARD_SECRET_KEY") or env.get("DASHBOARD_SECRET_KEY")):
        new["DASHBOARD_SECRET_KEY"] = secrets.token_hex(32)
    if new:
        write_env_values(new)
        env.update(new)
    return {
        "password": os.getenv("DASHBOARD_PASSWORD") or env["DASHBOARD_PASSWORD"],
        "secret": os.getenv("DASHBOARD_SECRET_KEY") or env["DASHBOARD_SECRET_KEY"],
    }


# ---------------- config.yaml ----------------
def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_config(data: Dict[str, Any]) -> None:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110)
    yaml.safe_load(text)  # geri okunabildiğinden emin ol
    _atomic_write(CONFIG_PATH, text)
    reload_config()


# ---------------- form yardımcıları ----------------
def f_text(name: str, label: str, required: bool = False, max_len: int = 2000) -> str:
    v = (request.form.get(name) or "").strip()
    if required and not v:
        raise FormError(f"“{label}” boş bırakılamaz.")
    if len(v) > max_len:
        raise FormError(f"“{label}” en fazla {max_len} karakter olabilir.")
    return v


def f_int(name: str, label: str, lo: int, hi: int) -> int:
    raw = (request.form.get(name) or "").strip()
    try:
        v = int(raw)
    except ValueError:
        raise FormError(f"“{label}” bir tam sayı olmalı.")
    if not lo <= v <= hi:
        raise FormError(f"“{label}” {lo} ile {hi} arasında olmalı.")
    return v


def f_float(name: str, label: str, lo: float, hi: float) -> float:
    raw = (request.form.get(name) or "").strip().replace(",", ".")
    try:
        v = float(raw)
    except ValueError:
        raise FormError(f"“{label}” bir sayı olmalı.")
    if not lo <= v <= hi:
        raise FormError(f"“{label}” {lo} ile {hi} arasında olmalı.")
    return v


def f_lines(name: str) -> List[str]:
    return [x.strip() for x in (request.form.get(name) or "").splitlines() if x.strip()]


def f_time(name: str, label: str) -> str:
    v = (request.form.get(name) or "").strip()
    if not TIME_RE.match(v):
        raise FormError(f"“{label}” SS:DD biçiminde olmalı (ör. 12:30).")
    return v


# ---------------- veri ----------------
def _all_drafts(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    q = "SELECT * FROM drafts"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db.conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_draft_view(r) for r in rows]


def _draft_view(r) -> Dict[str, Any]:
    idea = json.loads(r["idea_json"] or "{}")
    images = json.loads(r["images_json"]) if r["images_json"] else []
    when = None
    if r["scheduled_for"]:
        try:
            when = datetime.fromisoformat(r["scheduled_for"]).astimezone(TZ)
        except ValueError:
            pass
    label, tone = STATUS_LABELS.get(r["status"], (r["status"], "muted"))
    client = idea.get("client") or {}
    return {
        "id": r["id"],
        "title": idea.get("title", "(başlıksız)"),
        "hook": idea.get("hook", ""),
        "format": FORMAT_LABELS.get(idea.get("format"), idea.get("format", "")),
        "pillar": idea.get("pillar", ""),
        "own": bool(client.get("own")),
        "client": client.get("name", "") if client and not client.get("own") else "",
        "status": r["status"],
        "status_label": label,
        "tone": tone,
        "when": when,
        "when_label": slots.fmt(when) if when else "",
        "overdue": bool(when and r["status"] == db.SCHEDULED and when < datetime.now(TZ)),
        "image_count": len(images),
        "has_image": bool(images),
        "caption": r["caption"] or "",
        "permalink": r["permalink"] or "",
        "error": r["error"] or "",
        "created": (r["created_at"] or "")[:16].replace("T", " "),
    }


def _status_counts() -> Dict[str, int]:
    with db.conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM drafts GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def _inspiration_stats() -> Dict[str, Any]:
    with db.conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT owner) owners, MAX(scraped_at) last FROM inspiration"
        ).fetchone()
        top = c.execute(
            """SELECT owner, url, type, likes, comments_count, substr(caption,1,140) caption FROM inspiration
               ORDER BY (COALESCE(likes,0) + 4*COALESCE(comments_count,0)) DESC LIMIT 6"""
        ).fetchall()
    last = r["last"]
    return {
        "total": r["n"],
        "owners": r["owners"],
        "last": (last or "")[:16].replace("T", " "),
        "top": [dict(x) for x in top],
    }


def _bot_status() -> Dict[str, Any]:
    hb = db.kv_get("bot_heartbeat")
    if not hb:
        return {"ok": None, "label": "Bilinmiyor", "detail": "Bot henüz sinyal göndermedi (yeni sürümle yeniden başlatılmalı)."}
    try:
        age = (datetime.now(TZ) - datetime.fromisoformat(hb)).total_seconds()
    except ValueError:
        return {"ok": None, "label": "Bilinmiyor", "detail": ""}
    if age <= HEARTBEAT_STALE_SEC:
        return {"ok": True, "label": "Çalışıyor", "detail": f"Son sinyal {int(age)} sn önce"}
    mins = int(age // 60)
    return {"ok": False, "label": "Yanıt yok", "detail": f"Son sinyal {mins} dk önce"}


def _tail_log(path: Path, count: int = 80) -> List[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 64_000))
        data = fh.read().decode("utf-8", errors="replace")
    return data.splitlines()[-count:]


# ---------------- uygulama ----------------
def create_app(testing: bool = False) -> Flask:
    db.init()
    creds = {"password": "test", "secret": "test-secret"} if testing else ensure_dashboard_secrets()
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.config.update(
        SECRET_KEY=creds["secret"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
        TESTING=testing,
    )
    app.config["DASHBOARD_PASSWORD"] = creds["password"]

    # ---- kimlik doğrulama + CSRF ----
    @app.before_request
    def guard():
        if request.endpoint in ("login", "static"):
            return None
        if not session.get("auth"):
            return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
        if request.method == "POST":
            token = request.form.get("_csrf", "")
            if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                abort(400, "Oturum jetonu geçersiz; sayfayı yenileyip tekrar deneyin.")
        return None

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        cfg = load_config() if session.get("auth") else {}
        return {
            "csrf_token": session["csrf"],
            "brand_name": (cfg.get("brand") or {}).get("name", "İçerik Botu"),
            "brand_handle": (cfg.get("brand") or {}).get("handle", ""),
            "nav_active": request.endpoint,
            "bot": _bot_status() if session.get("auth") else None,
        }

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", ""), app.config["DASHBOARD_PASSWORD"]):
                session.clear()
                session.permanent = True
                session["auth"] = True
                session["csrf"] = secrets.token_urlsafe(24)
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
            time.sleep(1.0)  # kaba kuvvet denemelerini yavaşlat
            error = "Şifre hatalı."
        return render_template("dashboard/login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- sayfalar ----
    @app.route("/")
    def index():
        counts = _status_counts()
        drafts = _all_drafts(limit=60)
        upcoming = sorted(
            [d for d in drafts if d["status"] == db.SCHEDULED and d["when"]], key=lambda d: d["when"]
        )
        waiting = [d for d in drafts if d["status"] in (db.IDEA_PENDING, db.DESIGN_PENDING)]
        published = [d for d in drafts if d["status"] == db.PUBLISHED][:6]
        problems = [d for d in drafts if d["status"] == db.FAILED or d["overdue"]]
        cfg = load_config()
        max_q = float((cfg.get("posting") or {}).get("max_queue_days", 3))
        qd = slots.queue_days()
        return render_template(
            "dashboard/index.html",
            counts=counts,
            upcoming=upcoming,
            waiting=waiting,
            published=published,
            problems=problems,
            queue_days=qd,
            queue_max=max_q,
            queue_pct=min(100, int(qd / max_q * 100)) if max_q else 0,
            free_slots=[slots.fmt(s) for s in slots.upcoming_free_slots(4)],
            insp=_inspiration_stats(),
            run_at=(cfg.get("ideation") or {}).get("run_at", ""),
        )

    @app.route("/dashboard")
    def dashboard():
        return redirect(url_for("index"))

    @app.route("/content")
    def content():
        status = request.args.get("status") or None
        if status and status not in STATUS_LABELS:
            status = None
        return render_template(
            "dashboard/content.html",
            drafts=_all_drafts(status),
            counts=_status_counts(),
            statuses=STATUS_LABELS,
            current=status,
        )

    @app.route("/media/<int:draft_id>/<int:idx>")
    def media(draft_id: int, idx: int):
        d = db.get_draft(draft_id)
        if not d or not d["images_json"]:
            abort(404)
        images = json.loads(d["images_json"])
        if not 0 <= idx < len(images):
            abort(404)
        p = Path(images[idx]).resolve()
        if DATA_DIR.resolve() not in p.parents or not p.exists():
            abort(404)
        resp = send_file(p, mimetype="image/png", max_age=3600)
        return resp

    @app.route("/dashboard/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            try:
                cfg = load_config()
                b = cfg.setdefault("brand", {})
                b["name"] = f_text("brand_name", "Marka adı", required=True, max_len=80)
                b["handle"] = f_text("brand_handle", "Instagram kullanıcı adı", required=True, max_len=60)
                b["niche"] = f_text("brand_niche", "Alan", required=True)
                b["positioning"] = f_text("brand_positioning", "Konumlandırma")
                b["slogan"] = f_text("brand_slogan", "Slogan", max_len=160)
                b["audience"] = f_text("brand_audience", "Hedef kitle", required=True)
                b["tone"] = f_text("brand_tone", "Ton", required=True)
                b["goal"] = f_text("brand_goal", "Hedef", required=True)
                b["cta_default"] = f_text("brand_cta", "Varsayılan CTA", required=True, max_len=300)
                b["avoid"] = f_lines("brand_avoid")
                for k in ("positioning", "slogan"):
                    if not b[k]:
                        b.pop(k)
                save_config(cfg)
                flash("Marka ayarları kaydedildi. Bot 1 dakika içinde yeni ayarlarla çalışır.", "ok")
            except FormError as ex:
                flash(str(ex), "danger")
            return redirect(url_for("settings"))
        return render_template("dashboard/settings.html", cfg=load_config())

    @app.route("/dashboard/schedule", methods=["GET", "POST"])
    def schedule():
        if request.method == "POST":
            try:
                cfg = load_config()
                p = cfg.setdefault("posting", {})
                i = cfg.setdefault("ideation", {})
                slot_list = f_lines("slots")
                if not slot_list:
                    raise FormError("En az bir yayın saati girin.")
                for s in slot_list:
                    if not TIME_RE.match(s):
                        raise FormError(f"Yayın saati “{s}” SS:DD biçiminde olmalı.")
                if len(set(slot_list)) != len(slot_list):
                    raise FormError("Aynı yayın saati iki kez yazılmış.")
                ppd = f_int("posts_per_day", "Günlük gönderi", 1, 6)
                if ppd > len(slot_list):
                    raise FormError(f"Günde {ppd} gönderi için en az {ppd} yayın saati gerekir.")
                p["posts_per_day"] = ppd
                p["slots"] = sorted(slot_list)
                p["max_queue_days"] = f_int("max_queue_days", "Kuyruk sınırı", 1, 30)
                i["run_at"] = f_time("run_at", "Fikir üretim saati")
                i["ideas_per_run"] = f_int("ideas_per_run", "Tur başına fikir", 1, 6)
                i["carousel_ratio"] = round(f_float("carousel_ratio", "Carousel oranı", 0, 1), 2)
                i["max_carousel_slides"] = f_int("max_carousel_slides", "Carousel slayt sınırı", 3, 10)
                i["portfolio_per_week"] = f_int("portfolio_per_week", "Haftalık portföy", 0, 14)
                save_config(cfg)
                flash("Yayın ve üretim ayarları kaydedildi. Bot 1 dakika içinde uygular.", "ok")
            except FormError as ex:
                flash(str(ex), "danger")
            return redirect(url_for("schedule"))
        return render_template("dashboard/schedule.html", cfg=load_config())

    @app.route("/dashboard/inspiration", methods=["GET", "POST"])
    def inspiration():
        if request.method == "POST":
            try:
                cfg = load_config()
                ins = cfg.setdefault("inspiration", {})
                accounts = [a.lstrip("@").strip() for a in f_lines("accounts")]
                bad = [a for a in accounts if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", a)]
                if bad:
                    raise FormError(f"Geçersiz kullanıcı adı: {', '.join(bad[:3])}")
                ins["accounts"] = list(dict.fromkeys(accounts))
                ins["research_categories"] = f_lines("research_categories")
                ins["posts_per_account"] = f_int("posts_per_account", "Hesap başına gönderi", 1, 50)
                ins["top_posts_for_llm"] = f_int("top_posts_for_llm", "LLM'e giden gönderi", 1, 40)
                ins["scrape_every_days"] = f_int("scrape_every_days", "Tarama sıklığı", 1, 30)
                save_config(cfg)
                flash("İlham ayarları kaydedildi.", "ok")
            except FormError as ex:
                flash(str(ex), "danger")
            return redirect(url_for("inspiration"))
        return render_template(
            "dashboard/inspiration.html",
            cfg=load_config(),
            manual_refs=db.list_manual_references(50),
            insp=_inspiration_stats(),
        )

    @app.route("/dashboard/inspiration/reference", methods=["POST"])
    def add_reference():
        url = f_text("url", "Bağlantı", max_len=500)
        if not re.match(r"^https?://", url):
            flash("Bağlantı http:// ya da https:// ile başlamalı.", "danger")
        else:
            db.add_manual_reference(
                f_text("source", "Kaynak", max_len=40) or "manuel",
                url,
                f_text("title", "Başlık", max_len=200),
                f_text("notes", "Not", max_len=1000),
            )
            flash("Referans eklendi; bir sonraki fikir turunda dikkate alınacak.", "ok")
        return redirect(url_for("inspiration"))

    @app.route("/dashboard/inspiration/reference/<int:ref_id>/delete", methods=["POST"])
    def delete_reference(ref_id: int):
        db.delete_manual_reference(ref_id)
        flash("Referans silindi.", "ok")
        return redirect(url_for("inspiration"))

    @app.route("/dashboard/prompts", methods=["GET", "POST"])
    def prompts():
        if request.method == "POST":
            try:
                cfg = load_config()
                v = cfg.setdefault("visuals", {})
                v["enabled"] = request.form.get("visuals_enabled") == "on"
                quality = request.form.get("visual_quality", "medium")
                if quality not in ("low", "medium", "high"):
                    raise FormError("Görsel kalitesi low, medium ya da high olmalı.")
                v["quality"] = quality
                v["max_per_post"] = f_int("max_per_post", "Gönderi başına görsel", 0, 6)
                v["max_per_portfolio"] = f_int("max_per_portfolio", "Portföy başına görsel", 0, 8)
                v["style"] = f_text("visual_style", "Görsel stil", required=True, max_len=3000)
                v["rules"] = f_text("visual_rules", "Görsel kurallar", required=True, max_len=3000)
                l = cfg.setdefault("llm", {})
                l["model"] = f_text("llm_model", "Model", required=True, max_len=60)
                effort = request.form.get("llm_reasoning_effort", "")
                if effort not in ("", "minimal", "low", "medium", "high"):
                    raise FormError("Düşünme seviyesi geçersiz.")
                if effort:
                    l["reasoning_effort"] = effort
                else:
                    l.pop("reasoning_effort", None)
                save_config(cfg)
                flash("Görsel ve model ayarları kaydedildi.", "ok")
            except FormError as ex:
                flash(str(ex), "danger")
            return redirect(url_for("prompts"))
        return render_template("dashboard/prompts.html", cfg=load_config())

    @app.route("/dashboard/integrations", methods=["GET", "POST"])
    def integrations():
        if request.method == "POST":
            payload = {}
            for key, _, _ in SECRET_KEYS:
                v = (request.form.get(key) or "").strip()
                if v:
                    payload[key] = v
            pw = (request.form.get("DASHBOARD_PASSWORD") or "").strip()
            if pw:
                if len(pw) < 10:
                    flash("Panel şifresi en az 10 karakter olmalı.", "danger")
                    return redirect(url_for("integrations"))
                payload["DASHBOARD_PASSWORD"] = pw
            if not payload:
                flash("Değiştirilecek bir alan girilmedi.", "warn")
                return redirect(url_for("integrations"))
            try:
                write_env_values(payload)
            except FormError as ex:
                flash(str(ex), "danger")
                return redirect(url_for("integrations"))
            if "DASHBOARD_PASSWORD" in payload:
                app.config["DASHBOARD_PASSWORD"] = pw
            names = ", ".join(sorted(payload))
            flash(f"Güncellendi: {names}. Botun yeni anahtarları kullanması için yeniden başlatılması gerekir.", "ok")
            return redirect(url_for("integrations"))
        env = read_env_file()
        items = [
            {"key": k, "name": n, "desc": d, "set": bool(env.get(k)), "masked": _mask(env.get(k, ""))}
            for k, n, d in SECRET_KEYS
        ]
        return render_template("dashboard/integrations.html", items=items)

    @app.route("/dashboard/ops")
    def ops():
        return render_template(
            "dashboard/ops.html",
            counts=_status_counts(),
            queue_days=slots.queue_days(),
            insp=_inspiration_stats(),
            log_lines=_tail_log(DATA_DIR / "igbot.log"),
        )

    @app.errorhandler(400)
    def bad_request(ex):
        return render_template("dashboard/error.html", code=400, message=getattr(ex, "description", "")), 400

    @app.errorhandler(404)
    def not_found(ex):
        return render_template("dashboard/error.html", code=404, message="Sayfa bulunamadı."), 404

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = read_env_file()
    host = os.getenv("DASHBOARD_HOST") or env.get("DASHBOARD_HOST") or "127.0.0.1"
    port = int(os.getenv("DASHBOARD_PORT") or env.get("DASHBOARD_PORT") or "5001")
    app = create_app()
    if host not in ("127.0.0.1", "localhost"):
        log.warning("Panel %s adresinde ağa açık; yalnızca güvendiğiniz ağlarda kullanın.", host)
    log.info("Panel: http://%s:%s", "127.0.0.1" if host == "0.0.0.0" else host, port)
    from waitress import serve  # üretim için tek süreçli, güvenli WSGI sunucusu

    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
