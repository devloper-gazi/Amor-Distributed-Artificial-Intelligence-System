# Code Intelligence Mode — Operational Runbook

How to start the stack, debug common issues, and tune the autonomous
features.

## Start the stack

```bash
cp .env.example .env       # edit if you have Langfuse / GitHub keys
docker compose up -d
```

Wait ~3 min for healthchecks, then open `http://localhost:8000` and
register an account.

## Verify Code Intelligence Mode is wired

```bash
curl -s http://localhost:8000/openapi.json \
  | python -c "import json,sys; d=json.load(sys.stdin);
[print(p) for p in sorted(d.get('paths', {})) if p.startswith('/api/code')]"
```

Should show **11 paths**: `/triage`, `/start`, `/{sid}` (×3),
`/{sid}/cancel`, `/models` (+ `/models/{tag}/pull`), `/sandbox/health`,
`/capabilities`, `/capabilities/discover`.

## Add a new code model manually

```bash
docker exec amor-ollama ollama pull qwen2.5-coder:7b
# Refresh the registry probe
curl -s -X GET http://localhost:8000/api/code/models \
  -H "Authorization: Bearer $JWT"
```

The model becomes selectable for any agent role on next session.

## Debug a failed sandbox execution

1. `curl -s http://localhost:8000/api/code/sandbox/health` — confirm
   Docker daemon is reachable.
2. `docker exec amor-app-1 docker run --rm python:3.11-slim python -c "print('ok')"`
   — confirm the app container can spawn sandboxed runners.
3. `docker logs amor-app-1 | grep sandbox_` — look for image-pull or
   timeout traces.
4. If a runner image is missing: `docker pull python:3.11-slim` on the
   host (not inside the app container).

## Adversarial Reviewer halted my session

1. Open the chat — the `adversarial_alert` banner shows the rule_id
   and matched excerpt.
2. False positive? Edit `document_processor/code_intelligence/security/
   adversary_rules.yaml` and tighten the regex.
3. Hot-reload: `curl -X POST http://localhost:8000/api/code/admin/
   reload_adversarial_rules` — *(not yet exposed; add to a future
   admin endpoint)*. For now restart the app: `docker compose restart
   app`.

## Inspect the capability discovery feed

```bash
# Currently registered
curl -s http://localhost:8000/api/code/capabilities \
  -H "Authorization: Bearer $JWT" | jq .

# Force a cycle now
curl -s -X POST http://localhost:8000/api/code/capabilities/discover \
  -H "Authorization: Bearer $JWT" | jq .
```

The discoverer logs every cycle as `capability_discovery_cycle` —
filter logs:

```bash
docker logs amor-app-1 2>&1 | grep capability_discovery_cycle
```

## Disable autonomous discovery (privacy / compliance)

Set `CODE_CAPABILITY_DISCOVERY_ENABLED=false` in `.env` and restart:

```bash
docker compose up -d --force-recreate app
```

The lifespan no longer spawns `run_forever()` and the
`/capabilities/discover` endpoint returns 503.

## Where do traces go?

- If `CODE_LANGFUSE_URL` + `CODE_LANGFUSE_PUBLIC_KEY` +
  `CODE_LANGFUSE_SECRET_KEY` are set → Langfuse.
- Otherwise → `document_processor/code_intelligence/traces/{date}.jsonl`,
  one span per line.

```bash
docker exec amor-app-1 ls /app/document_processor/code_intelligence/traces/
docker exec amor-app-1 head -5 /app/document_processor/code_intelligence/traces/$(date -u +%Y-%m-%d).jsonl
```

## Emergency: reset everything

```bash
docker compose down -v          # drops volumes
docker compose up -d
```

This wipes Mongo (chat history, capabilities), Redis (sessions,
caches), and the prewarmed sandbox images.
