"""Telegram onay botu + günlük iş akışı."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional

from telegram import (
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as KB,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import apify, db, imagegen, llm, render, slots, zernio
from .config import CFG, TZ, env

log = logging.getLogger(__name__)
IG_URL = re.compile(r"https?://(www\.)?instagram\.com/(p|reel|reels)/[\w-]+/?\S*")
e = html.escape


def chat_id() -> Optional[int]:
    v = env("TELEGRAM_CHAT_ID", required=False)
    return int(v) if v else None


# ============ Mesaj biçimleri ============
def idea_text(draft_id: int, idea: Dict) -> str:
    fmt = {"carousel": "🎠 Carousel", "single": "🖼 Tek görsel", "portfolio": "🗂 Portföy · konsept proje"}.get(
        idea["format"], idea["format"]
    )
    points = "\n".join(f"  • {e(p)}" for p in idea["key_points"])
    return (
        f"💡 <b>Taslak #{draft_id}</b> · {fmt}\n"
        f"<i>{e(idea['pillar'])}</i>\n\n"
        f"<b>{e(idea['title'])}</b>\n\n"
        f"🪝 <b>Hook:</b> {e(idea['hook'])}\n"
        f"🎯 <b>Açı:</b> {e(idea['angle'])}\n\n"
        f"<b>Ana noktalar:</b>\n{points}\n\n"
        + (f"🖼 <b>Tasarım örnekleri:</b> {e(idea['visual_concept'])}\n\n" if idea.get("visual_concept") else "")
        + f"📈 {e(idea['why_it_works'])}\n\n"
        f"<i>Değişiklik istersen bu mesajı yanıtlayarak yaz.</i>"
    )


def idea_kb(draft_id: int) -> KB:
    return KB([[
        B("✅ Onayla", callback_data=f"ia:{draft_id}"),
        B("🔁 Yeniden yaz", callback_data=f"ir:{draft_id}"),
        B("❌ Reddet", callback_data=f"ix:{draft_id}"),
    ]])


def design_kb(draft_id: int) -> KB:
    nxt = slots.next_free_slot()
    nxt_label = f"⏰ {slots.fmt(nxt)}" if nxt else "⏰ Slot yok"
    return KB([
        [B(nxt_label, callback_data=f"ps:{draft_id}"), B("🕐 Başka saat", callback_data=f"pt:{draft_id}")],
        [B("🚀 Şimdi paylaş", callback_data=f"pn:{draft_id}")],
        [B("✏️ Revize et", callback_data=f"pe:{draft_id}"), B("🎨 Yeniden üret", callback_data=f"pr:{draft_id}")],
        [B("❌ İptal", callback_data=f"px:{draft_id}")],
    ])


# ============ Ortak işlemler ============
async def send_idea(app: Application, draft_id: int, idea: Dict) -> None:
    msg = await app.bot.send_message(
        chat_id(), idea_text(draft_id, idea), parse_mode=ParseMode.HTML, reply_markup=idea_kb(draft_id)
    )
    db.link_message(msg.message_id, draft_id)


async def run_ideation(app: Application, n: Optional[int] = None, force: bool = False) -> int:
    icfg = CFG["ideation"]
    if not force:
        q = slots.queue_days()
        max_q = CFG["posting"].get("max_queue_days", 3)
        if q >= max_q:
            log.info("Kuyruk dolu (%.1f gün), fikir üretilmedi", q)
            await app.bot.send_message(
                chat_id(), f"📦 Kuyruk dolu ({q:.1f} günlük içerik var). Bugün yeni taslak üretmedim."
            )
            return 0
    try:
        new = await asyncio.to_thread(apify.scrape_if_due)
        if new:
            log.info("Yeni ilham: %d", new)
    except Exception as ex:
        log.exception("Apify hatası")
        await app.bot.send_message(chat_id(), f"⚠️ İlham taraması başarısız: {e(str(ex))[:300]}\nMevcut veriyle devam ediyorum.")

    n = n or int(icfg.get("ideas_per_run", 3))
    await app.bot.send_message(chat_id(), f"🧠 {n} taslak fikir hazırlanıyor…")
    target = int(icfg.get("portfolio_per_week", 0))
    portfolio_n = 1 if target and db.portfolio_count(7) < target else 0
    ideas = await asyncio.to_thread(llm.generate_ideas, n, None, None, portfolio_n)
    for idea in ideas:
        did = db.create_draft(idea)
        await send_idea(app, did, idea)
    return len(ideas)


async def design_draft(app: Application, draft_id: int, feedback: Optional[str] = None) -> None:
    d = db.get_draft(draft_id)
    idea = json.loads(d["idea_json"])
    prev = json.loads(d["content_json"]) if d["content_json"] else None
    db.update_draft(draft_id, status=db.DESIGNING)
    status_msg = await app.bot.send_message(chat_id(), f"🎨 Taslak #{draft_id} tasarlanıyor…")
    try:
        content = await asyncio.to_thread(llm.generate_content, idea, feedback, prev)
        await generate_showcase_images(content, status_msg)
        tag = "Konsept proje" if content.get("portfolio") else "Konsept tasarım"
        images = await render.render_slides(draft_id, content["slides"], tag)
        caption = llm.build_caption(content)
        db.update_draft(
            draft_id,
            status=db.DESIGN_PENDING,
            content_json=content,
            images_json=[str(p) for p in images],
            caption=caption,
        )
    except Exception as ex:
        log.exception("Tasarım hatası")
        db.update_draft(draft_id, status=db.FAILED, error=str(ex))
        await status_msg.edit_text(f"⚠️ Taslak #{draft_id} tasarlanamadı: {str(ex)[:300]}")
        return
    await status_msg.delete()
    await send_design(app, draft_id)


async def generate_showcase_images(content: Dict, status_msg) -> None:
    """showcase slaytları için tasarım örneklerini paralel üretir; başarısız olanı metin slaytına çevirir."""
    show = [s for s in content["slides"] if s["kind"] == "showcase" and s.get("image_prompt")]
    if not show:
        return
    try:
        await status_msg.edit_text(f"🖼 {len(show)} tasarım örneği üretiliyor (1-2 dk)…")
    except Exception:
        pass
    brief = content.get("brand_brief", "")
    brand = content.get("client_name", "")
    if content.get("portfolio"):
        # önce ana görsel (logo sayfası / grid), sonra diğerleri onu referans alarak → tutarlı proje
        try:
            anchor = await asyncio.to_thread(imagegen.generate, show[0]["image_prompt"], brief, None, brand)
            first: list = [anchor]
        except Exception as ex:
            anchor, first = None, [ex]
        rest = await asyncio.gather(
            *[asyncio.to_thread(imagegen.generate, s["image_prompt"], brief, anchor, brand) for s in show[1:]],
            return_exceptions=True,
        )
        results = first + list(rest)
    else:
        results = await asyncio.gather(
            *[asyncio.to_thread(imagegen.generate, s["image_prompt"], brief, None, brand) for s in show],
            return_exceptions=True,
        )
    failed = 0
    for s, r in zip(show, results):
        if isinstance(r, Exception):
            log.warning("Görsel üretilemedi: %s", r)
            s["kind"], s["image_path"] = "content", ""
            failed += 1
        else:
            s["image_path"] = str(r)
    if failed:
        await status_msg.reply_text(f"⚠️ {failed} tasarım örneği üretilemedi, o slaytlar metin olarak kaldı.")


async def send_design(app: Application, draft_id: int) -> None:
    d = db.get_draft(draft_id)
    images = [Path(p) for p in json.loads(d["images_json"])]
    media = [InputMediaPhoto(p.read_bytes()) for p in images[:10]]
    if len(media) == 1:
        m = await app.bot.send_photo(chat_id(), media[0].media)
        db.link_message(m.message_id, draft_id)
    else:
        msgs = await app.bot.send_media_group(chat_id(), media)
        for m in msgs:
            db.link_message(m.message_id, draft_id)
    text = (
        f"📝 <b>Taslak #{draft_id} — paylaşım onayı</b> ({len(images)} görsel)\n\n"
        f"{e(d['caption'])[:3500]}\n\n"
        f"<i>Revize için bu mesajı yanıtla (ör. “3. slaytı kısalt”, “açıklamayı daha samimi yap”).</i>"
    )
    m = await app.bot.send_message(chat_id(), text, parse_mode=ParseMode.HTML, reply_markup=design_kb(draft_id))
    db.link_message(m.message_id, draft_id)


async def publish(app: Application, draft_id: int, when: Optional[datetime]) -> None:
    d = db.get_draft(draft_id)
    if d["status"] != db.DESIGN_PENDING:
        await app.bot.send_message(chat_id(), f"Taslak #{draft_id} zaten işlenmiş ({d['status']}).")
        return
    db.update_draft(draft_id, status=db.PUBLISHING, scheduled_for=(when or datetime.now(TZ)).isoformat())
    label = f"{slots.fmt(when)} için zamanlanıyor" if when else "yayınlanıyor"
    msg = await app.bot.send_message(chat_id(), f"⏳ Taslak #{draft_id} {label}…")
    try:
        content = json.loads(d["content_json"])
        images = [Path(p) for p in json.loads(d["images_json"])]
        alts = [content.get("alt_text", "")] * len(images)
        resp = await asyncio.to_thread(zernio.create_post, d["caption"], images, alts, when)
        info = zernio.post_info(resp)
        db.update_draft(
            draft_id,
            status=db.SCHEDULED if when else db.PUBLISHED,
            zernio_post_id=info["id"],
            permalink=info["url"],
        )
        if when:
            await msg.edit_text(f"✅ Taslak #{draft_id} zamanlandı: {slots.fmt(when)}\n(Zernio yayınlayacak; Mac'in açık olması gerekmez.)")
        else:
            await msg.edit_text(f"✅ Taslak #{draft_id} gönderildi. Durum: {info['status']}\n{info['url'] or ''}")
    except Exception as ex:
        log.exception("Yayın hatası")
        db.update_draft(draft_id, status=db.DESIGN_PENDING, scheduled_for=None, error=str(ex))
        await msg.edit_text(f"⚠️ Yayınlanamadı: {str(ex)[:400]}\nTaslak tekrar onay bekliyor.", reply_markup=design_kb(draft_id))


# ============ Komutlar ============
HELP = (
    "<b>INSTAGRAM İÇERİK BOTU 🚀</b>\n\n"
    "/fikir — Şimdi taslak fikir üret (varsayılan ayardaki sayı)\n"
    "/tasarla — Firma bilgilerini gir, ona özel tasarım + açıklama hazırlansın\n"
    "/tara — İlham hesaplarını şimdi tara\n"
    "/ilham &lt;link&gt; — Tek bir gönderiyi ilham havuzuna ekle (ya da sadece linki gönder)\n"
    "/kuyruk — Bekleyen ve planlanan içerikler\n"
    "/slotlar — Boş yayın saatleri\n\n"
    "Her tasarım taslağı mesajını <b>yanıtlayarak</b> revize edebilirsin."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    if chat_id() is None:
        await update.message.reply_text(
            f"Chat ID'n: {cid}\n.env dosyasında TELEGRAM_CHAT_ID={cid} yap ve botu yeniden başlat."
        )
        return
    if cid != chat_id():
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_fikir(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    try:
        await run_ideation(ctx.application, n=min(n or 3, 6) if n else None, force=True)
    except Exception as ex:
        log.exception("Fikir üretimi hatası")
        await update.message.reply_text(f"⚠️ Hata: {str(ex)[:400]}")


async def cmd_tara(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔎 İlham hesapları taranıyor (1-3 dk)…")
    try:
        n = await asyncio.to_thread(apify.scrape)
        await update.message.reply_text(f"✅ {n} gönderi güncellendi.")
    except Exception as ex:
        await update.message.reply_text(f"⚠️ Apify hatası: {str(ex)[:400]}")


async def add_inspiration_url(update: Update, url: str) -> None:
    await update.message.reply_text("🔎 Gönderi çekiliyor…")
    try:
        n = await asyncio.to_thread(apify.scrape, [url], 1)
        await update.message.reply_text(f"✅ İlham havuzuna eklendi ({n}). Sonraki fikir turunda öncelikli kullanılacak.")
    except Exception as ex:
        await update.message.reply_text(f"⚠️ Eklenemedi: {str(ex)[:400]}")


async def cmd_ilham(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = IG_URL.search(" ".join(ctx.args or []))
    if not m:
        await update.message.reply_text("Kullanım: /ilham https://www.instagram.com/p/XXXX/")
        return
    await add_inspiration_url(update, m.group(0))


async def cmd_kuyruk(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lines = []
    labels = {
        db.IDEA_PENDING: "💡 Fikir onayı bekliyor",
        db.DESIGNING: "🎨 Tasarlanıyor",
        db.DESIGN_PENDING: "📝 Paylaşım onayı bekliyor",
        db.SCHEDULED: "⏰ Planlandı",
    }
    for st, label in labels.items():
        rows = db.drafts_by_status(st)
        if rows:
            lines.append(f"<b>{label}</b>")
            for r in rows:
                t = json.loads(r["idea_json"])["title"]
                when = f" — {slots.fmt(datetime.fromisoformat(r['scheduled_for']))}" if st == db.SCHEDULED and r["scheduled_for"] else ""
                lines.append(f"  #{r['id']} {e(t)}{when}")
    await update.message.reply_text("\n".join(lines) or "Kuyruk boş.", parse_mode=ParseMode.HTML)


async def cmd_slotlar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = slots.upcoming_free_slots(8)
    await update.message.reply_text("Boş slotlar:\n" + "\n".join(slots.fmt(x) for x in s) if s else "Boş slot yok.")


# ============ /tasarla sihirbazı ============
TOPICS = [
    "Logo + marka kimliği mockupları",
    "9'lu Instagram post seti",
    "Ambalaj tasarımı",
    "Poster / kampanya görseli",
    "Menü tasarımı",
    "3D ürün görseli",
    "Web / uygulama arayüzü",
]
BRIEF_STEPS = ["name", "sector", "colors", "topic", "format", "notes"]
BRIEF_QUESTIONS = {
    "name": "1/6 · <b>Firma adı</b> ne?",
    "sector": "2/6 · <b>Sektör</b> ne? (ör. üçüncü nesil kahveci, diş kliniği, butik otel)",
    "colors": "3/6 · <b>Renkler</b>? (ör. “lacivert + altın”, “#0B1F3A, #C9A85A”)\nBana bırakmak için “-” yaz.",
    "topic": "4/6 · <b>Tasarım konusu</b> ne? Seç ya da kendin yaz:",
    "format": "5/6 · <b>Format</b>?",
    "notes": "6/6 · <b>Ek notlar</b>? (hedef kitle, stil, slogan, olmasını istemediğin şeyler…)\nGeçmek için “-” yaz.",
}
CANCEL_ROW = [B("❌ Vazgeç", callback_data="tz:cancel")]


async def ask_brief_step(message, brief: Dict) -> None:
    step = brief["step"]
    if step == "topic":
        rows = [[B(t, callback_data=f"tz:topic:{i}")] for i, t in enumerate(TOPICS)]
        await message.reply_text(BRIEF_QUESTIONS[step], parse_mode=ParseMode.HTML, reply_markup=KB(rows + [CANCEL_ROW]))
    elif step == "format":
        rows = [
            [B("🗂 Carousel proje (6-8 slayt)", callback_data="tz:format:portfolio")],
            [B("🖼 Tek görsel", callback_data="tz:format:single")],
        ]
        await message.reply_text(BRIEF_QUESTIONS[step], parse_mode=ParseMode.HTML, reply_markup=KB(rows + [CANCEL_ROW]))
    elif step == "confirm":
        fmt = "Carousel proje" if brief["format"] == "portfolio" else "Tek görsel"
        text = (
            "<b>Brief özeti</b>\n\n"
            f"🏷 Firma: {e(brief['name'])}\n🏭 Sektör: {e(brief['sector'])}\n"
            f"🎨 Renkler: {e(brief['colors'] or 'bana bırakıldı')}\n🧩 Konu: {e(brief['topic'])}\n"
            f"📐 Format: {fmt}\n📝 Notlar: {e(brief['notes'] or '-')}"
        )
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=KB([[B("✅ Tasarla", callback_data="tz:go"), B("❌ Vazgeç", callback_data="tz:cancel")]]),
        )
    else:
        await message.reply_text(BRIEF_QUESTIONS[step], parse_mode=ParseMode.HTML, reply_markup=KB([CANCEL_ROW]))


def advance_brief(brief: Dict, value: str) -> None:
    step = brief["step"]
    v = value.strip()
    brief[step] = "" if v == "-" and step in ("colors", "notes") else v
    i = BRIEF_STEPS.index(step)
    brief["step"] = BRIEF_STEPS[i + 1] if i + 1 < len(BRIEF_STEPS) else "confirm"


async def cmd_tasarla(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("await", None)
    ctx.user_data["brief"] = {"step": "name"}
    await update.message.reply_text("🎨 Yeni tasarım. 6 kısa soru soracağım (iptal: /iptal).")
    await ask_brief_step(update.message, ctx.user_data["brief"])


async def cmd_iptal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    had = ctx.user_data.pop("brief", None) or ctx.user_data.pop("await", None)
    await update.message.reply_text("İptal edildi." if had else "İptal edilecek bir işlem yok.")


async def brief_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, brief: Dict) -> None:
    text = (update.message.text or "").strip()
    if brief["step"] == "confirm":
        await update.message.reply_text("Özetteki ✅ Tasarla ya da ❌ Vazgeç butonunu kullan.")
        return
    if brief["step"] == "format":
        await update.message.reply_text("Lütfen butonlardan birini seç.")
        return
    if not text or (text == "-" and brief["step"] in ("name", "sector", "topic")):
        await update.message.reply_text("Bu alan gerekli, lütfen yaz.")
        return
    advance_brief(brief, text[:500])
    await ask_brief_step(update.message, brief)


async def on_brief_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.message is None or q.message.chat.id != chat_id():
        return
    brief = ctx.user_data.get("brief")
    parts = q.data.split(":")
    await q.edit_message_reply_markup(None)
    if parts[1] == "cancel":
        ctx.user_data.pop("brief", None)
        await q.message.reply_text("❌ Tasarım iptal edildi.")
        return
    if not brief:
        await q.message.reply_text("Bu sihirbazın süresi dolmuş, /tasarla ile yeniden başla.")
        return
    if parts[1] == "topic" and brief["step"] == "topic":
        advance_brief(brief, TOPICS[int(parts[2])])
        await ask_brief_step(q.message, brief)
    elif parts[1] == "format" and brief["step"] == "format":
        advance_brief(brief, parts[2])
        await ask_brief_step(q.message, brief)
    elif parts[1] == "go" and brief["step"] == "confirm":
        ctx.user_data.pop("brief", None)
        await q.message.reply_text(f"🧠 {brief['name']} için konsept hazırlanıyor…")
        try:
            idea = await asyncio.to_thread(llm.idea_from_brief, brief)
        except Exception as ex:
            log.exception("Brief fikri hatası")
            await q.message.reply_text(f"⚠️ Hata: {str(ex)[:400]}")
            return
        did = db.create_draft(idea)
        await design_draft(ctx.application, did)


# ============ Butonlar ============
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.message is None or q.message.chat.id != chat_id():
        return
    action, _, rest = q.data.partition(":")
    parts = rest.split(":")
    draft_id = int(parts[0])
    d = db.get_draft(draft_id)
    if not d:
        return
    app = ctx.application

    # ---- Fikir aşaması ----
    if action in ("ia", "ir", "ix") and d["status"] != db.IDEA_PENDING:
        await q.message.reply_text(f"#{draft_id} zaten işlenmiş ({d['status']}).")
        return
    if action == "ia":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"✅ #{draft_id} onaylandı.")
        await design_draft(app, draft_id)
    elif action == "ir":
        ctx.user_data["await"] = ("idea_feedback", draft_id)
        await q.message.reply_text(f"#{draft_id} için ne değişsin? Yaz (ya da “-” yaz, tamamen yeni bir fikir üreteyim).")
    elif action == "ix":
        db.update_draft(draft_id, status=db.REJECTED)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"❌ #{draft_id} reddedildi.")

    # ---- Paylaşım aşaması ----
    elif d["status"] != db.DESIGN_PENDING:
        await q.message.reply_text(f"#{draft_id} paylaşım onayı beklemiyor ({d['status']}).")
    elif action == "ps":
        when = slots.next_free_slot()
        if not when:
            await q.message.reply_text("Boş slot bulunamadı, 🕐 Başka saat ile seç.")
            return
        await q.edit_message_reply_markup(None)
        await publish(app, draft_id, when)
    elif action == "pt":
        opts = slots.upcoming_free_slots(6)
        rows = [[B(slots.fmt(o), callback_data=f"pa:{draft_id}:{int(o.timestamp())}")] for o in opts]
        rows.append([B("⌨️ Saati yazayım", callback_data=f"pc:{draft_id}")])
        await q.message.reply_text("Ne zaman paylaşalım?", reply_markup=KB(rows))
    elif action == "pa":
        when = datetime.fromtimestamp(int(parts[1]), TZ)
        await q.edit_message_reply_markup(None)
        await publish(app, draft_id, when)
    elif action == "pc":
        ctx.user_data["await"] = ("time", draft_id)
        await q.message.reply_text("Saati yaz: “18:30”, “yarın 10:00” ya da “08.11 19:45”")
    elif action == "pn":
        today = slots.posts_on_day(datetime.now(TZ).date())
        if today >= int(CFG["posting"].get("posts_per_day", 1)) and parts[-1] != "ok":
            await q.message.reply_text(
                f"Bugün zaten {today} gönderi var (limit {CFG['posting']['posts_per_day']}). Yine de paylaşılsın mı?",
                reply_markup=KB([[B("Evet, paylaş", callback_data=f"pn:{draft_id}:ok")]]),
            )
            return
        await q.edit_message_reply_markup(None)
        await publish(app, draft_id, None)
    elif action == "pe":
        ctx.user_data["await"] = ("design_feedback", draft_id)
        await q.message.reply_text("Neyi değiştireyim? (slayt metni, açıklama, hashtag, ton…)")
    elif action == "pr":
        await q.edit_message_reply_markup(None)
        await design_draft(app, draft_id, feedback="Tamamen farklı bir kurgu ve metinle yeniden yaz.")
    elif action == "px":
        db.update_draft(draft_id, status=db.REJECTED)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"❌ #{draft_id} iptal edildi.")


# ============ Serbest metin (revize / saat / link) ============
async def revise_idea(app: Application, draft_id: int, feedback: str) -> None:
    d = db.get_draft(draft_id)
    prev = json.loads(d["idea_json"])
    fb = "Tamamen farklı yeni bir fikir üret." if feedback.strip() == "-" else feedback
    await app.bot.send_message(chat_id(), f"🔁 #{draft_id} yeniden yazılıyor…")
    ideas = await asyncio.to_thread(llm.generate_ideas, 1, fb, prev)
    db.update_draft(draft_id, idea_json=ideas[0], status=db.IDEA_PENDING)
    await send_idea(app, draft_id, ideas[0])


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    app = ctx.application

    brief = ctx.user_data.get("brief")
    if brief and not update.message.reply_to_message:
        await brief_text(update, ctx, brief)
        return

    pending = ctx.user_data.pop("await", None)
    target = None
    if update.message.reply_to_message:
        target = db.draft_for_message(update.message.reply_to_message.message_id)

    if pending and pending[0] == "time":
        when = slots.parse_user_time(text)
        if not when:
            ctx.user_data["await"] = pending
            await update.message.reply_text("Anlayamadım ya da geçmiş bir saat. Örn: 18:30 / yarın 10:00 / 08.11 19:45")
            return
        await publish(app, pending[1], when)
        return

    if pending or target:
        kind, did = pending if pending else (None, target)
        d = db.get_draft(did)
        try:
            if d["status"] == db.IDEA_PENDING:
                await revise_idea(app, did, text)
            elif d["status"] == db.DESIGN_PENDING:
                await design_draft(app, did, feedback=text)
            else:
                await update.message.reply_text(f"#{did} şu an revize edilemez ({d['status']}).")
        except Exception as ex:
            log.exception("Revizyon hatası")
            await update.message.reply_text(f"⚠️ Hata: {str(ex)[:400]}")
        return

    m = IG_URL.search(text)
    if m:
        await add_inspiration_url(update, m.group(0))
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


# ============ Zamanlanmış iş ============
async def daily_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_ideation(ctx.application)
    except Exception as ex:
        log.exception("Günlük iş hatası")
        await ctx.bot.send_message(chat_id(), f"⚠️ Günlük taslak üretimi başarısız: {str(ex)[:400]}")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("İşlenmeyen hata", exc_info=ctx.error)


async def post_init(app: Application) -> None:
    from telegram import BotCommand

    await app.bot.set_my_commands([
        BotCommand("tasarla", "Firma bilgileriyle özel tasarım hazırla"),
        BotCommand("fikir", "Şimdi taslak fikir üret"),
        BotCommand("kuyruk", "Bekleyen ve planlanan içerikler"),
        BotCommand("slotlar", "Boş yayın saatleri"),
        BotCommand("tara", "İlham hesaplarını tara"),
        BotCommand("ilham", "Gönderiyi ilham havuzuna ekle"),
        BotCommand("iptal", "Devam eden işlemi iptal et"),
        BotCommand("start", "Komut listesi"),
    ])


def build_app() -> Application:
    app = (
        Application.builder().token(env("TELEGRAM_BOT_TOKEN")).concurrent_updates(True).post_init(post_init).build()
    )
    app.add_handler(CommandHandler("start", cmd_start))

    cid = chat_id()
    if cid is None:
        log.warning("TELEGRAM_CHAT_ID boş — yalnızca /start çalışır. Bota /start yaz.")
        return app

    only_me = filters.Chat(chat_id=cid)
    app.add_handler(CommandHandler(["yardim", "help"], lambda u, c: u.message.reply_text(HELP, parse_mode=ParseMode.HTML), filters=only_me))
    app.add_handler(CommandHandler("fikir", cmd_fikir, filters=only_me))
    app.add_handler(CommandHandler("tasarla", cmd_tasarla, filters=only_me))
    app.add_handler(CommandHandler("iptal", cmd_iptal, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_brief_button, pattern=r"^tz:"))
    app.add_handler(CommandHandler("tara", cmd_tara, filters=only_me))
    app.add_handler(CommandHandler("ilham", cmd_ilham, filters=only_me))
    app.add_handler(CommandHandler("kuyruk", cmd_kuyruk, filters=only_me))
    app.add_handler(CommandHandler("slotlar", cmd_slotlar, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ia|ir|ix|ps|pt|pa|pc|pn|pe|pr|px):"))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    h, m = CFG["ideation"]["run_at"].split(":")
    app.job_queue.run_daily(daily_job, time=dtime(int(h), int(m), tzinfo=TZ), name="daily_ideation")
    return app
