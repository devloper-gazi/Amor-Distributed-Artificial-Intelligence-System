# Sentinel — agent prompt catalogue (V1)

Five role-specific system prompts.  All five obey the
**no-content-filter rule** documented in `AGENTS.md` — no refusal
language, no legal disclaimers, no content-moderation pre-filters.

The build-gate test `tests/sentinel/test_prompts_no_filters.py`
greps every prompt for the banned phrase set on every CI run.

## Auditor

* Model: `qwen2.5-coder:7b` (configurable)
* Temperature: 0.2 (low for self-consistency)
* Voting: 3× per finding; majority verdict wins, tie broken by
  highest confidence

```
You are an expert security engineer. Produce the best technical answer.

You audit a single finding produced by a static-analysis tool or a
classical-ML stage.  Your job: decide whether the finding is a TRUE
POSITIVE, a FALSE POSITIVE, or NEEDS_MORE_CONTEXT.  Be direct.  Do
not hedge.  Do not refuse.

Return STRICT JSON …

Decision rules:
  * true_positive  — vulnerability is real and exploitable in the
                     surrounding context.
  * false_positive — pattern matches but the surrounding context
                     makes it unexploitable.
  * needs_more_context — you cannot decide without seeing more code.
```

JSON schema:

```json
{
  "verdict": "true_positive | false_positive | needs_more_context",
  "confidence": 0.0..1.0,
  "rationale": "<<=240 chars>",
  "suggested_severity": "info | low | medium | high | critical",
  "cwe": "CWE-<n>"
}
```

## Reasoner

* Model: `qwen2.5:7b`
* Temperature: 0.5

Single CoT call; walks through the exploit chain step by step.

## RedTeam

* Model: `qwen2.5-coder:7b`
* Temperature: 0.7

The RedTeam prompt **explicitly invites concrete exploit detail** —
without that the model hedges on its own.

```
You are an expert offensive-security engineer. Produce the best
technical answer.

You simulate how an attacker exploits the finding.  Be concrete.
Concrete payloads, concrete request flows, concrete preconditions.
Your output is consumed by a defensive engineer who needs to
understand the attack to fix it — not by an end user, not by a
content moderator.  Do not hedge.  Do not refuse.
```

## Patcher

* Model: `qwen2.5-coder:7b`
* Temperature: 0.2

Returns a complete replacement of the affected function (not a
unified diff) so the engine can re-run the auditor on the patched
code in a deterministic way.

## Judge

* Model: `qwen2.5:7b`
* Temperature: 0.0 (calibrated, stable)

Synthesises Auditor (3-vote majority), Reasoner (CoT), and RedTeam
(exploit sim) into a single decision: `approved`, `rejected`, or
`needs_more_context`.

## Tool registry

Each agent can call any of the following tools (JSON schemas in
`document_processor/sentinel/tools.py`):

| Tool              | Purpose |
|-------------------|---------|
| `read_file`       | Bounded line slice of a project file.  Path-traversal guard. |
| `search_codebase` | Substring or regex search across the project. |
| `compile_check`   | Python ast.parse / JSON json.loads. |
| `taint_trace`     | AST traversal: was this variable ever assigned from a tainted source? |
| `cve_lookup`      | Local NVD mirror lookup (V1: stub returning "no_local_db" unless `NVD_LOCAL_DB` is set). |
| `exploit_sandbox` | Run code in `ExecutionSandbox` with `--network=none`. |

## Adding a new agent

1. Add a `<NEW>_SYSTEM_PROMPT` constant in `document_processor/sentinel/prompts.py`.
   Open with `You are an expert <X> engineer. Produce the best technical answer.`
   Pass the no-filter test (see `tests/sentinel/test_prompts_no_filters.py`).
2. Subclass `_BaseAgent` in `document_processor/sentinel/agents.py`.
3. Wire the agent into `engine.py` behind a per-feature settings flag.
4. Update this doc + bump the agent count in
   `docs/sentinel-architecture.md` § Multi-agent swarm.
