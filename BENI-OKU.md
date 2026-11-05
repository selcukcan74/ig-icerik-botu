# BENİ-OKU — Kurulum ve müşteriye uyarlama rehberi

Bu paket, Instagram için fikir üretimi, görsel hazırlama, onay ve yayın planlama adımlarını otomatikleştiren bir içerik motorudur. **Kod kişisel bir kurulumdan genelleştirilmiştir; kullanmadan önce aşağıdaki alanları kendi markanıza ve servis hesaplarınıza göre düzenleyin.**

> **Önemli:** Bu paketle birlikte kişisel API anahtarları, Telegram sohbet bilgileri, veritabanı, loglar, üretilmiş görseller veya sanal ortam paylaşılmaz. Her kullanıcı kendi servis hesaplarını bağlamalıdır.

## 1. İlk kurulum

1. Python 3.10 veya üzeri kurun.
2. Depoyu indirin ve proje klasörüne girin.
3. Sanal ortam oluşturup bağımlılıkları yükleyin:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

4. Ortam dosyasını oluşturun:

   ```bash
   cp .env.example .env
   ```

5. Aşağıdaki servis anahtarlarını `.env` içine girin.
6. `config.yaml` dosyasını markanıza göre düzenleyin.
7. `./run.sh` ile başlatın ve Telegram botuna `/start` gönderin. Botun bildirdiği sohbet kimliğini `.env` içindeki `TELEGRAM_CHAT_ID` alanına yazıp uygulamayı yeniden başlatın.
8. `/fikir` komutuyla ilk fikir üretimini deneyin.

## 2. Hangi belgede hangi alan değiştirilmeli?

### A. `.env` — zorunlu servis bağlantıları

`.env.example` dosyasını `.env` olarak kopyalayın. Aşağıdaki alanları kendi hesaplarınızdan alınan değerlerle doldurun:

| Değişken | Nereden alınır? | Ne için kullanılır? |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI Platform / API keys | Metin ve görsel üretimi |
| `APIFY_TOKEN` | Apify Console / Integrations | Referans Instagram hesaplarını tarama |
| `TELEGRAM_BOT_TOKEN` | Telegram'da `@BotFather` | Onay botu bağlantısı |
| `TELEGRAM_CHAT_ID` | Botu başlatıp `/start` gönderince | Onayların iletileceği özel sohbet |
| `ZERNIO_API_KEY` | Zernio hesabı | İçerik planlama ve yayınlama |
| `ZERNIO_ACCOUNT_ID` | Zernio bağlı sosyal hesap bilgileri | Hedef Instagram hesabı |

**Bu dosyayı kimseyle paylaşmayın ve GitHub'a yüklemeyin.** Anahtarları mesajlaşma veya ekran görüntüsüyle göndermeyin.

### B. `config.yaml` — markaya göre düzenlenecek ana dosya

| Bölüm | Değiştirilecek alanlar | Açıklama |
|---|---|---|
| `timezone` | Saat dilimi | Örn. `Europe/Istanbul` |
| `brand` | `name`, `handle`, `language`, `niche`, `audience`, `tone`, `goal`, `cta_default` | Marka adı, Instagram kullanıcı adı, sektör, hedef kitle, marka sesi ve içerik hedefi |
| `brand.pillars` | Liste | İçerik kategorileri / konu sütunları |
| `brand.avoid` | Liste | Üretilmesini istemediğiniz konular ve kurallar |
| `design` | `width`, `height`, `colors`, `fonts`, `google_fonts` | Görsel ölçüleri, renk paleti ve yazı tipleri |
| `posting` | `posts_per_day`, `slots`, `max_queue_days` | Günlük gönderi sayısı, saatleri ve kuyruk limiti |
| `ideation` | `run_at`, `ideas_per_run`, `carousel_ratio`, `max_carousel_slides` | Fikir üretim saati ve içerik formatı tercihleri |
| `ideation.portfolio_*` | `portfolio_per_week`, `portfolio_types` | Portföy/konsept içerik üretimi |
| `visuals` | `enabled`, `model`, `size`, `quality`, `max_per_post`, `max_per_portfolio` | Görsel üretim modeli, kalite ve adet sınırları |
| `visuals.style` | Metin | Markanızın görsel sanat yönetimi / tasarım dili |
| `visuals.rules` | Metin | Görsel üretiminde uyulacak kurallar |
| `inspiration.accounts` | Liste | İlham alınacak herkese açık Instagram kullanıcı adları; `@` yazmadan girin |
| `inspiration.*` | Sayısal ayarlar | Hesap başına tarama sayısı, tarama sıklığı ve modele aktarılacak örnek sayısı |
| `llm` | `model`, `reasoning_effort` | OpenAI model ve akıl yürütme ayarı |

**Not:** `inspiration.accounts` başlangıçta boş bırakılmıştır. En az bir hesap eklemeden ilham taraması içerik kaynağı bulamaz. Hesapların içeriklerini kopyalamayın; yalnızca fikir/format/etkileşim örüntülerini referans alın.

### C. `templates/slide.html.j2` — görsel şablon

Bu dosya Instagram görsellerinin HTML tasarım şablonudur. Tasarımın yerleşimini, tipografisini, boşluklarını, slayt türlerini veya görsel bileşenlerini değiştirmek istiyorsanız düzenleyin. Renk ve font gibi temel değişkenleri mümkün olduğunca önce `config.yaml → design` bölümünden yönetin.

### D. `igbot/llm.py` — içerik üretim davranışı

Fikir üretimi, slayt metni, caption, hashtag ve revizyon talimatları bu dosyadaki promptlarda bulunur. Markaya özgü ek üretim kuralları gerekiyorsa burayı düzenleyin. Genel marka bilgilerini kod içine sabitlemeyin; `config.yaml` üzerinden okuyun.

### E. `igbot/imagegen.py` — görsel üretim promptu

Görsel üretim talimatlarının birleştirilmesi ve referans görsel kullanımı burada yapılır. Tasarım dili için önce `config.yaml → visuals.style` ve `visuals.rules` alanlarını kullanın.

### F. `com.example.instagram-content-engine.plist` — isteğe bağlı macOS otomatik başlatma

Yalnızca macOS'ta oturum açıldığında botu otomatik başlatmak istiyorsanız kullanın. Dosyadaki `/ABSOLUTE/PATH/TO/PROJECT` alanlarının tamamını kendi proje yolunuzla değiştirin. Python yolu `.venv/bin/python` olmalıdır. Bu dosya Windows/Linux için geçerli değildir; normal başlatma için `./run.sh` yeterlidir.

### G. `requirements.txt` ve `run.sh`

Genellikle değiştirmeniz gerekmez. Yeni Python paketi eklenirse `requirements.txt` güncellenmelidir. `run.sh` proje klasöründe sanal ortamın Python'uyla uygulamayı başlatır.

## 3. Komutlar

| Telegram komutu | İşlev |
|---|---|
| `/start` | Botu başlatır ve sohbet kimliğini gösterir |
| `/fikir [n]` | Hemen fikir üretir |
| `/tara` | İlham hesaplarını tarar |
| `/ilham <Instagram bağlantısı>` | Tek gönderiyi ilham havuzuna ekler |
| `/tasarla` | Bir marka/müşteri brief'i üzerinden içerik hazırlar |
| `/iptal` | Devam eden tasarım sihirbazını iptal eder |
| `/kuyruk` | Bekleyen ve planlanmış gönderileri listeler |
| `/slotlar` | Yayın saatlerini listeler |

Fikir veya tasarım mesajını Telegram'da yanıtlayarak revizyon isteyebilirsiniz. İçerik, fikir onayı ve görsel onayı olmadan sonraki aşamaya geçmez.

## 4. Üretim hattı nasıl çalışır?

1. Apify, `config.yaml → inspiration.accounts` listesindeki hesapların herkese açık gönderilerini tarar.
2. OpenAI, marka bilgileri ve tarama sinyallerinden özgün günlük fikirler oluşturur.
3. Telegram üzerinden fikir onayı, ret veya revizyon alınır.
4. Onaylanan fikir için OpenAI slayt metinlerini, açıklamayı ve hashtag'leri üretir.
5. Jinja/HTML şablonu Playwright ile PNG görsellere dönüştürülür. İhtiyaç duyulan tasarım örnekleri OpenAI görsel modeliyle üretilebilir.
6. Telegram'da görseller ve metin için ikinci onay alınır.
7. Onaylanan içerik Zernio ile hemen yayınlanır veya planlanır.

## 5. Maliyet ve operasyon notları

- OpenAI, Apify ve Zernio kullanımına göre ücretlendirme/limit uygulayabilir. İlk denemeleri düşük adetlerle yapın.
- Görsel üretimi metin üretiminden daha maliyetli olabilir; `visuals.enabled`, kalite ve adet sınırlarını kontrol edin.
- Apify tarama sıklığını ve hesap başına gönderi sayısını ihtiyacınıza göre azaltabilirsiniz.
- Zernio'da doğru Instagram hesabının bağlı olduğundan emin olun. Önce test hesabında deneyin.
- Telegram onay akışı nihai insan kontrolü içindir; yayın öncesi görsel, açıklama, etiket ve hedef hesabı kontrol edin.
- Botun günlük fikir üretmesi için çalıştığı makinenin açık ve internete bağlı olması gerekir. Zernio'ya iletilmiş planlı gönderiler, Zernio servisinin koşullarına göre yayınlanır.

## 6. Güvenlik ve veri

- `.env`, `data/`, `.venv/`, loglar, SQLite veritabanı, indirilen referanslar ve oluşturulmuş görseller paylaşım paketine dahil edilmemelidir.
- API anahtarlarını iptal/yenileme gerekirse ilgili servis panellerinden yapın.
- Telegram botunu yalnızca sizin kontrol ettiğiniz özel sohbet üzerinden kullanın.
- Üçüncü taraf Instagram hesaplarından veri çekmeden önce Apify ve Instagram şartlarını, gizlilik ve telif haklarını değerlendirin.
- `data/` klasörü yerel kullanım verilerini içerir; yedekleme ve silme politikasını kendiniz belirleyin.

## 7. İlk test kontrol listesi

- [ ] `.env` oluşturuldu ve altı değişken dolduruldu.
- [ ] `config.yaml` marka bilgileri ve içerik sütunları güncellendi.
- [ ] İlham hesapları eklendi veya ilham taraması kullanılmayacaksa boş bırakıldı.
- [ ] Telegram `/start` tamamlandı ve doğru `TELEGRAM_CHAT_ID` girildi.
- [ ] Zernio'da doğru Instagram hesabı bağlı.
- [ ] `/fikir 1` ile fikir üretimi test edildi.
- [ ] Fikir onayı, görsel üretimi ve ikinci onay test edildi.
- [ ] İlk paylaşım önce test hesabında veya ileri tarihli planlamayla denendi.

---

**Bu depo genel amaçlı bir başlangıç projesidir.** Her markanın API erişimleri, Instagram hesap türü, görsel kimliği ve yayın kuralları farklı olabilir; canlı kullanımdan önce kendi ortamınızda uçtan uca test edin.
