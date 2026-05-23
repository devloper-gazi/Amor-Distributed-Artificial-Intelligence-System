# Sprint 6 v18 — Async pipeline + weekly ORPO cron + v18 launch gate

> Cycle F Sprint 6 — three landed pieces:
>   1. async pipeline parallelization (`code_pipeline_parallel=True`)
>   2. critic prefix-warmup (`code_critic_prefix_warmup=True`)
>   3. weekly ORPO LoRA training cron (`tools/training/orpo_weekly_cron.py`)
>
> **v18.1 Step 2 (2026-05-15)**: extended the weekly cron with a
> **Step 0 Postgres → JSONL bridge** that consumes accumulated
> MessageActions ratings into `data/preference_pairs/build.jsonl`.
> Closes the v18 carry-over "MessageActions → preference_pairs.jsonl
> bridge" caveat.

## What the cron does (end-to-end)

```
data plane                   tools/training/orpo_weekly_cron.py            artifacts
═══════════                  ════════════════════════════════════          ══════════
preference_pairs   ─Step 0──▶ export_preference_pairs()           ─▶ data/preference_pairs/build.jsonl
(Postgres table)              │ idempotent (24h sidecar)               .last_export
                              │ skip_export / force_export flags
                              ▼
                              for role in (coder, tester, debugger):
                                _resolve_pairs_file(role)
                                  │ per-role file?      → data/preference_pairs/<role>.jsonl
                                  │ else                → data/preference_pairs/build.jsonl  (shared)
                                  ▼
                                _pair_count >= min_pairs?
                                  │ yes → orpo_role_adapter.py            ─▶ models/lora/candidate/<role>-r16-<utc>.gguf
                                  │ no  → skip
                              ▼
                              write_diff_report()                  ─▶ data/training/diff_<utc>.md
```

**Idempotency:** export step writes `.last_export` sidecar; re-running
within `EXPORT_IDEMPOTENCY_HOURS` (24 by default) returns
`skipped_fresh` without re-hitting Postgres.  Pass `--force-export`
to bypass.

## Operator schedule install

### Windows (Task Scheduler)

```powershell
# Run from repo root.  Replace <repo-root> with the absolute path.
schtasks /Create `
  /SC WEEKLY /D SUN /ST 02:00 `
  /TN "AMOR\OrpoWeeklyCron" `
  /TR "python <repo-root>\tools\training\orpo_weekly_cron.py --json" `
  /F
```

Verify:
```powershell
schtasks /Query /TN "AMOR\OrpoWeeklyCron" /V /FO LIST
```

Remove:
```powershell
schtasks /Delete /TN "AMOR\OrpoWeeklyCron" /F
```

### Linux / macOS (cron)

```bash
# crontab -e  (each user)
# Sunday 02:00, repo root via absolute path
0 2 * * 0  cd /path/to/Amor-Distributed-Artificial-Intelligence-System && python tools/training/orpo_weekly_cron.py --json >> data/training/cron.log 2>&1
```

Verify:
```bash
crontab -l | grep orpo_weekly_cron
```

### Manual one-off run (any platform)

```bash
# All roles + full Postgres export
python tools/training/orpo_weekly_cron.py

# Dry-run (no trainer / no DB hit; shows what WOULD happen)
python tools/training/orpo_weekly_cron.py --dry-run

# Single role + skip the export (use existing JSONL on disk)
python tools/training/orpo_weekly_cron.py --role coder --skip-export

# Force a re-export within the 24h window (e.g. you just dropped
# 200 new ratings and want them in this run)
python tools/training/orpo_weekly_cron.py --force-export

# Different export window / mode
python tools/training/orpo_weekly_cron.py --export-since 7d --export-mode research
```

## Output paths

| Path | Purpose |
|---|---|
| `data/preference_pairs/build.jsonl` | Step 0 output — exported (chosen, rejected) pairs from Postgres |
| `data/preference_pairs/.last_export` | ISO timestamp of last successful export (idempotency check) |
| `data/preference_pairs/{coder,tester,debugger}.jsonl` | Per-role override files; if present, take precedence over `build.jsonl` |
| `models/lora/candidate/<role>-r16-<utc>/` | Candidate adapter directory |
| `models/lora/candidate/<role>-r16-<utc>.gguf` | Converted GGUF after `--convert-gguf` |
| `data/training/diff_<utc>.md` | Operator-facing diff report (promote checklist) |

## Promote decision

After each cron run, open the diff report:

```bash
ls -t data/training/diff_*.md | head -1 | xargs cat
```

Each role-trained section has a 4-step checklist:

1. Inspect adapter sanity (spot-check completions)
2. Run eval-delta vs the in-production adapter:
   ```bash
   python tools/lora/promote.py --role <role> --candidate <path>
   ```
   This emits a Sprint-0 corpus delta report.
3. If delta ≥ +3 pp role-adherence → promote:
   ```bash
   python tools/lora/promote.py --role <role> --candidate <path> --promote
   ```
4. If delta < +3 pp → leave candidate, accumulate more pairs.

## Privacy reminder

The Postgres `preference_pairs` table stores raw prompt/chosen/rejected
text **only when `opt_in_raw=True`** on the row.  The MessageActions UI
defaults to opt-in OFF; raw text is hashed (SHA-256) for dedup but the
trainer skips hash-only rows.  Operators must explicitly opt in per
session (or globally via `/admin/training`) for the trainer to have
data.

## Rollback

* Disable the cron (operator skip; no code change):
  - Windows: `schtasks /Delete /TN "AMOR\OrpoWeeklyCron" /F`
  - Linux: remove the crontab line
* Disable just the export step (keep cron running, hand-feed JSONL):
  `python tools/training/orpo_weekly_cron.py --skip-export`
* Disable the entire feature (v18 setting flag):
  `AMOR_CODE_PIPELINE_PARALLEL=false` (Sprint 6 piece 1)
  `AMOR_CODE_CRITIC_PREFIX_WARMUP=false` (Sprint 6 piece 2)
  Cron piece has no setting flag — uninstall task is the rollback.

## Verification

```bash
# 22-test cron sweep
pytest tests/training/test_orpo_weekly_cron.py -v
# expected: 22/22 PASSED

# Dry-run smoke (no DB / no trainer needed)
python tools/training/orpo_weekly_cron.py --dry-run --json
# expected: JSON report with export.status=skipped_fresh (or exported
# if .last_export is missing), 3 role results all skipped="dry-run"

# End-to-end with real Postgres (requires running stack)
docker exec amor-app-2 python /app/tools/training/orpo_weekly_cron.py \
  --force-export --skip-export=false --dry-run
# expected: Step 0 hits Postgres, writes data/preference_pairs/build.jsonl,
# all 3 roles proceed to (dry-run skip)
```
