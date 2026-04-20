# Amor — v2.0.0 Sürüm Notları

> v1 (eski sürüm) `main` branch'inde aynen duruyor. v2 bağımsız bir branch / tag olarak yayınlandı; geri uyumluluğu bozan değişiklikler içerir.

---

## 🧠 v2.1 Güncellemesi — Thinking Mode (Clarify-First Derin Akıl Yürütme)

Bu güncelleme, Amor'un tasarımcı/mimar yetkinliklerini sıfırdan kuruyor. Karmaşık bir soruyla (mikroservis mimarisi, dağıtık sistem, yazılım altyapısı kurulumu vb.) gelindiğinde model **önce netleştirici sorular sorar, cevabı aldıktan sonra** altı fazlı bir pipeline üzerinden derin akıl yürütmeyi çalıştırır. Hem **backend** hem **UI/UX** production-grade şekilde tasarlandı.

### 🆕 Yeni Özellikler

- **Clarify-first human-in-the-loop akışı**
  - Kullanıcı promptu önce `/api/thinking/analyze` üzerinden karmaşıklık (`trivial / moderate / complex / expert`) ve deliverable türü (`plan / architecture / code / analysis / decision / explanation`) açısından sınıflandırılıyor.
  - Gerektiğinde en fazla 5 netleştirici soru üretiliyor: her soru için `question`, `why_it_matters`, `suggestions` (hazır öneri çipleri) ve `input_type` (text / choice / number / multiline) alanları var.
  - Kullanıcı isterse "Skip — proceed with assumptions" diyerek sorular olmadan devam edebiliyor.

- **Altı fazlı akıl yürütme pipeline'ı** (`ThinkingEngine`)
  1. **Understand** — prompt'un yeniden ifadesi + kısıtlar + varsayımlar + bilinmeyenler
  2. **Decompose** — 3–7 alt soruya parçalama
  3. **Explore** — 2–4 alternatif yaklaşım (pros/cons/risk/effort ile birlikte)
  4. **Evaluate & decide** — gerekçeli seçim, güven skoru (0–1), trade-off'lar
  5. **Synthesize** — nihai Markdown deliverable (mimari diyagramlar, kod blokları, kademeli plan)
  6. **Critique** — kendi çıktısına karşı red-team: riskler (low/medium/high), açık sorular, sonraki adımlar

- **Effort ayarı** — Her faz için ayrı token bütçesi: `quick` / `standard` / `deep`. Deep mode'da her faz için 2500+ token ayrılıyor.

- **Dayanıklı JSON parse'ı** — LLM çıktılarındaki markdown fence'leri, prose sarmalamaları ve trailing comma'lar 3 aşamalı fallback ile temizleniyor (direct → fenced block → balanced-brace).

- **Sağlayıcı seçimi** — Default olarak yerel Ollama (qwen2.5:7b); istenirse Claude 3.5 Sonnet sağlayıcısı kullanılabilir.

### 🎨 Yeni UI/UX — `ThinkingView`

- **İki durumlu kart** (tek komponent): `questioning` → `thinking`.
- **Clarification formu:** numaralandırılmış sorular, "why it matters" açıklaması, tıklanabilir öneri çipleri, "Skip" butonu.
- **Canlı timeline:** 6 fazlı pipeline grid'i; her fazın durumu (`pending / in_progress / completed / failed / skipped`) renkli ikonla gösteriliyor.
- **Artifact kartları:**
  - *Understanding* — yeniden ifade + kısıtlar + varsayımlar + açık sorular
  - *Sub-questions* — numaralı alt sorular
  - *Alternatives* — her alternatif için pros/cons/risk/effort; **chosen** olan ring ile vurgulanıyor
  - *Decision* — seçim gerekçesi + animasyonlu **confidence bar**
  - *Deliverable* — yerleşik güvenli Markdown renderer (headings, code blocks, lists, bold/italic)
  - *Self-critique* — severity'ye göre renkli kenarlıklı risk kartları (high / medium / low)
- **Monokrom tema** — mevcut `tokens.css` üzerinden; tam light + dark mode desteği.
- **Snapshot persistence** — Thinking kartları sohbet geçmişinde JSON snapshot olarak saklanıyor; sayfayı yenileyince bire bir geri yükleniyor.

### 🔌 Yeni API Endpoints (`/api/thinking`)

| Metot | Endpoint | Amaç |
|-------|----------|------|
| POST  | `/api/thinking/analyze` | Promptu sınıflandır + netleştirici soruları üret |
| POST  | `/api/thinking/think` | Pipeline session'ı başlat (background task) |
| GET   | `/api/thinking/{sid}/events` | Canlı SSE stream — phase_start / phase_complete / deliverable_ready / done |
| GET   | `/api/thinking/{sid}/status` | Polling fallback |
| GET   | `/api/thinking/{sid}` | Alias |

- Tüm endpoint'ler JWT gerektiriyor; session'lar **kullanıcı bazında izole** (başka kullanıcı 404 alır).
- Redis'te TTL'li persist (`THINKING_SESSION_TTL_SECONDS`, default 7200).
- SSE `?access_token=<jwt>` query-param auth fallback'i (EventSource için).

### ⚙️ Yeni Konfigürasyon

```env
# Opsiyonel — thinking session saklama süresi (saniye, default 7200)
THINKING_SESSION_TTL_SECONDS=7200
```

### 🗂️ Eklenen / Değişen Dosyalar

**Backend (yeni):**
- `document_processor/thinking/__init__.py`
- `document_processor/thinking/models.py` — Pydantic v2 şemaları
- `document_processor/thinking/prompts.py` — 6 faz için senior-engineer prompt builder'ları
- `document_processor/thinking/engine.py` — `ThinkingEngine` orchestrator
- `document_processor/api/thinking_routes.py` — FastAPI router

**Backend (değişen):**
- `document_processor/main.py` — conditional import + `THINKING_AVAILABLE` flag'i + router registration

**Frontend (yeni):**
- `web_ui/static/js/thinking-view.js` — ~430 satır `ThinkingView` komponenti
- `web_ui/static/css/thinking-view.css` — ~550 satır monokrom stil

**Frontend (değişen):**
- `web_ui/static/js/chat-research.js` — `thinkingWithLocalAI()` + `_askClarifications()` + `_streamThinking()` + `_pollThinking()` + snapshot restoration
- `web_ui/templates/index.html` — thinking-view CSS + JS dahil edildi

**Dokümantasyon:**
- `README.md` — yeni Thinking Mode ekran alıntısı
- `docs/screenshots/website-thinking.png` — yeni

### ✅ End-to-End Test

Canlı ortamda doğrulanmış akış:
- `POST /analyze` → "Design a scalable real-time chat system" promptuna karşılık 2–3 clarifying soru üretti (`complexity=moderate, deliverable=architecture`).
- `POST /think` → session açıldı, fazlar sırayla ilerledi (`understand ✓ → decompose ✓ → explore → evaluate → synthesize → critique`).
- UI kartı her fazı canlı gösterdi; snapshot yenilemeden sonra geri yüklendi.

---

## 🆕 v2.0.0 Öne Çıkanlar

### 1. Tam Bir Kullanıcı Hesap Sistemi
Amor artık çok kullanıcılı. Her kullanıcı kendi hesabıyla giriş yapıp sadece kendi geçmişini görür.

- **Kayıt / Giriş** — Sadece kullanıcı adı + parola (e-posta zorunlu değil). Hafif, sürtünmesiz onboarding.
- **Argon2id parola hashleme** — `time_cost=3`, `memory=64 MiB`, `parallelism=4` (OWASP 2024 önerisi).
- **JWT access token (HS256, 15 dk TTL)** + **opak refresh token** (SHA-256 hash'li, `httpOnly` cookie içinde `/api/auth` path'ine scope'lanmış).
- **Refresh rotation + replay detection** — Aynı refresh token ikinci kez kullanılırsa tüm aile (family) iptal edilir.
- **Account lockout** — 8 başarısız girişten sonra 15 dk kilit; IP bazlı rate limit (`login_attempts` tablosu).
- **Citext e-posta** (opsiyonel), UUID birincil anahtarlar, PostgreSQL `pgcrypto` + `citext` extension'larıyla.
- **Yeni endpoint'ler:**
  - `POST /api/auth/register` · `POST /api/auth/login`
  - `POST /api/auth/refresh` (cookie tabanlı) · `POST /api/auth/logout` · `POST /api/auth/logout-all`
  - `GET  /api/auth/me`

### 2. Kullanıcı Başına Veri İzolasyonu
- Her araştırma, sohbet oturumu ve klasör `user_id` ile etiketleniyor.
- Backend'de sahip doğrulaması: A kullanıcısı B'nin oturumuna erişemez (404 döner).
- `get_current_user` dependency tüm kritik route'lara uygulandı.

### 3. Sıfırdan Yenilenmiş Auth UI/UX
- İki ayrı görünüm (sekme yerine kayan geçiş): Sign in ↔ Create account.
- Mevcut monokrom tasarımla tam uyumlu (`tokens.css` değişkenleri üzerinden).
- Üst bardaki kullanıcı rozeti (avatar + display name) dropdown menüsü ile:
  - "Log out of this device" (bu cihazdan çık)
  - "Sign out everywhere" (tüm cihazlardan çık — tüm refresh token'ları iptal eder)
- Access token yalnızca bellekte tutulur (XSS exfiltrasyonuna karşı); refresh token sadece `httpOnly` cookie'de.
- Silent refresh (expire olmadan 30 sn önce) + 401 otomatik retry.

### 4. Araştırma Sistemi — Geliştirildi ve Kusursuzlaştırıldı
- **Canlı SSE streaming** — `EventSource` üzerinden adım adım research kartı; kaynaklar, bulgular, analiz, özet akarak geliyor.
- **Auth-aware EventSource** — Native `EventSource` custom header taşıyamadığı için backend artık `?access_token=...` query param'ını da kabul ediyor (fallback chain: `Authorization: Bearer` → `X-Access-Token` → `?access_token`).
- **`ResearchView` snapshot sistemi** — Araştırma tamamlandıktan sonra tüm durum snapshot'a kaydediliyor; sohbet geçmişi yüklendiğinde araştırma kartı birebir geri yükleniyor.
- **Gelişmiş polling fallback** — SSE bağlantısı düşerse otomatik polling'e geçiş, 30 dk'ya kadar dayanabilir.
- Yeni `document_processor/research/advanced_researcher.py` — çoklu ajan orkestrasyonu ile daha derin araştırma.
- Frontend:
  - `web_ui/static/js/research-view.js` — stand-alone research card component.
  - `web_ui/static/css/research-view.css` — monokrom tasarımla uyumlu kart stili.
- **Kimliği doğrulanmış fetch her yerde** — Tüm research/chat API çağrıları `window.amorAuth.fetch` üzerinden geçiyor; 401 durumunda otomatik refresh + retry.

### 5. Chat / Oturum Yönetiminde İyileştirmeler
- Her oturum artık oluşturan kullanıcıya bağlı.
- Klasörler (chat folders) kullanıcı bazlı izole.
- Message persist çağrıları artık JWT taşıyor.

## 🔧 Teknik Değişiklikler

### Yeni Dosyalar
```
document_processor/auth/__init__.py
document_processor/auth/models.py             # Pydantic modelleri (RegisterRequest, LoginRequest, User, AuthTokens...)
document_processor/auth/service.py            # Argon2 + JWT + refresh rotation servisi
document_processor/auth/dependencies.py       # FastAPI dependency'leri (get_current_user, get_optional_user)
document_processor/api/auth_routes.py         # /api/auth/* route'ları
document_processor/migrations/002_users.sql   # users, refresh_tokens, login_attempts tabloları
document_processor/research/__init__.py
document_processor/research/advanced_researcher.py

web_ui/static/js/auth.js                      # Client auth layer + overlay
web_ui/static/js/auth-chip.js                 # Top-bar kullanıcı rozeti
web_ui/static/js/research-view.js             # Canlı research kartı
web_ui/static/css/auth.css                    # Monokrom auth UI
web_ui/static/css/research-view.css
```

### Değiştirilen Dosyalar
```
document_processor/api/chat_folders_routes.py    # user scoping
document_processor/api/chat_sessions_routes.py   # user scoping
document_processor/api/local_ai_routes_simple.py # user scoping + SSE auth
document_processor/config/settings.py            # JWT secret, cookie ayarları, TTL
document_processor/main.py                       # auth router + lifespan bootstrap
web_ui/static/js/app.js                          # apiFetch → amorAuth.fetch
web_ui/static/js/chat-research.js                # tüm fetch çağrıları auth-aware
web_ui/templates/index.html                      # auth.js + auth-chip.js script tag'leri
requirements.txt                                 # argon2-cffi, PyJWT, sqlalchemy[asyncio], asyncpg
```

## 🧱 Yeni Veritabanı Şeması (migrations/002_users.sql)
- `users` — UUID PK, citext username/email, Argon2 hash, `failed_attempts`, `locked_until`, soft-disable.
- `refresh_tokens` — token_hash (SHA-256), family_id (rotation grubu), `revoked_at`, `replaced_by`, IP/UA metadata.
- `login_attempts` — IP + identifier kombinasyonu, rate limit için.

## 🔐 Yeni Ortam Değişkenleri (`.env`)
```env
AUTH_JWT_SECRET=<256-bit random>           # Boş bırakılırsa servis açılmaz
AUTH_ACCESS_TOKEN_TTL_MINUTES=15
AUTH_REFRESH_TOKEN_TTL_DAYS=30
AUTH_COOKIE_NAME=amor_rt
AUTH_COOKIE_SECURE=true                    # Prod'da mutlaka true
AUTH_COOKIE_SAMESITE=lax
AUTH_LOGIN_MAX_ATTEMPTS=8
AUTH_LOGIN_LOCKOUT_MINUTES=15
```
`.env.example` içine örnekleri eklemeyi unutmayın.

## 💥 Breaking Changes (v1 → v2 Göç Notları)
- Tüm `/api/local-ai/*`, `/api/chat/*`, `/api/sessions/*` route'ları artık **kimlik doğrulama gerektiriyor**. Anonim istemciler `401` alır.
- Veritabanı migration'ı gerekli: `002_users.sql` ilk açılışta otomatik çalışır (her DDL bağımsız transaction'da çalışır, çoklu replica race'i güvenli).
- `.env` içinde `AUTH_JWT_SECRET` yoksa servis başlatılmaz.
- Mevcut oturum/folder/research kayıtları `user_id=NULL` ile kalır; v1'den göç edeceklerin onları yeni bir admin kullanıcıya atamak için manuel `UPDATE` çalıştırması gerekir.

## 🧪 Doğrulandı
- Argon2id + JWT + rotating refresh token + per-user isolation: curl ile uçtan uca test edildi.
- Cross-user isolation: user2, user1'in research session'ına 404 aldı ✓
- SSE query-param auth: `?access_token=...` → 200, header yok → 401 ✓
- Multi-replica (amor-app-1, amor-app-2) Docker Compose deploy doğrulandı.

## 🚀 Kurulum
```bash
git clone -b v2 https://github.com/devloper-gazi/Amor-Distributed-Artificial-Intelligence-System.git
cd Amor-Distributed-Artificial-Intelligence-System
cp .env.example .env
# .env içinde AUTH_JWT_SECRET oluştur:  openssl rand -hex 32
docker compose up -d
```

---
**v1 hâlâ `main` branch'inde erişilebilir** — geri dönmek isterseniz `git checkout main`.
