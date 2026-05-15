# Sprint 0 v18 — Baseline Runbook

> Cycle E v18 baseline yenileme adımları.  Pazartesi gece bırakıp
> Salı sabah analiz etmek üzere tasarlandı.  Tüm komutlar repo
> kökünden çalıştırılır.

## Charter (özet)

- **Judge primary**: Mistral-Small-3-24B-Instruct (Q4_K_M, CPU)
- **Judge fallback**: Phi-4-14B-Instruct (Q4_K_M, CPU)
- **Protocol**: position-swap A/B → B/A + 2-rubric (correctness 1-5,
  completeness 1-5) + uncertainty flag (>1 pt delta)
- **Corpus**: 10 prompt × 6 mode coverage (Sprint 0 charter)
- **Beklenen wall**: Mistral ~1-2 saat/pass, Phi-4 ~30 dk/pass
- **Sonuç**: `data/baselines/sprint0_latest.json` + JSONL trace

## Pre-flight (bir kez, her judge için)

### Mistral-Small-3 (default)

```bash
docker exec amor-app-1 sh -c '
  mkdir -p /data/custom_models/judge && cd /data/custom_models/judge && \
  hf download bartowski/Mistral-Small-24B-Instruct-2501-GGUF \
    --include "*Q4_K_M*" --local-dir .
'
```

Disk: ~14.3 GB.  Volume `amor_custom-models-data` içine düşer.

### Phi-4 (fallback)

```bash
docker exec amor-app-1 sh -c '
  mkdir -p /data/custom_models/judge && cd /data/custom_models/judge && \
  hf download bartowski/phi-4-GGUF \
    --include "*Q4_K_M*" --local-dir .
'
```

Disk: ~9.1 GB.

### Auth

`amor-baseline-runner` kullanıcısının şifresini env-var olarak set et
(uzun-ömürlü token rotasyon riskine karşı):

```bash
export AMOR_BASELINE_USERNAME=amor-baseline-runner
export AMOR_BASELINE_PASSWORD='<vault-secret>'
```

## Pazartesi gece — overnight run

### Default (Mistral-Small-3)

```bash
nohup tools/run_sprint0_v18.sh > /tmp/sprint0_v18.log 2>&1 &
echo "PID=$!"
```

Wait ~6 saat (3 pass × ~2 saat).

### Phi-4 fallback

Ya host RAM 16 GB Mistral'ı kaldıramıyorsa, ya da gece yerine bir
çalışma günü içinde bitmeli:

```bash
AMOR_SPRINT0_JUDGE=phi4 nohup tools/run_sprint0_v18.sh \
  > /tmp/sprint0_v18.log 2>&1 &
```

Wait ~1.5 saat.

## Salı sabah — analiz

### 1. Run özetine bak

```bash
tail -40 data/baselines/v18_*.log | head -60
```

Beklenen son birkaç satırda:

```
Sprint 0 v18 baseline run complete
  profile : mistral
  exit    : 0
  wall    : 21438s
  log     : data/baselines/v18_<ts>.log
  result  : data/baselines/sprint0_latest.json

Judge summary:
  rows       : 10
  judged     : 10
  uncertain  : 0
  errored    : 0
  correctness: mean=3.20 stdev=0.79 range=[2..5]
  completeness: mean=3.10 stdev=0.74 range=[2..4]
```

### 2. /admin/baselines'de görsel doğrulama

`http://localhost:8000/admin/baselines` (Türkçe: `/admin/temeller`) →
10 prompt, her biri "completed" durumda, judge sütunu doluyor (X/Y
formatında).

### 3. Kalite kontrolleri

- **Uncertainty rate** ≤ 2/10 ise judge tutarlı.  > 2 ise position-swap
  protokolünü tekrar çalıştır (`tools/run_sprint0_baseline.py
  --rejudge`).
- **Errored rows** = 0 olmalı.  > 0 ise log'ta `judge_score.error`
  satırını oku.
- **Mean correctness** ≥ 3.0 ise baseline OK.  < 3.0 → planlanmış
  improvement claim'lerinin baseline'ı zayıf, AMOR architect modeli +
  prompt'larını gözden geçir.

### 4. Promote to v18 baseline of record

Tatmin ediciyse:

```bash
cp data/baselines/sprint0_latest.json \
   data/baselines/sprint0_v18_baseline_of_record.json
git add data/baselines/sprint0_v18_baseline_of_record.json
git commit -m "Sprint 0 v18 baseline of record (Mistral-Small-3 judge)"
```

Bu Sprint 1-12'nin "improvement on" referansı olur.

## Failure modes + recovery

### Judge container 90s'de açılmıyor

```bash
docker logs amor-judge | tail -30
```

Genellikle:
- GGUF volume'da yok → re-download
- llama-server image'ı eskimiş → `docker pull
  ghcr.io/ggml-org/llama.cpp:server`
- RAM yetersiz → `phi4` profile'a geç

### Judge timeout (>240s per call)

```bash
AMOR_BASELINE_JUDGE_TIMEOUT_S=900 tools/run_sprint0_v18.sh
```

veya profile'dan otomatik (`request_timeout_s` profile'da set edilmiş).

### Tek bir prompt fail oldu, kalan 9 OK

```bash
tools/run_sprint0_baseline.py \
  --only build-snake-html \
  --judge-profile mistral \
  --judge-url http://localhost:9101
```

### Judge model hatalı çıktı verdi (uncertain çok yüksek)

Phi-4 ile bağımsız doğrulama:

```bash
AMOR_SPRINT0_JUDGE=phi4 tools/run_sprint0_baseline.py --rejudge
```

İki judge'ın mean'leri ±0.3 içinde uyuşuyorsa baseline OK.  Diverge
ediyorsa critic protokolüne ek bir tie-break judge eklemek gerekir
(LLM-as-jury, post-Sprint-0 work).

## Ürettiği artefactlar (commit edilebilir)

- `data/baselines/sprint0_latest.json` — son baseline snapshot
- `data/baselines/sprint0_<utc>.jsonl` — per-prompt JSONL trace
- `data/baselines/v18_<ts>.log` — run log
- `data/baselines/sprint0_v18_baseline_of_record.json` — promote
  edilmiş v18 baseline (Sprint 1-12 referansı)
