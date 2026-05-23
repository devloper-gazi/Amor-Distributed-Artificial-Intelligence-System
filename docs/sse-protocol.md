# AMOR SSE protocol — pipeline events

> Cycle C Sprint 4 Day 4.  Defines the wire format the AMOR pipeline
> emits over Server-Sent Events and the canonical envelope future
> consumers (and external integrators) target.

## 1. Transport

* HTTP `text/event-stream`, one event per `\n\n`-terminated chunk.
* Each chunk uses the standard SSE fields:
  ```
  id: <ULID>
  event: <reserved — not used today>
  data: <JSON-encoded event payload>
  ```
* Re-connection uses `Last-Event-ID` (Phase 17 Commit S — Redis
  Streams replay).  Sprint 9 will harden this further.
* Buffering is disabled at the gateway (`proxy_buffering off;`) and
  the response carries `X-Accel-Buffering: no`.

## 2. Two protocol layers

The wire actually carries two shapes today.  Both are valid; new
consumers should target the **canonical envelope** (§4) and use the
adapter described in §5 to map legacy AMOR events forward.

### 2.1  Native AMOR events

Emitted by `document_processor/code_intelligence/engine.py:_emit` and
fanned out by `_publish` in `code_intelligence_routes.py`.  Examples:

| `type`                      | Description |
|-----------------------------|-------------|
| `phase_start`               | Pipeline phase begins |
| `phase_complete`            | Phase succeeded |
| `phase_failed`              | Phase raised |
| `code_ready`                | Coder produced code |
| `execution_start`           | Sandbox run begins |
| `execution_install_packages`| Sandbox package list resolved |
| `execution_extra_files`     | Multi-file fixtures attached |
| `execution_result`          | Sandbox run finished |
| `static_analysis_result`    | Linter / type-checker finished |
| `test_ready`                | Tester emitted test code |
| `debug_iteration_start`     | Debug retry begins |
| `review_ready`              | Reviewer emitted review |
| `deliverable_ready`         | Final bundle assembled |
| `repomap_attached`          | Sprint 3 — repomap context prepended |
| `language_corrected`        | Sprint 4 Build — sniffer flipped triage |
| `model_download_start`      | Auto-pull begins |
| `model_download_progress`   | Auto-pull in-flight |
| `model_download_complete`   | Auto-pull done |
| `done`                      | Stream complete |
| `cancelled`                 | Client cancelled |
| `error`                     | Pipeline aborted |
| `snapshot`                  | Late-subscriber catch-up dump |

### 2.2  Canonical tool-call envelope (Vercel AI SDK 5–compatible)

The envelope below mirrors Vercel AI SDK's stream-protocol so the
front-end's `ToolCallCard` and any third-party SDK consumer can show
animated tool calls without learning AMOR's event vocabulary.

```ts
type ToolEvent =
  | { type: "tool-input-start";      tool: string; toolCallId: string; meta?: object }
  | { type: "tool-input-delta";      toolCallId: string; delta: string }   // streamed json/text fragment
  | { type: "tool-input-available";  toolCallId: string; input: unknown }  // input parse complete
  | { type: "tool-output-available"; toolCallId: string; output: unknown; isError?: boolean }
  | { type: "tool-error";            toolCallId: string; message: string };
```

Reserved tool names (lowercased, kebab-case): `sandbox-execute`,
`static-analysis`, `code-review`, `repomap-attach`, `language-detect`,
`model-pull`, `debug-retry`, `repo-symbol-search`, `mention-resolve`.

## 3. Resumable streams

`Last-Event-ID` is honoured.  Server replays from the requested ULID
forward (per-session Redis Stream).  Late subscribers always receive
the most recent `snapshot` event first so the client can rebuild
state without missing intermediate transitions.

## 4. Event ordering invariants

* `phase_start` → `phase_complete` OR `phase_failed` (per phase).
* `execution_start` precedes any `execution_result` for the same
  iteration; `execution_install_packages` and `execution_extra_files`
  both come between them.
* `done`, `cancelled`, `error` are terminal — no events after.
* In the canonical envelope: `tool-input-start` precedes
  `tool-input-delta*` precedes `tool-input-available` precedes
  `tool-output-available` for the same `toolCallId`.

## 5. Adapter (frontend)

`web_ui/v2/src/lib/tool-stream.ts` exports `toToolEvents(amor: AmorEvent)`
that maps the legacy events in §2.1 into the canonical envelope of
§2.2.  The mapping is one-to-many (a single `execution_result` becomes
both `tool-input-available` and `tool-output-available`):

| AMOR event                      | Canonical envelope |
|---------------------------------|--------------------|
| `execution_start`               | `tool-input-start { tool: "sandbox-execute" }` |
| `execution_install_packages`    | `tool-input-delta` (packages list) |
| `execution_extra_files`         | `tool-input-delta` (files map) |
| `execution_result`              | `tool-input-available` + `tool-output-available` |
| `static_analysis_result`        | `tool-input-start` + `tool-output-available` |
| `review_ready`                  | `tool-input-start` + `tool-output-available` |
| `repomap_attached`              | `tool-output-available { tool: "repomap-attach" }` |

Future engine work (post-Sprint 4) can natively emit §2.2 events; the
adapter is a no-op pass-through when the input already matches the
envelope.

## 6. Backwards compatibility

Native AMOR events stay on the wire indefinitely — pre-Sprint-4
clients (legacy v1 UI, integration scripts) still parse them.
Sprint 4 adds the canonical envelope as an *additional* lens; no
existing event type is renamed or removed.

## 7. Worked example (sandbox run)

Engine emits, in order:

```
data: {"type":"execution_start","language":"python","iteration":1,...}
data: {"type":"execution_install_packages","packages":["numpy"],...}
data: {"type":"execution_result","exit_code":0,"stdout":"...","stderr":"",...}
```

The frontend adapter projects this into:

```
{ "type":"tool-input-start", "tool":"sandbox-execute", "toolCallId":"sb-1" }
{ "type":"tool-input-delta", "toolCallId":"sb-1", "delta":"{\"packages\":[\"numpy\"]}" }
{ "type":"tool-input-available", "toolCallId":"sb-1", "input":{"language":"python","packages":["numpy"]} }
{ "type":"tool-output-available", "toolCallId":"sb-1", "output":{"exit_code":0,"stdout":"..."} }
```

`ToolCallCard` renders one expandable card per `toolCallId` with the
animated state machine `pending → running → complete | error`.
