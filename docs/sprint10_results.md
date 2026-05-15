# Sprint 10 — i18n Turkish + locale-aware everything

> Cycle C, Days 1–5.  Closed 2026-05-04.

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | Frontend i18n primitive + Settings localized.  Homegrown ~190 LOC `t()` / `locale` signal / `setLocale()` / `formatDate` / `formatNumber` / `formatRelative` / `plural` / `normalizeTurkish`.  No new deps. | `web_ui/v2/src/i18n/{index.ts,en.ts,tr.ts}`, `web_ui/v2/src/routes/Settings.tsx`, 19 vitest tests |
| 2 | Chrome strings (mode labels, Sidebar, CommandPalette, ConnectionBanner, UnifiedComposer, Chat).  ~38 new keys × 2 locales. | `web_ui/v2/src/components/shell/{Sidebar,CommandPalette,ConnectionBanner}.tsx`, `web_ui/v2/src/components/chat/UnifiedComposer.tsx`, `Chat.tsx`; 4 new vitest tests for `modeLabel`/`modeSubtitle` |
| 3 | Chat surface + admin route strings (MessageActions, MessageBubble's "Remembered" pill, ToolCallCard, Memory, Training, Agent, Diagnostics).  ~100+ keys × 2 locales. | `MessageActions.tsx`, `MessageBubble.tsx`, `ToolCallCard.tsx`, `routes/{Memory,Training,Agent,Diagnostics}.tsx` |
| 4 | Backend i18n module + RFC-7231 `Accept-Language` parser + `get_locale` FastAPI dep + `localized_http_exception()` helper.  Wired into `repo_routes`, `admin_memory_routes`, `admin_training_routes`, `agent_routes` — every `HTTPException.detail` now respects Accept-Language. | `document_processor/i18n/{__init__.py,messages.py}`, all four route modules, 21 pytest tests |
| 5 | Cross-sprint sweep + `sprint10_results.md` + bundle gate | this file |

## Acceptance criteria — pass/fail

* **`@solid-primitives/i18n` adopted** — _replaced_ with a 190-LOC
  homegrown primitive.  Same surface area, zero new deps; bundle
  delta on Day 1 was +5.99 kB gzipped.
* **All UI strings extracted to `frontend/src/i18n/{en,tr}.ts`** —
  **PASS** for the user-visible chrome (Settings, Sidebar, palette,
  composer, message actions, tool cards) + every Sprint 6/7/8/9
  route.  Per-mode legacy routes (`Build.tsx`, `Research.tsx`,
  `Thinking.tsx`, etc.) still hold ad-hoc literals — they keep
  English defaults via the `t()` fallback, no regression.
* **`Intl.DateTimeFormat` / `Intl.NumberFormat` for all dates and
  numbers** — **PASS** via `formatDate` / `formatNumber` /
  `formatRelative`; verified by tests including the
  Turkish-decimal-separator round-trip
  (`1234.5` → `"1,234.5"` en vs `"1.234,5"` tr).
* **Backend Accept-Language for system messages and errors** —
  **PASS** for the four most-touched routers (live verified:
  same endpoint with `Accept-Language: en` vs `tr` returns
  matching translated `detail`).
* **UI selector in Settings** — **PASS**; persists to
  `localStorage["amor.locale"]` and broadcasts via the Solid
  signal.
* **Turkish dotted/dotless i normalization in search/sort** —
  **PASS** (`normalizeTurkish` collapses `İ`/`I`/`ı`/`i` to a
  canonical `i` baseline; 4 tests pin the contract).

## Frontend surface

* New module: `web_ui/v2/src/i18n/index.ts` (~190 LOC)
  * `Locale = "en" | "tr"`
  * `locale` (Solid signal), `setLocale(next)`, `getSupportedLocales()`
  * `t(key, params?)` — interpolates `{{name}}`, falls back en→key
  * `useT()` — closure-form for prop drilling
  * `plural(n, forms)` — `Intl.PluralRules` per active locale
  * `formatDate / formatNumber / formatRelative`
  * `modeLabel(meta)` / `modeSubtitle(meta)` — works on raw key or
    `ModeMeta` object; falls back to the object's English label
  * `normalizeTurkish(text)` — fold `İ`/`I`/`ı`/`i` → `i`, drop
    combining-dot artefacts
  * `resetLocale()` test hook

* Tables: `en.ts` + `tr.ts`, both ~140 keys, in lockstep.

* 79 vitest tests (was 56 pre-Sprint-10 → +23 in Sprint 10):
  * 19 i18n primitive (translator, locale signal, plural,
    formatters, dotted-i normalization)
  * 4 mode helper (locale switch, ModeMeta object input,
    fallbacks)
  * 56 carried over from prior sprints (unchanged contracts)

## Backend surface

* New module: `document_processor/i18n/{__init__.py,messages.py}`
  (~330 LOC including catalogues)
  * `t(key, locale, **params)` — same interpolation semantics as
    the frontend
  * `parse_accept_language(header)` — RFC-7231 parser with q-value
    sort + first-occurrence tiebreak; falls back to `"en"`
  * `get_locale(request)` — FastAPI dep; resolution order:
    `X-AMOR-Locale` header → `amor.locale` cookie →
    `Accept-Language` → `"en"`
  * `localized_http_exception(...)` — emits an
    `HTTPException(detail=t(...))` so every route stays one line
    per error site

* Catalogues:
  * `common.*` (auth_required, db_unavailable, not_found, …)
  * `training.*` (Sprint 6 routes)
  * `memory.*` (Sprint 7 routes)
  * `agent.*` (Sprint 8 routes)
  * `stream.*` (Sprint 9 cross-replica)
  * `repo.*` (Sprint 4 Day 2 symbol search)

* Migrated routers: `repo_routes`, `admin_memory_routes`,
  `admin_training_routes`, `agent_routes`.  Every
  `raise HTTPException(...)` site now reads the locale from the
  request + emits a translated detail.

## Tests

```
$ pytest tests/local_ai/test_backend_i18n.py
21 passed   (translator + parser + cookie + header + integration)

$ pytest tests/local_ai/ tests/api/ tests/code_intelligence/test_sandbox_security_posture.py tests/training/
158 passed

$ npx vitest run
Tests: 79 passed (5 → 6 test files)
```

Cross-sprint backend sweep: **158 passed** (was 137 pre-Sprint-10
→ +21 backend i18n tests, the rest unchanged).
Frontend sweep: **79 passed** (was 56 → +23 i18n tests).

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 107.58 kB  delta: +11.38 kB (budget: +40.00 kB)
[bundle-size] OK
```

Sprint 4–10 cumulative delta is **+11.38 kB / +40 kB budget**
(28% used; 72% headroom).  Backend has no bundle to gate; the
i18n catalogue weighs ~5 kB on disk.

## Live verification

Same endpoint, two locales:

```
$ curl -X POST -H "Accept-Language: en" .../api/admin/memory/add -d '{"text":"x"}'
{"detail":"memory backend not available — set AMOR_MEMORY_BACKEND=mem0"}

$ curl -X POST -H "Accept-Language: tr" .../api/admin/memory/add -d '{"text":"x"}'
{"detail":"bellek backend'i kullanılamıyor — AMOR_MEMORY_BACKEND=mem0 ayarlayın"}

$ curl -X POST -H "Accept-Language: tr" .../api/admin/training/runs/<missing>/promote
{"detail":"çalışma bulunamadı"}
```

Settings → Türkçe seçimi anında bütün chrome'u Türkçeleştirir:
- Sidebar: "Modlar / Sistem / Ayarlar / Temeller / LLM / Değerlendirmeler"
- Mode pill: "Araştırma / İnşa / Düşünme / Konsorsiyum / Bekçi / Sistem"
- CommandPalette: "Bir komut yaz… / Eşleşme yok. / gez / seç / kapat"
- MessageActions: "Mesajı kopyala / Yanıtı yeniden üret / Bu mesajdan dallanma aç / Bu cevap iyi/zayıf"
- ToolCallCard: "bekliyor / çalışıyor / tamamlandı / hata · girdi / çıktı / tekrar N"
- Memory / Training / Agent / Diagnostics: TopBar başlıkları, statları,
  buton metinleri, error banner'ları hepsi yerelleşir

## Caveats

* **Per-mode legacy routes** (Build/Research/Thinking/Consortium/
  Sentinel) still ship hardcoded English headings.  Their content
  is mode-specific and a future sprint can migrate them in batch
  using the same pattern Sprint 10 Day 3 used for the admin routes.
  The fallback chain (`t()` returns the literal key when missing)
  means there's no broken UI today — just untranslated panels.
* **Pluralization** is wired but only used in a handful of places
  (e.g. "Remembered N").  Most existing strings are written
  amount-agnostic ("X pairs" works for any X in both en and tr).
* **Backend Accept-Language migration is partial** — the four
  most-touched routers were migrated; older routers
  (`code_intelligence_routes`, `chat_research_routes`,
  `auth_routes`, …) still emit English `detail` strings.  Same
  pattern applies; future sprint can batch-convert when needed.
* **Mem0 fact-extraction LLM** (Sprint 7) doesn't honour the
  per-request locale yet — facts are extracted in the model's
  natural output language, which is usually English.  Sprint 10
  is i18n for AMOR's *own* surfaces; LLM output i18n is its own
  problem (system prompts, decoding params).

## Rollback

* **Disable frontend i18n entirely**: revert
  `web_ui/v2/src/i18n/index.ts` to a constant English locale —
  `t()` becomes `(key) => en[key] ?? key`.  Every consumer keeps
  working because they call `t()` not `tr[key]` directly.
* **Disable backend Accept-Language**: revert the
  `localized_http_exception` calls in the four routers to plain
  `HTTPException(detail="...")`.  No DB / wire-format changes.
* **Force a single locale**: ship `AMOR_FORCE_LOCALE=en|tr` env
  var.  Frontend `setLocale` becomes a no-op; backend
  `get_locale` returns the env value.  Two-line patch in each
  module; not implemented today since the per-user pattern is
  the right default.

## Operator how-to

```bash
# 1. User-side (frontend): visit /settings → Dil → "Türkçe".
#    Persists in localStorage["amor.locale"] and broadcasts via
#    Solid signal.  Works offline.

# 2. API-side (backend): include Accept-Language header in every
#    fetch.  The frontend does NOT send this today; it relies on
#    the cookie pattern instead.  Operators wiring an external
#    integration can do either:
curl -H "Accept-Language: tr" .../api/admin/training/runs
# or:
curl --cookie "amor.locale=tr" .../api/admin/training/runs

# 3. Override per-call: explicit X-AMOR-Locale header beats both.
curl -H "X-AMOR-Locale: tr" .../...
```

## Bu sprint sayesinde

Sprint 10 closed Cycle C's i18n acceptance gate.  AMOR's chrome,
chat surface, admin tooling, and HTTP errors all speak Turkish or
English based on the user's choice — no third-party dep, no
runtime locale loading, no mid-flight wire format change.  The
foundation exists for additional locales (e.g. Spanish, Japanese)
by adding a single `tables/<locale>.ts` + `messages.<locale>.py`
file each.
