"""OpenAI ile fikir taslağı, slayt içeriği ve açıklama üretimi."""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Literal, Optional

import httpx
import openai
from pydantic import BaseModel, Field

from . import db
from .config import CFG

log = logging.getLogger(__name__)
_client: Optional[openai.OpenAI] = None


def client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI(timeout=300)  # OPENAI_API_KEY .env'den okunur
    return _client


# ---------------- Şemalar ----------------
class Idea(BaseModel):
    title: str = Field(description="Kısa iç başlık (Telegram'da görünür)")
    format: Literal["single", "carousel", "portfolio"] = Field(
        description="portfolio = baştan sona hayali bir konsept tasarım projesi (logo+mockup seti, 9'lu post seti vb.)"
    )
    pillar: str = Field(description="Hangi içerik sütununa ait")
    hook: str = Field(description="Kaydırmayı durduracak ilk cümle / kapak başlığı")
    angle: str = Field(description="İçeriğin özgün bakış açısı, 1-2 cümle")
    key_points: List[str] = Field(description="Ana mesajlar; carousel ise slayt başına bir madde")
    inspired_by: List[str] = Field(description="İlham alınan gönderi id'leri (verilen listeden)")
    why_it_works: str = Field(description="İlham verisine dayanarak neden tutacağı, 1-2 cümle")
    visual_concept: str = Field(
        description="Gönderide GÖSTERİLECEK somut tasarım örnekleri (ör. 'hayali kahve markası için logo + "
        "ambalaj mockup', 'önce/sonra poster'). Referans görsellerin hangi özelliklerinden (kompozisyon, "
        "tipografi, renk kullanımı, mockup/3D tarzı) ilham alındığını kısaca belirt."
    )


class IdeaBatch(BaseModel):
    ideas: List[Idea]


class Slide(BaseModel):
    kind: Literal["cover", "content", "list", "quote", "showcase", "cta"]
    eyebrow: str = Field(description="Üstte küçük etiket, ör. 'İPUCU 02' — boş olabilir")
    title: str = Field(description="Ana başlık; en fazla ~8 kelime")
    body: str = Field(description="Açıklama metni; en fazla ~35 kelime, boş olabilir")
    bullets: List[str] = Field(description="'list' türü için 2-5 kısa madde, diğerlerinde boş liste")
    highlight: str = Field(description="Başlıkta vurgu rengiyle boyanacak tek kelime/ifade; başlıkta birebir geçmeli, yoksa boş")
    image_prompt: str = Field(
        description="Yalnızca 'showcase' için: görsel modeline gidecek İNGİLİZCE, ayrıntılı tarif "
        "(ne tasarlandığı, hayali marka adı, kompozisyon, kamera açısı, malzeme, renkler, ışık). Diğer türlerde boş."
    )


class PostContent(BaseModel):
    brand_brief: str = Field(
        description="Yalnızca portfolio için: projenin İNGİLİZCE marka tanımı (hayali marka adı, sektör, kitle, "
        "renk paleti hex kodlarıyla, tipografi karakteri, logo tarifi, görsel atmosfer). Diğer formatlarda boş."
    )
    slides: List[Slide]
    caption: str = Field(description="Instagram açıklaması: hook + değer + CTA; hashtag içermez")
    hashtags: List[str] = Field(description="8-15 hashtag, # işaretiyle")
    alt_text: str = Field(description="Görme engelliler için kısa görsel açıklaması")


# ---------------- Yardımcılar ----------------
def _brand_block() -> str:
    b = CFG["brand"]
    return (
        f"Marka: {b['name']} ({b['handle']})\n"
        f"Alan: {b['niche']}\nHedef kitle: {b['audience']}\nTon: {b['tone']}\n"
        f"Hedef: {b['goal']}\nVarsayılan CTA: {b['cta_default']}\n"
        f"İçerik sütunları: {'; '.join(b['pillars'])}\n"
        f"Kaçınılacaklar: {'; '.join(b['avoid'])}\n"
        f"Dil: {b.get('language', 'tr')} (tüm çıktı bu dilde)"
    )


SYSTEM = (
    "Sen bir Instagram içerik stratejisti ve metin yazarısın. Görevin, verilen markanın "
    "sesiyle, ilham hesaplarındaki kalıplardan (konu, hook yapısı, format, yorumlarda sorulan sorular) "
    "öğrenerek ÖZGÜN gönderiler üretmek. İçeriği asla kopyalama; fikri dönüştür ve markanın "
    "uzmanlığına bağla. Yorumlardaki sorular, itirazlar ve talepler en değerli sinyaldir. "
    "Uydurma istatistik kullanma.\n\n" + _brand_block()
)


def _inspiration_payload(rows) -> str:
    out = []
    for r in rows:
        comments = json.loads(r["comments_json"] or "[]")
        out.append(
            {
                "id": r["id"],
                "hesap": r["owner"],
                "tür": r["type"],
                "beğeni": r["likes"],
                "yorum": r["comments_count"],
                "izlenme": r["views"],
                "açıklama": (r["caption"] or "")[:700],
                "yorumlar": [c["text"] for c in comments],
            }
        )
    return json.dumps(out, ensure_ascii=False, indent=1)


def _fetch_images(rows, max_images: int = 6) -> List[Dict[str, Any]]:
    """En iyi birkaç gönderinin görselini indirip base64 ekler (görsel stil sinyali). Hata olursa atlar."""
    blocks: List[Dict[str, Any]] = []
    for r in rows:
        if len(blocks) >= max_images * 2:
            break
        if not r["image_url"]:
            continue
        try:
            resp = httpx.get(r["image_url"], timeout=15, follow_redirects=True)
            resp.raise_for_status()
            mt = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            if mt not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                continue
            blocks.append({"type": "text", "text": f"Gönderi {r['id']} görseli:"})
            b64 = base64.standard_b64encode(resp.content).decode()
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}", "detail": "low"}})
        except Exception as e:  # CDN linkleri süreli olabilir
            log.debug("Görsel indirilemedi %s: %s", r["id"], e)
    return blocks


def _parse(content: List[Dict[str, Any]], schema):
    llm = CFG["llm"]
    kwargs: Dict[str, Any] = {}
    if llm.get("reasoning_effort"):
        kwargs["reasoning_effort"] = llm["reasoning_effort"]
    resp = client().chat.completions.parse(
        model=llm["model"],
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": content},
        ],
        response_format=schema,
        **kwargs,
    )
    msg = resp.choices[0].message
    if msg.refusal:
        raise RuntimeError(f"Model isteği reddetti: {msg.refusal}")
    if msg.parsed is None:
        raise RuntimeError(f"Yapılandırılmış çıktı alınamadı (finish_reason={resp.choices[0].finish_reason})")
    return msg.parsed


# ---------------- Ana fonksiyonlar ----------------
def generate_ideas(
    n: int, feedback: Optional[str] = None, previous: Optional[Dict] = None, portfolio_n: int = 0
) -> List[Dict]:
    ins_cfg = CFG["inspiration"]
    idea_cfg = CFG["ideation"]
    rows = db.top_inspiration(ins_cfg.get("top_posts_for_llm", 15))
    recent = db.recent_titles()

    content: List[Dict[str, Any]] = []
    if rows:
        content.append(
            {
                "type": "text",
                "text": "İLHAM HESAPLARINDAN GÖNDERİLER (etkileşime göre sıralı):\n"
                + _inspiration_payload(rows),
            }
        )
        content.append(
            {
                "type": "text",
                "text": "Aşağıdaki görseller referans hesapların tasarımları. Tasarım dillerini incele: "
                "kompozisyon, tipografi, renk, mockup/3D kullanımı, slayt kurgusu. Bunları kopyalamadan, "
                "markamızın estetiğiyle yeniden yorumlanabilecek tasarım örneği fikirleri çıkar.",
            }
        )
        content.extend(_fetch_images(rows))
    else:
        content.append({"type": "text", "text": "Henüz ilham verisi yok; marka sütunlarına göre üret."})

    task = (
        f"\n\nGÖREV: {n} adet gönderi fikri üret.\n"
        f"- Yaklaşık %{int(idea_cfg.get('carousel_ratio', 0.7) * 100)} carousel, kalanı tek görsel.\n"
        f"- Carousel en fazla {idea_cfg.get('max_carousel_slides', 7)} slayt (kapak + içerik + CTA).\n"
        "- Fikirler farklı sütunlara dağılsın, birbirine benzemesin.\n"
        "- Tek görsel formatı tek güçlü mesaj ya da tek etkileyici tasarım örneği için.\n"
        "- İzleyici yalnızca tasarım bilgisi değil, TASARIM ÖRNEKLERİ de görmeli: her fikirde "
        "visual_concept ile somut, görsel olarak etkileyici örnekler tanımla (konsept logo, marka kimliği "
        "mockup'ı, ambalaj, poster, sosyal medya postu, önce/sonra, 3D sahne).\n"
        "- inspired_by alanına yalnızca yukarıdaki listedeki id'leri yaz.\n"
        f"- Daha önce işlenen konuları tekrarlama: {json.dumps(recent, ensure_ascii=False)}"
    )
    if portfolio_n and not previous:
        types = idea_cfg.get("portfolio_types", [])
        task += (
            f"\n- Bu fikirlerden TAM OLARAK {portfolio_n} tanesi format='portfolio' olsun: hayali bir müşteri için "
            "baştan sona konsept tasarım projesi. Proje türü seçenekleri: "
            + "; ".join(types)
            + f". Son portföy projeleri (türü ve sektörü tekrarlama): {json.dumps(db.recent_portfolios(), ensure_ascii=False)}. "
            "Portföy fikrinde title proje adı olsun (ör. 'Mora Coffee — marka kimliği'), visual_concept'te "
            "gösterilecek parçaları sırala (ör. logo, kartvizit, kahve bardağı, tabela). key_points tasarım kararları olsun."
        )
    elif not previous:
        task += "\n- Bu turda format='portfolio' kullanma."
    if previous and feedback:
        task += (
            "\n\nREVİZYON: Aşağıdaki fikir geri bildirime göre yeniden yazılacak. "
            f"Yalnızca 1 fikir döndür.\nÖnceki fikir: {json.dumps(previous, ensure_ascii=False)}\n"
            f"Geri bildirim: {feedback}"
        )
    content.append({"type": "text", "text": task})

    batch: IdeaBatch = _parse(content, IdeaBatch)
    ideas = [i.model_dump() for i in batch.ideas][:n]
    db.mark_inspiration_used([x for i in ideas for x in i["inspired_by"]])
    return ideas


def idea_from_brief(brief: Dict) -> Dict:
    """/tasarla sihirbazından gelen bilgilerle doğrudan bir fikir oluşturur (fikir onayı atlanır)."""
    fmt = brief.get("format", "portfolio")
    task = (
        "MÜŞTERİ BRIEF'İ (Telegram /tasarla ile girildi):\n"
        f"- Firma adı: {brief.get('name')}\n- Sektör: {brief.get('sector')}\n"
        f"- Renkler: {brief.get('colors') or 'Belirtilmedi — sektöre ve markaya uygun bir palet öner'}\n"
        f"- Tasarım konusu: {brief.get('topic')}\n- Ek notlar: {brief.get('notes') or '-'}\n\n"
        f"GÖREV: Bu brief için format='{fmt}' olan TEK bir Instagram gönderisi fikri oluştur. Gönderi, yapılandırılmış marka hesabının "
        "bu firma için yaptığı konsept tasarım çalışmasını sergileyecek. title 'Firma adı — konu' biçiminde olsun. "
        "visual_concept'te gösterilecek parçaları sırala (ör. logo sunumu, kartvizit, tabela, bardak). key_points "
        "tasarım kararları olsun (renk, tipografi, konsept). Firma adını ve verilen renkleri birebir kullan, değiştirme. "
        "inspired_by boş liste olsun."
    )
    batch: IdeaBatch = _parse([{"type": "text", "text": task}], IdeaBatch)
    idea = batch.ideas[0].model_dump()
    idea["format"] = fmt
    idea["inspired_by"] = []
    idea["client"] = {k: brief.get(k, "") for k in ("name", "sector", "colors", "topic", "notes")}
    return idea


def generate_content(idea: Dict, feedback: Optional[str] = None, previous: Optional[Dict] = None) -> Dict:
    idea_cfg = CFG["ideation"]
    vis = CFG.get("visuals", {})
    max_show = int(vis.get("max_per_post", 3)) if vis.get("enabled", True) else 0
    fmt = idea["format"]
    if fmt == "portfolio":
        max_port = int(vis.get("max_per_portfolio", 6)) if vis.get("enabled", True) else 0
        rules = (
            f"PORTFÖY GÖNDERİSİ (konsept proje): {max_port + 2}-{max_port + 3} slayt. "
            "brand_brief alanını mutlaka doldur. "
            f"Slayt 1: kind='showcase', projenin ANA görseli (marka kimliği için logo sunum sayfası; post seti için "
            f"3x3 Instagram profil grid mockup'ı), eyebrow='KONSEPT PROJE', title=proje/marka adı. "
            f"Ardından {max(1, max_port - 1)}-{max_port - 1} adet 'showcase' slaytı: her biri projenin bir parçası "
            "(mockup, tekil post, ambalaj, tabela…); title 1-3 kelime (ör. 'Kartvizit', 'Post 01'), body en fazla 12 kelime. "
            "Araya en fazla 1 'list' slaytı (renk paleti, tipografi, konsept kararları). Son slayt 'cta' "
            "(ör. 'Markan için böyle bir kimlik ister misin?'). Hiç 'cover' kullanma.\n"
            "image_prompt kuralları: İngilizce; slayt 1'in tarifi tek başına eksiksiz olsun (logo/grid tamamen "
            "tanımlı). Sonraki slaytlar slayt 1'in görselini REFERANS alarak üretilecek: 'the same brand from the "
            "reference image' diye başla, aynı logo/renk/tipografiyi uygulandığı yeni yüzeyi tarif et. Dikey 2:3, "
            "alt %25 sade. Görsel içi metin yalnızca marka adı ve çok kısa İngilizce/Latin kelimeler. "
            "Caption: proje hikayesi (brief → konsept → kararlar), bunun hayali bir konsept çalışma olduğunu "
            "belirt, sonda DM CTA. "
        )
        max_show = max_port
    elif fmt == "single":
        rules = (
            "Tek görsel: tam olarak 1 slayt üret. "
            + ("Tercihen kind='showcase' (etkileyici tek tasarım örneği + kısa başlık); " if max_show else "")
            + "alternatif olarak 'cover' veya 'quote'. "
        )
    else:
        rules = (
            f"Carousel: 4-{idea_cfg.get('max_carousel_slides', 7)} slayt. İlk slayt 'cover' (merak uyandıran hook), "
            "son slayt 'cta' (kaydet/paylaş/DM çağrısı), aradakiler 'content', 'list' veya 'showcase'. "
            "Her slayt tek bir fikir taşısın; metinler kısa, mobilde okunaklı. eyebrow ile ilerleme ver (ör. '01/05'). "
        )
        if max_show:
            rules += (
                f"1-{max_show} adet 'showcase' slaytı ekle: anlatılan ilkeyi gösteren somut tasarım örnekleri "
                "(bilgi slaytından hemen sonra örneğini göstermek iyi çalışır). showcase slaytında title kısa "
                "(en fazla 6 kelime), body en fazla 15 kelime olsun; görsel konuşsun. "
            )
    if max_show and fmt != "portfolio":
        rules += (
            "\nimage_prompt kuralları: İngilizce yaz; tek bir somut tasarım çıktısını tarif et (ör. 'photoreal mockup "
            "of a matte black coffee bag for a fictional roastery called \"Mora\"...'). Dikey 2:3 kompozisyon, ana "
            "obje merkezde, alt %30'u sade bırak (üstüne başlık binecek). Görselin içine Türkçe metin yazdırma; "
            "gerekiyorsa yalnızca kısa hayali marka adı. Referans görsellerin stil özelliklerini (kompozisyon, ışık, "
            "malzeme, tipografi karakteri) uygula ama hiçbir tasarımı kopyalama. "
        )
    client_info = idea.get("client")
    if client_info:
        rules += (
            "\nMÜŞTERİ ÇALIŞMASI: Bu gönderi gerçek bir brief'e dayanıyor. Marka adı TAM OLARAK "
            f"\"{client_info['name']}\" (yazımını değiştirme, hayali marka uydurma). Sektör: {client_info['sector']}. "
            f"Renkler: {client_info['colors'] or 'sektöre uygun bir palet seç ve brand_brief içinde hex kodlarıyla yaz'}. "
            f"Ek notlar: {client_info['notes'] or '-'}. brand_brief alanını HER formatta doldur (renkler hex kodlarıyla). "
            "Tüm image_prompt'larda marka adını ve renkleri aynen kullan. Caption'da bunun markanın "
            f"{client_info['name']} için hazırladığı bir konsept çalışma olduğunu söyle; sonda DM CTA. "
        )
    content: List[Dict[str, Any]] = []
    refs = db.get_inspiration(idea.get("inspired_by", []))
    if refs and max_show:
        content.append({"type": "text", "text": "İlham alınan referans tasarımlar (stil sinyali, kopyalama):"})
        content.extend(_fetch_images(refs, max_images=3))
    task = (
        f"ONAYLANMIŞ FİKİR:\n{json.dumps(idea, ensure_ascii=False, indent=1)}\n\n"
        f"GÖREV: Bu fikrin görsel metinlerini ve Instagram açıklamasını yaz.\n{rules}\n"
        "Açıklama: ilk satır güçlü hook (125 karakter içinde), ardından 3-6 kısa paragraf değer, "
        "sonda soru ile yorum teşviki + CTA. Satır araları kullan. Hashtag'leri ayrı alanda ver "
        "(niş + orta hacimli karışık, Türkçe ağırlıklı)."
    )
    if previous and feedback:
        task += (
            f"\n\nREVİZYON: Önceki sürüm:\n{json.dumps(previous, ensure_ascii=False)}\n"
            f"Geri bildirim: {feedback}\nGeri bildirimin dokunmadığı kısımları koru."
        )
    content.append({"type": "text", "text": task})
    pc: PostContent = _parse(content, PostContent)
    data = pc.model_dump()
    if fmt == "single":
        data["slides"] = data["slides"][:1]
    # sınırı aşan ya da tarifsiz showcase'leri metin slaytına çevir
    shown = 0
    for sl in data["slides"]:
        if sl["kind"] != "showcase":
            sl["image_prompt"] = ""
        elif sl["kind"] == "showcase":
            if shown < max_show and sl["image_prompt"].strip():
                shown += 1
            else:
                sl["kind"], sl["image_prompt"] = "content", ""
    data["portfolio"] = fmt == "portfolio"
    data["client_name"] = client_info["name"] if client_info else ""
    if not data["portfolio"] and not client_info:
        data["brand_brief"] = ""
    return data


def build_caption(content: Dict) -> str:
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in content.get("hashtags", []))
    cap = content["caption"].strip()
    full = f"{cap}\n\n{tags}".strip()
    return full[:2200]
