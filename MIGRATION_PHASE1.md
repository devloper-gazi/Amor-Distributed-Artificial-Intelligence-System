# Phase 1 Optimizasyon — Geçiş Notu (Proje Sahibi İçin)

> **Sürüm aralığı:** `36126e2 .. 25e8c96` (4 commit)
> **Branş:** `v2`
> **Risk seviyesi:** Düşük (varsayılanlar muhafazakâr, fail-open kontratı, tüm public API şekilleri korundu)

---

## 1) Ne değişti?

Araştırma pipeline'ı `plan → gather → analyze → synthesize` akışına **iki yeni davranış** eklendi:

| # | Değişiklik | Amaç |
|---|---|---|
| 1 | `gather()` ile `analyze()` arasına **deterministik pre-LLM relevance gate** eklendi (yeni `document_processor/research/relevance.py`) | LLM'e gitmeden önce alakasız kaynaklar (404, login wall, off-topic, tekrarlı URL, aynı domainin hâkimiyeti) düşürülür |
| 2 | `analyze()` döngüsü artık **bounded `asyncio.Semaphore`** ile paralel çalışıyor | Tek-thread'li `for` yerine tier başına 1–3 concurrent LLM çağrısı (Ollama'yı ezmeden) |
| 3 | (Opt-in) `call_ollama` etrafına **SHA-256 anahtarlı Redis cache** wrapper eklendi | Aynı (model, system, prompt, max_tokens, temp) tuple'ı tekrar geldiğinde Ollama'ya hiç gitmez |

**Yeni dosyalar:**
- `document_processor/research/relevance.py` (~340 satır, sadece stdlib)
- `document_processor/tests/test_relevance.py` (11 test, tamamı offline)
- `document_processor/tests/conftest.py`

**Değişen dosyalar:**
- `document_processor/research/advanced_researcher.py` — `_apply_relevance_filter()` eklendi, `analyze()` concurrency'li
- `document_processor/config/settings.py` — 14 yeni pydantic-settings flag'i
- `document_processor/api/local_ai_routes_simple.py` — cache wrapper, `_OLLAMA_TEMPERATURE` constant
- `document_processor/research/__init__.py` — yeni public sınıflar export edildi

**Public API'da değişiklik YOK:** `AdvancedResearcher.run()` aynı dict'i döndürüyor (`citations`, `report_markdown`, `confidence`, `phases`, vs). Yeni `relevance_filter` SSE event'i eklendi (additif, frontend forward-compatible).

---

## 2) Tamamen nasıl devre dışı bırakılır?

Tek bir env değişkeni:

```yaml
# docker-compose.yml app servisinde
environment:
  - ENABLE_RELEVANCE_PREFILTER=false
```

Ya da `.env` dosyasında:

```bash
ENABLE_RELEVANCE_PREFILTER=false
```

Bu, filter'ı bypass eder ve davranış **Phase 1 öncesiyle birebir aynı** olur. `analyze()` concurrency açık kalır (yine de %2-3× hız avantajı var) — onu da kapatmak için:

```bash
ANALYZE_CONCURRENCY_BASIC=1
ANALYZE_CONCURRENCY_MEDIUM=1
ANALYZE_CONCURRENCY_DEEP=1
ANALYZE_CONCURRENCY_EXPERT=1
ANALYZE_CONCURRENCY_ULTRA=1
```

---

## 3) Tüm config flag'leri

Hepsi `pydantic-settings` üzerinden — varsayılanları `document_processor/config/settings.py`'de, env adı `UPPER_CASE`.

### Relevance filter

| Env değişkeni | Tip | Varsayılan | Anlamı |
|---|---|---|---|
| `ENABLE_RELEVANCE_PREFILTER` | bool | **`true`** | Filter'ı tamamen aç/kapat |
| `RELEVANCE_PREFILTER_FAIL_OPEN` | bool | `true` | Filter exception fırlatırsa orijinal listeyi geri ver (tavsiye: bırakın) |
| `RELEVANCE_PREFILTER_DEBUG` | bool | `false` | Her source için debug skor breakdown'u snapshot'a yazılır (ultra'da bellek için kapalı tutun) |
| `RELEVANCE_PREFILTER_MIN_SCORE` | float (0–1) | `0.15` | Bu skorun altındaki kaynaklar düşürülür |

### Per-tier kaynak cap'leri (filter sonrası tutulan max kaynak sayısı)

| Env değişkeni | Varsayılan | Mevcut LLM-survival sayısı (~) |
|---|---|---|
| `RELEVANCE_PREFILTER_MAX_SOURCES_BASIC`  | `8`   | 8 (passthrough) |
| `RELEVANCE_PREFILTER_MAX_SOURCES_MEDIUM` | `25`  | ~9 |
| `RELEVANCE_PREFILTER_MAX_SOURCES_DEEP`   | `60`  | ~25 |
| `RELEVANCE_PREFILTER_MAX_SOURCES_EXPERT` | `100` | ~50 |
| `RELEVANCE_PREFILTER_MAX_SOURCES_ULTRA`  | `120` | ~11 (gerçek run) |

> Cap'ler bilinçli olarak **bugünkü efektif LLM-survival sayısından yüksek** tutuldu — filter "iyi içeriği kaybediyor" suçlaması alamaz.

### Per-tier analyze concurrency

| Env değişkeni | Varsayılan | Hard ceiling |
|---|---|---|
| `ANALYZE_CONCURRENCY_BASIC`  | `1` | 8 |
| `ANALYZE_CONCURRENCY_MEDIUM` | `2` | 8 |
| `ANALYZE_CONCURRENCY_DEEP`   | `2` | 8 |
| `ANALYZE_CONCURRENCY_EXPERT` | `3` | 8 |
| `ANALYZE_CONCURRENCY_ULTRA`  | `3` | 8 |

> 8'lik tavan kod içinde sabittir (`_analyze_concurrency_for` clamp). Bunun üstüne çıkamazsınız — Ollama'yı korumak için.

### Opsiyonel LLM response cache (Phase 5)

| Env değişkeni | Tip | Varsayılan | Anlamı |
|---|---|---|---|
| `LLM_RESPONSE_CACHE_ENABLED` | bool | **`false`** | Aynı prompt'lar Redis'ten servis edilsin |
| `LLM_RESPONSE_CACHE_TTL_SECONDS` | int | `604800` (7 gün) | Cache entry yaşı |

Açmak için sadece `LLM_RESPONSE_CACHE_ENABLED=true` + restart. Cache key `sha256(json.dumps([model, system, prompt, max_tokens, temp]))`.

---

## 4) Eşikleri ayarlama (tuning playbook)

### "Çok fazla kaynak düşüyor, rapor yetersiz"
- `RELEVANCE_PREFILTER_MIN_SCORE=0.10` (varsayılan 0.15) → daha düşük eşik
- Veya tier cap'lerini büyüt: `RELEVANCE_PREFILTER_MAX_SOURCES_DEEP=100` gibi
- Veya `ENABLE_RELEVANCE_PREFILTER=false` → tamamen kapat

### "Yeterince agresif değil, hâlâ çok LLM çağrısı var"
- `RELEVANCE_PREFILTER_MIN_SCORE=0.20` → eşik yukarı
- Tier cap'lerini küçült (örn. `RELEVANCE_PREFILTER_MAX_SOURCES_ULTRA=60`)

### "Ollama çakılıyor / OOM"
- `ANALYZE_CONCURRENCY_DEEP=1`, `ANALYZE_CONCURRENCY_EXPERT=1`, `ANALYZE_CONCURRENCY_ULTRA=2` → paralelliği düşür
- Veya `OLLAMA_NUM_PARALLEL=1` (zaten docker-compose'da `OLLAMA_NUM_PARALLEL=2`)

### "Hata ayıklamak istiyorum, hangi kaynak hangi skoru aldı?"
- `RELEVANCE_PREFILTER_DEBUG=true` → her selected source için skor breakdown snapshot'a yazılır
- Restart sonrası: `docker exec amor-redis-1 redis-cli GET local_ai_research_session:<sid>` ile snapshot çekilir; `score_debug` field'ında her source için `{token, title, phrase, domain, content, bad_pen, dup_pen, overrep_pen, final}` görünür

### Cache'i daha kısa tut (LLM model güncellenecek)
```bash
LLM_RESPONSE_CACHE_TTL_SECONDS=86400   # 1 gün
```
Veya tamamen sıfırla:
```bash
docker exec amor-redis-1 sh -c "redis-cli --scan --pattern 'llm:*' | xargs -r redis-cli DEL"
```

---

## 5) Hangi log/metrikler izlenmeli?

### Container logları (`docker logs amor-app-1 amor-app-2`)

Filter çalıştığında her sub-Q × source'da bir kez şu satır gelir (WARN seviyesinde — proje root logger'ı container'da WARNING):

```
research.relevance_filter tier=deep original=80 selected=58 rejected=22 fallback=False method=lexical+domain v1, tier=deep, min=0.15, cap=60
```

**Anahtar göstergeler:**
- `original` vs `selected` oranı → filter'ın gerçekten ne kadar düşürdüğü
- `rejected=0` + `fallback=False` → filter aktif ama hiçbir şey düşürmedi (eşik çok düşük olabilir)
- `fallback=True` → filter exception fırlattı, fail-open devreye girdi (logger.warning'de exception izi olur)

### Per-source LLM hatası

```
analyze source 12 failed: <exception>
```

Tek source başarısız olunca diğerleri devam eder. Çok sayıda görüyorsanız Ollama timeout problemi var.

### SSE event'leri (frontend / `watch_live.py` / Redis snapshot)

Yeni event tipi:
```json
{
  "type": "relevance_filter",
  "tier": "deep",
  "original": 80,
  "selected": 58,
  "rejected": 22,
  "method": "lexical+domain v1, tier=deep, min=0.15, cap=60",
  "fallback_used": false
}
```

Mevcut `analyzing_source` event'i artık "completed counter" semantiği taşır (concurrent çalıştığı için strict iteration index değil).

### Önemli sayısal göstergeler

Bir önceki/sonraki run'ı karşılaştırırken bakılacaklar:
- **Ollama çağrı sayısı**: `docker logs amor-ollama | grep "POST.*generate" | wc -l` — Phase 1 öncesi vs sonrası %20–85 düşüş bekleniyor
- **Wall-clock süresi**: `started_at` vs `completed_at` snapshot field'ları
- **Confidence skoru**: `to_dict()['confidence']` — yeni log curve sayesinde 11 source'lu run bile 60+ alabiliyor
- **Final citation sayısı**: `len(citations)` — düşmüşse filter agresif demektir

### Cache hit oranı (sadece Phase 5 açıkken)

```bash
docker exec amor-redis-1 redis-cli --scan --pattern 'llm:*' | wc -l
```
İlk gün 0–10, hafta sonunda 100+ bekleniyor (tekrar eden prompt'lar olduğunda).

DEBUG log'unda görülür:
```
llm cache hit (len=2341)
```

---

## 6) Phase 2 için yapılacaklar (ileri seviye iyileştirmeler)

Aşağıdakiler mevcut planda **ertelenmiştir** — Phase 1 stabil olduktan sonra sırayla ele alınması önerilen yol haritası:

### Yüksek değer / orta risk

1. **Embedding tabanlı relevance** — `sentence-transformers/all-MiniLM-L6-v2` (80 MB, CPU'da ~10 ms/doc). Mevcut lexical scorer'a ek olarak semantic similarity skoru. Pre-filter'ın False Negative oranını ciddi düşürür.
2. **Map-reduce batched analyze** — Her source için ayrı LLM çağrısı yerine, sub-question başına bir LLM çağrısı (tüm source'ların context'i tek prompt'ta). Ultra'da 120 → 14 çağrı (~10× hız).
3. **Cross-encoder rerank** — Pre-LLM final selection'da `cross-encoder/ms-marco-MiniLM-L-6-v2` (90 MB, 30 ms/pair) ile top-K sıralaması.

### Orta değer / orta risk

4. **`httpx.AsyncClient` lifecycle refactor** — Şu an her `call_ollama` çağrısında yeni client yaratılıyor. FastAPI lifespan'a taşıma + reuse.
5. **`local_ai_routes_simple.py` modülerizasyonu** — 1700+ satır; `routes/` + `ollama_client.py` + `cache.py` olarak böl.
6. **Translation flag wiring fix** — `translated: False` her zaman dönüyor, root cause araştırılacak.
7. **SSE event replay buffer** — Cross-replica resilience için Redis Stream tabanlı per-event ID + replay.

### Mimari (uzun vadeli)

8. **Agent split** — `Planner / Scraper / Relevance / Extractor / Synthesizer` ayrı sınıflar; her biri kendi cache + retry policy.
9. **DuckDB / np.memmap** — RAG embeddings'i `List[List[float]]`'tan kalıcı kolonlu store'a taşıma.
10. **Multi-model ML stack** — `BART summarizer` (analyze yerine), `MNLI classifier` (zero-shot off-topic detection), `msmarco-distilbert` (query-doc matching).
11. **Native acceleration** — Tantivy BM25, SIMD cosine top-K, Rust readability extraction (pyo3).

### Kalan teknik borçlar (Phase 1 dışı, daha küçük)

- Logger seviyesi container'da WARNING — `logger.info` görünmez. Şimdilik kritik filter event'leri `logger.warning`'e çekildi; uzun vadede `logging.basicConfig(level=settings.log_level)` setup'ı düzeltilmeli.
- Frontend password validation kuralları backend ile tam senkron değil (backend artık tek doğru kaynak — frontend'in güncellemeye gerek yok ama uyarı mesajları aynılaştırılabilir).

---

## 7) Acil rollback prosedürü

Tüm Phase 1 commit'lerini bir kerede geri almak gerekirse:

```bash
git checkout v2
git revert --no-edit 25e8c96 112d9ce 226fd72 36126e2
git push origin v2
docker compose restart app
```

Daha hafif (sadece runtime kapatma, kod kalır):

```bash
# .env veya docker-compose.yml app servisi
ENABLE_RELEVANCE_PREFILTER=false
ANALYZE_CONCURRENCY_BASIC=1
ANALYZE_CONCURRENCY_MEDIUM=1
ANALYZE_CONCURRENCY_DEEP=1
ANALYZE_CONCURRENCY_EXPERT=1
ANALYZE_CONCURRENCY_ULTRA=1
LLM_RESPONSE_CACHE_ENABLED=false
docker compose restart app
```

Sonuç: davranış birebir Phase 1 öncesi.

---

## 8) Doğrulama (kabul kriterleri)

```bash
# Birim testler (offline, ~1.5 sn)
docker exec amor-app-1 python -m pytest \
    document_processor/tests/test_relevance.py -v
# Beklenen: 11 passed

# End-to-end smoke (medium tier, ~5–10 dk)
curl -X POST http://localhost:8000/api/local-ai/research \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"topic":"What is photosynthesis?","depth":"medium",
         "use_translation":false,"target_language":"en",
         "save_to_knowledge":false}'

# Container loglarında "research.relevance_filter" satırı görmeli
docker logs --tail 200 amor-app-1 amor-app-2 | grep relevance_filter

# Final raporda confidence > 50, citations > 0 olmalı
docker exec amor-redis-1 redis-cli GET local_ai_research_session:<sid>
```
