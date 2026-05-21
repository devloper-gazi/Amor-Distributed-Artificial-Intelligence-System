import {
  type Component,
  createSignal,
  createMemo,
  Show,
  For,
} from "solid-js";
import { TopBar } from "../components/shell/TopBar";
import { ConnectionBanner } from "../components/shell/ConnectionBanner";
import { ChatComposer } from "../components/chat/ChatComposer";
import { MessageThread } from "../components/chat/MessageThread";
import { t } from "../i18n";
import {
  Button,
  StatusPill,
  type Status,
} from "../components/ui";
import { api } from "../lib/api";
import {
  openEventStream,
  type OpenedStream,
  type StreamStatus,
  type SseEvent,
} from "../lib/sse";
import type { ChatTurn } from "../lib/types";
import { sessions } from "../lib/sessions";
import { invalidateSessionsList } from "../lib/query-client";

interface StartResp {
  session_id: string;
  success?: boolean;
}

interface PhaseDef {
  key: string;
  label: string;
  pct: number;
  /** User-facing description shown while this phase is running so
   *  the chat thread isn't a blank "startingÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦" for 1ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ2 minutes
   *  during a slow LLM phase like implement. */
  doingNow: string;
}

const PHASES: ReadonlyArray<PhaseDef> = [
  { key: "triage",     label: "Triage",      pct: 10, doingNow: "Classifying the request" },
  { key: "model_prep", label: "Model prep",  pct: 15, doingNow: "Preparing models" },
  { key: "plan",       label: "Plan",        pct: 25, doingNow: "Drafting a plan" },
  { key: "implement",  label: "Implement",   pct: 50, doingNow: "Writing the code (this is the slow phase ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â usually 30ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ120 s)" },
  { key: "execute",    label: "Execute",     pct: 60, doingNow: "Running the code in the sandbox" },
  { key: "analyze",    label: "Analyse",     pct: 68, doingNow: "Static-analysing the output" },
  { key: "test",       label: "Test",        pct: 78, doingNow: "Generating tests" },
  { key: "debug",      label: "Debug",       pct: 88, doingNow: "Debugging failures" },
  { key: "review",     label: "Review",      pct: 98, doingNow: "Final review" },
];

const PHASE_BY_KEY: Record<string, PhaseDef> = Object.fromEntries(
  PHASES.map((p) => [p.key, p]),
);

type PhaseStatus = "pending" | "running" | "done" | "failed" | "skipped";

let _idCounter = 0;
const newId = (): string => `b-${Date.now()}-${++_idCounter}`;

/* ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ Module-scoped state ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚ÂÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬
 * Signals created at module level so the user can navigate away
 * from /build (e.g. to /system) and come back without wiping the
 * conversation or killing an in-flight pipeline.  ``resetBuild``
 * clears state on logout.  Turns + phases also persist to
 * localStorage so an F5 / browser-restart preserves the
 * conversation transcript. */

const STORAGE_KEY_TURNS = "amor.chat.v1.build.turns";
const STORAGE_KEY_PHASES = "amor.chat.v1.build.phases";
// Cycle D ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â per-mode effort persistence (twin of Research's
// `amor.research.effort`).  Build accepts the same five canonical
// tiers as the LocalAI backend.  Default = "medium" matches the
// long-standing hardcoded default.
const STORAGE_KEY_EFFORT = "amor.build.effort";

const BUILD_EFFORT_TIERS = [
  { value: "basic",  label_key: "effort.basic.label",  description_key: "effort.basic.description" },
  { value: "medium", label_key: "effort.medium.label", description_key: "effort.medium.description" },
  { value: "deep",   label_key: "effort.deep.label",   description_key: "effort.deep.description" },
  { value: "expert", label_key: "effort.expert.label", description_key: "effort.expert.description" },
  { value: "ultra",  label_key: "effort.ultra.label",  description_key: "effort.ultra.description" },
] as const;
type BuildEffort = (typeof BUILD_EFFORT_TIERS)[number]["value"];

function loadBuildEffort(): BuildEffort {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_EFFORT);
    if (raw && BUILD_EFFORT_TIERS.some((t) => t.value === raw)) {
      return raw as BuildEffort;
    }
  } catch {
    // ignore
  }
  return "medium";
}

function saveBuildEffort(value: BuildEffort): void {
  try {
    localStorage.setItem(STORAGE_KEY_EFFORT, value);
  } catch {
    // ignore
  }
}

function loadJSON<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}
function saveJSON(key: string, value: unknown): void {
  try {
    const json = JSON.stringify(value);
    if (json.length > 256_000) return; // bail on absurdly large blobs
    localStorage.setItem(key, json);
  } catch {
    // ignore quota / disabled storage
  }
}

const [turnsRaw, setTurnsRaw] = createSignal<ChatTurn[]>(
  loadJSON<ChatTurn[]>(STORAGE_KEY_TURNS, []),
);
const turns = turnsRaw;
const setTurns = ((next: Parameters<typeof setTurnsRaw>[0]) => {
  const result = setTurnsRaw(next);
  queueMicrotask(() => saveJSON(STORAGE_KEY_TURNS, turns().slice(-100)));
  return result;
}) as typeof setTurnsRaw;
const [busy, setBusy] = createSignal(false);
const [status, setStatus] = createSignal<StreamStatus>("closed");
const [sessionId, setSessionId] = createSignal<string | null>(null);
// Cycle D Sessions polish ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â links the running pipeline to its
// chat_sessions row so done/error events can bump updated_at and
// nudge the sidebar's derived activity status to "recent".
const [chatSessionId, setChatSessionId] = createSignal<string | null>(null);
const [effort, setEffortRaw] = createSignal<BuildEffort>(loadBuildEffort());
const setEffort = (next: BuildEffort): void => {
  setEffortRaw(next);
  saveBuildEffort(next);
};
const [phasesRaw, setPhasesRaw] = createSignal<Record<string, PhaseStatus>>(
  loadJSON<Record<string, PhaseStatus>>(STORAGE_KEY_PHASES, {}),
);
const phases = phasesRaw;
const setPhases = ((next: Parameters<typeof setPhasesRaw>[0]) => {
  const result = setPhasesRaw(next);
  queueMicrotask(() => saveJSON(STORAGE_KEY_PHASES, phases()));
  return result;
}) as typeof setPhasesRaw;
/** Active phase key + when it started.  Drives the live status block
 *  rendered above the composer so the user doesn't stare at a blank
 *  "startingÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦" for 60+ seconds during a slow phase. */
const [activePhase, setActivePhase] = createSignal<string | null>(null);
const [phaseStartedAt, setPhaseStartedAt] = createSignal<number | null>(null);
/** Re-renders every second so the elapsed counter ticks live. */
const [tickNow, setTickNow] = createSignal<number>(Date.now());

let stream: OpenedStream | null = null;
let assistantTurnId: string | null = null;
let tickTimer: ReturnType<typeof setInterval> | null = null;

const startTicker = (): void => {
  if (tickTimer !== null) return;
  tickTimer = setInterval(() => setTickNow(Date.now()), 1000);
};
const stopTicker = (): void => {
  if (tickTimer !== null) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
};

const cleanupStream = (): void => {
  if (stream) {
    stream.close();
    stream = null;
  }
};

export function resetBuild(): void {
  cleanupStream();
  stopTicker();
  setTurns([]);
  setBusy(false);
  setStatus("closed");
  setSessionId(null);
  setPhases({});
  setActivePhase(null);
  setPhaseStartedAt(null);
  assistantTurnId = null;
  // Wipe the persisted transcript so the next user doesn't see
  // the previous one's prompts on F5.
  try {
    localStorage.removeItem(STORAGE_KEY_TURNS);
    localStorage.removeItem(STORAGE_KEY_PHASES);
  } catch {
    // ignore
  }
}

const setPhase = (key: string, st: PhaseStatus): void => {
  setPhases((prev) => ({ ...prev, [key]: st }));
};

const patchAssistant = (
  content: string,
  tag?: string,
  streaming = false,
): void => {
  if (!assistantTurnId) return;
  const id = assistantTurnId;
  setTurns((prev) =>
    prev.map((t) => (t.id === id ? { ...t, content, tag, streaming } : t)),
  );
};

const currentBuffer = (): string => {
  if (!assistantTurnId) return "";
  const t = turns().find((x) => x.id === assistantTurnId);
  return t?.content ?? "";
};

const appendBlock = (block: string): void => {
  const cur = currentBuffer();
  const next =
    cur === "_(startingÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦)_" || cur === "" ? block : cur + "\n" + block;
  patchAssistant(next, undefined, true);
};

const handleEvent = (ev: SseEvent): void => {
  const type = String(ev.type ?? "");
  const phase = String(ev.phase ?? "");
  switch (type) {
    case "phase_start":
      if (phase) {
        setPhase(phase, "running");
        setActivePhase(phase);
        setPhaseStartedAt(Date.now());
        startTicker();
      }
      patchAssistant(currentBuffer(), `phase: ${phase}`, true);
      break;
    case "phase_complete":
      if (phase) {
        setPhase(phase, "done");
        if (activePhase() === phase) setActivePhase(null);
      }
      break;
    case "phase_failed":
      if (phase) setPhase(phase, "failed");
      patchAssistant(
        currentBuffer() +
          `\n\n**Phase failed:** ${phase} ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ${String(ev.error ?? "")}`,
        "failed",
      );
      break;
    case "code_ready": {
      const code = String(ev.code ?? "");
      const lang = String(ev.language ?? "");
      appendBlock(`### Code (${lang})\n\n\`\`\`${lang}\n${code}\n\`\`\`\n`);
      break;
    }
    case "language_corrected": {
      // Cycle B Commit V ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â engine flipped the language post-coder
      // (e.g. pythonÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢html when the body sniffed as HTML).  Surface it
      // so the user knows we re-routed without them seeing a Python
      // pip-install attempt against an HTML runner.
      const from = String(ev.from ?? "");
      const to = String(ev.to ?? "");
      appendBlock(
        `_Language corrected: \`${from}\` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ \`${to}\` (sandbox runner switched, stale dependencies dropped)._\n`,
      );
      break;
    }
    case "planner_fallback": {
      // Cycle D Fix #6 ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â planner LLM emitted unparseable / empty
      // output; engine swapped in a deterministic minimal plan so
      // the pipeline can still produce a deliverable.  Surface it
      // as a subtle italic notice so the operator can spot the
      // degradation without thinking the run failed.
      appendBlock(
        "_ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã‚Â¡Ãƒâ€šÃ‚Â  Planner fallback active ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â model produced unparseable output, " +
        "running with a minimal generated plan._\n",
      );
      break;
    }
    case "install_packages_filtered": {
      // Cycle D Fix #3 ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â engine cross-checked declared deps against
      // actual code use and dropped some packages (e.g. doxygen+latex
      // for a self-contained C++ formatter).  Surface so the operator
      // sees why a "pip install" they expected didn't happen.
      const dropped = Array.isArray(ev.dropped) ? ev.dropped : [];
      if (dropped.length) {
        appendBlock(
          `_Skipped install: \`${dropped.join("`, `")}\` ` +
          "(package not referenced in generated code)._\n",
        );
      }
      break;
    }
    case "execution_install_packages": {
      const pkgs = Array.isArray(ev.packages) ? ev.packages : [];
      if (pkgs.length) {
        appendBlock(`_Installing packages: \`${pkgs.join("`, `")}\`_\n`);
      }
      break;
    }
    case "execution_extra_files": {
      const files = Array.isArray(ev.files) ? ev.files : [];
      if (files.length) {
        appendBlock(
          `_Mounting sidecar files: ${files.map((f) => `\`${f}\``).join(", ")}_\n`,
        );
      }
      break;
    }
    case "repomap_attached": {
      // Sprint 3 Day 4 ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â engine prepended a repomap excerpt to the
      // triage system message.  Surface it as a small collapsible
      // <details> block so the user sees what context the planner
      // actually had.  Render only the metadata (token count + render
      // time) ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â the full repomap markdown isn't streamed to the UI
      // (it's already in the LLM's prompt).
      const tk = Number(ev.tokens_estimate ?? 0);
      const ms = Number(ev.render_ms ?? 0);
      const budget = Number(ev.budget_tokens ?? 0);
      appendBlock(
        `<details><summary>ÃƒÆ’Ã‚Â°Ãƒâ€¦Ã‚Â¸ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒâ€¦Ã‚Â¡ Repomap context attached ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ${tk} tokens / ${budget} budget ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· rendered in ${ms} ms</summary>\n\nThe triage agent saw a token-budgeted summary of the workspace symbol graph before classifying this prompt. Toggle <code>AMOR_REPOMAP_ENABLED</code> on the app service to disable.</details>\n`,
      );
      break;
    }
    case "memory_recalled": {
      // Sprint 7 Day 4 ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â Mem0 surfaced N memories before triage.
      // Stamp the assistant turn with ``remembered`` metadata so
      // ``MessageBubble`` renders the "Remembered N" pill, and add
      // a small <details> block below the bubble for inspection.
      const count = Number(ev.count ?? 0);
      if (count > 0 && assistantTurnId) {
        const id = assistantTurnId;
        const snippetsArr = Array.isArray(ev.snippets) ? ev.snippets : [];
        const snippets = snippetsArr
          .filter((s): s is string => typeof s === "string")
          .slice(0, 3);
        setTurns((prev) =>
          prev.map((t) =>
            t.id === id
              ? { ...t, remembered: { count, snippets } }
              : t,
          ),
        );
        if (snippets.length > 0) {
          appendBlock(
            `<details><summary>ÃƒÆ’Ã‚Â°Ãƒâ€¦Ã‚Â¸Ãƒâ€šÃ‚Â§Ãƒâ€šÃ‚Â  Remembered ${count} prior fact${count === 1 ? "" : "s"}</summary>\n\n${snippets.map((s) => `- ${s}`).join("\n")}\n</details>\n`,
          );
        }
      }
      break;
    }
    case "test_ready": {
      const code = String(ev.code ?? "");
      appendBlock(`### Tests\n\n\`\`\`\n${code}\n\`\`\`\n`);
      break;
    }
    case "test_execution_result": {
      // Cycle D ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â Tests actually ran against the implementation.
      // Render a pass/fail block so the operator can see test
      // outcomes in addition to the standalone test source.
      const result = (ev.result ?? {}) as Record<string, unknown>;
      const exit = result.exit_code;
      const skipped = Boolean(result.skipped);
      const stdout = String(result.stdout ?? "").trim();
      const stderr = String(result.stderr ?? "").trim();
      if (skipped) {
        appendBlock(
          "_Test execution skipped ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â runner not configured for this language._\n",
        );
        break;
      }
      const passed = exit === 0;
      const head = passed
        ? "### Test Results ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ Pass"
        : "### Test Results ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢Ãƒâ€šÃ‚ÂÃƒâ€¦Ã¢â‚¬â„¢ Fail";
      const block = [
        head,
        exit !== undefined ? `exit_code: ${exit}` : "",
        stdout
          ? `\n**stdout:**\n\n\`\`\`\n${stdout.slice(0, 2000)}\n\`\`\``
          : "",
        stderr && !passed
          ? `\n**stderr:**\n\n\`\`\`\n${stderr.slice(0, 1000)}\n\`\`\``
          : "",
      ]
        .filter(Boolean)
        .join("\n");
      appendBlock(block + "\n");
      break;
    }
    case "test_execution_skipped": {
      const reason = String(ev.reason ?? "");
      appendBlock(
        `_Test execution skipped (${reason}).  Tests rendered as code only._\n`,
      );
      break;
    }
    case "reflexion_score": {
      // Cycle D ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â Reflexion baseline score emitted right after the
      // first review.  Subtle italic so it stays unobtrusive.
      const score = Number(ev.score ?? 0);
      const phase = String(ev.phase ?? "");
      appendBlock(
        `_Quality score (${phase}): ${score}/100._\n`,
      );
      break;
    }
    case "reflexion_iteration_start": {
      const iter = Number(ev.iteration ?? 0);
      const max = Number(ev.max ?? 0);
      const baseline = Number(ev.baseline_score ?? 0);
      const threshold = Number(ev.threshold ?? 80);
      appendBlock(
        `_ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢Ãƒâ€šÃ‚Â» Reflexion iteration ${iter}/${max} starting ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ` +
        `baseline ${baseline}/100 below threshold ${threshold}._\n`,
      );
      break;
    }
    case "reflexion_iteration_complete": {
      const iter = Number(ev.iteration ?? 0);
      const outcome = String(ev.outcome ?? "");
      const baseline = Number(ev.baseline_score ?? 0);
      const newScore = Number(ev.new_score ?? 0);
      if (outcome === "improved") {
        appendBlock(
          `_ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“Ãƒâ€šÃ‚Â¨ Reflexion ${iter} improved quality: ` +
          `${baseline} ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ ${newScore}/100.  Adopted the new version._\n`,
        );
      } else if (outcome === "no_gain") {
        appendBlock(
          `_Reflexion ${iter} produced no gain (${newScore} ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â°Ãƒâ€šÃ‚Â¤ ${baseline}).  ` +
          `Kept the original version._\n`,
        );
      } else if (outcome === "error") {
        const err = String(ev.error ?? "unknown");
        appendBlock(
          `_Reflexion ${iter} errored (${err}).  Kept the original version._\n`,
        );
      }
      break;
    }
    case "execution_result": {
      const result = (ev.result ?? {}) as Record<string, unknown>;
      const stdout = String(result.stdout ?? "").trim();
      const stderr = String(result.stderr ?? "").trim();
      const exit = result.exit_code;
      const skipped = Boolean(result.skipped);
      const block = [
        `### Execution${skipped ? " (skipped)" : ""}`,
        exit !== undefined ? `exit_code: ${exit}` : "",
        stdout
          ? `\n**stdout:**\n\n\`\`\`\n${stdout.slice(0, 2000)}\n\`\`\``
          : "",
        stderr
          ? `\n**stderr:**\n\n\`\`\`\n${stderr.slice(0, 1000)}\n\`\`\``
          : "",
      ]
        .filter(Boolean)
        .join("\n");
      appendBlock(block + "\n");
      break;
    }
    case "review_ready": {
      const review = (ev.review ?? ev.detail ?? {}) as Record<string, unknown>;
      const verdict = String(review.verdict ?? review.score ?? "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â");
      const summary = String(review.final_comment ?? review.summary ?? "");
      appendBlock(`### Review\n\n**Verdict:** ${verdict}\n\n${summary}\n`);
      break;
    }
    case "deliverable_ready": {
      const md = String(ev.markdown ?? "");
      if (md) appendBlock(`### Deliverable\n\n${md}\n`);
      break;
    }
    case "model_download_progress": {
      const pct = Number(ev.pct ?? 0);
      const model = String(ev.model ?? "");
      patchAssistant(currentBuffer(), `pulling ${model} ${pct}%`, true);
      break;
    }
    case "done":
      for (const p of PHASES) {
        if (!phases()[p.key]) setPhase(p.key, "done");
      }
      patchAssistant(currentBuffer() || "_(done)_", "done");
      cleanupStream();
      stopTicker();
      setActivePhase(null);
      setBusy(false);
      bumpChatSession();
      break;
    case "error":
      patchAssistant(
        currentBuffer() +
          `\n\n**Error:** ${String(ev.message ?? "unknown")}`,
        "failed",
      );
      cleanupStream();
      stopTicker();
      setActivePhase(null);
      setBusy(false);
      bumpChatSession();
      break;
    case "cancelled":
      patchAssistant(currentBuffer() + "\n\n_(cancelled)_", "cancelled");
      cleanupStream();
      stopTicker();
      setActivePhase(null);
      setBusy(false);
      bumpChatSession();
      break;
  }
};

/** Cycle D ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â at the end of a pipeline run, bump the linked
 *  chat_session's ``updated_at`` so the sidebar moves the row to the
 *  top of the "Today" / "Now" group and the derived activity dot
 *  reflects the recent finish.  Touches title with a no-op patch
 *  (the smallest mutation that updates ``updated_at`` server-side).
 *  Best-effort ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â failure is logged and silently swallowed. */
const bumpChatSession = (): void => {
  const id = chatSessionId();
  if (!id) return;
  // Mode pipelines emit ``done`` from a stream callback; we don't want
  // to await here and slow the UI cleanup.  Fire-and-forget.
  void sessions
    .update(id, {})
    .then(() => invalidateSessionsList())
    .catch((err) => {
      console.warn("[build] sessions.update on done/error failed:", err);
    });
};

const start = async (prompt: string): Promise<void> => {
  cleanupStream();
  setBusy(true);
  setStatus("connecting");
  setPhases({});
  setTurns((prev) => [
    ...prev,
    { id: newId(), role: "user", content: prompt, ts: Date.now() },
  ]);
  assistantTurnId = newId();
  setTurns((prev) => [
    ...prev,
    {
      id: assistantTurnId!,
      role: "assistant",
      content: "_(startingÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦)_",
      streaming: true,
      tag: "phase: triage",
      ts: Date.now(),
    },
  ]);

  try {
    // Cycle D Sessions polish ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â register a chat_session ROW first so
    // the sidebar shows the new session immediately (before the
    // pipeline even reaches the triage phase).  ``code`` is the
    // backend's canonical mode tag for Build sessions.
    let chatSessionId: string | undefined;
    try {
      const created = await sessions.create({
        mode: "code",
        title: prompt.slice(0, 60),
      });
      chatSessionId = (created as { id?: string }).id;
      invalidateSessionsList();
    } catch (err: unknown) {
      // Don't block the run if the chat-session create fails ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â
      // the pipeline can still run; the sidebar just won't list it.
      console.warn("[build] chat_session create failed:", err);
    }

    const resp = await api.post<StartResp>("/api/code/start", {
      prompt,
      effort: effort(),
      chat_session_id: chatSessionId,
    });
    setSessionId(resp.session_id);
    // Track the chat_session_id so ``done`` / ``error`` events can
    // bump ``updated_at`` and refresh the sidebar's relative-time +
    // status-dot derivation.
    setChatSessionId(chatSessionId ?? null);
    stream = openEventStream({
      url: `/api/code/${resp.session_id}/events`,
      onStatusChange: (s) => setStatus(s),
      onEvent: handleEvent,
    });
  } catch (err: unknown) {
    const detail =
      (err as { body?: { detail?: string } })?.body?.detail ??
      (err instanceof Error ? err.message : "Failed to start build");
    setBusy(false);
    setStatus("closed");
    patchAssistant(`**Error:** ${String(detail)}`, "failed");
  }
};

const cancel = async (): Promise<void> => {
  const sid = sessionId();
  if (sid) {
    try {
      await api.post(`/api/code/${sid}/cancel`);
    } catch {
      // ignore
    }
  }
  cleanupStream();
  stopTicker();
  setActivePhase(null);
  patchAssistant("_(cancelled)_", "cancelled");
  setBusy(false);
};

/**
 * Live phase status bar ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â rendered above the composer while busy.
 * Shows the current phase's user-friendly description + an elapsed
 * counter that ticks every second.  Empty when the pipeline isn't
 * running.  Solves the "(startingÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦) for 60 s" black hole during
 * the slow Implement phase.
 */
const PhaseStatusBar: Component = () => {
  const elapsed = createMemo<number>(() => {
    const start = phaseStartedAt();
    if (start === null) return 0;
    return Math.max(0, Math.floor((tickNow() - start) / 1000));
  });

  const fmtElapsed = (s: number): string => {
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}m ${r.toString().padStart(2, "0")}s`;
  };

  return (
    <Show when={busy() && activePhase()}>
      {(phase) => {
        const def = (): PhaseDef | undefined => PHASE_BY_KEY[phase()];
        return (
          <div
            role="status"
            aria-live="polite"
            class="flex items-center gap-3 border-t border-border-subtle bg-bg-elevated-v25 px-5 py-3 text-sm"
          >
            <span
              class="h-2 w-2 rounded-full motion-safe:animate-pulse"
              style={{ background: "var(--mode-accent)" }}
              aria-hidden="true"
            />
            <span class="flex-1 truncate text-text-display">
              <span class="font-medium">{def()?.label ?? phase()}</span>
              <span class="ml-2 text-text-body">
                {def()?.doingNow ?? "running"}
              </span>
            </span>
            <span class="font-mono text-xs text-text-subtle tabular-nums">
              {fmtElapsed(elapsed())}
            </span>
          </div>
        );
      }}
    </Show>
  );
};

/**
 * Build mode component ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â pure render shell.  All state lives at the
 * module level above so it survives route remounts (Build ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ System
 * ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ Build doesn't wipe an in-flight pipeline).
 */
export const Build: Component = () => {
  const headerStatus = createMemo<Status>(() => {
    switch (status()) {
      case "open":
        return busy() ? "warming" : "healthy";
      case "connecting":
      case "reconnecting":
        return "warming";
      case "offline":
        return "failed";
      default:
        return busy() ? "warming" : "healthy";
    }
  });

  return (
    <div data-mode="build" class="flex h-full">
      {/* Left rail ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â phase timeline */}
      <aside class="hidden w-52 shrink-0 border-r border-border-subtle bg-bg-elevated-v25 lg:flex lg:flex-col">
        <div class="border-b border-border-subtle px-3 py-3 text-[0.65rem] font-semibold uppercase tracking-widest text-text-subtle">
          Pipeline
        </div>
        <ol class="flex-1 overflow-y-auto p-2 space-y-1">
          <For each={PHASES}>
            {(p) => {
              const st = (): PhaseStatus => phases()[p.key] ?? "pending";
              const dot = (): string => {
                switch (st()) {
                  case "running":
                    return "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬ÂÃƒâ€šÃ‚Â";
                  case "done":
                    return "ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ";
                  case "failed":
                    return "ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â";
                  case "skipped":
                    return "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬ÂÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹";
                  default:
                    return "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬ÂÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹";
                }
              };
              return (
                <li
                  class={[
                    "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs",
                    st() === "running"
                      ? "bg-bg-hover text-text-display"
                      : st() === "done"
                        ? "text-text-body"
                        : st() === "failed"
                          ? "text-status-failed"
                          : "text-text-subtle",
                  ].join(" ")}
                >
                  <span
                    class={[
                      "w-3 text-center",
                      st() === "running"
                        ? "motion-safe:animate-pulse"
                        : "",
                    ].join(" ")}
                    aria-hidden="true"
                    style={
                      st() === "running"
                        ? { color: "var(--mode-accent)" }
                        : undefined
                    }
                  >
                    {dot()}
                  </span>
                  <span class="flex-1 truncate">{p.label}</span>
                  <span class="text-[0.6rem] text-text-subtle">
                    {p.pct}%
                  </span>
                </li>
              );
            }}
          </For>
        </ol>
      </aside>

      {/* Right side ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â chat */}
      <div class="flex min-w-0 flex-1 flex-col">
        <TopBar
          title={t("build.title")}
          subtitle={t("build.subtitle")}
          actions={
            <Show when={busy()}>
              <StatusPill status={headerStatus()} size="sm" />
              <Button variant="secondary" size="sm" onClick={cancel}>
                {t("common.cancel")}
              </Button>
            </Show>
          }
        />
        <ConnectionBanner status={status()} />
        <MessageThread
          turns={turns()}
          emptyState={
            <div class="max-w-md text-center">
              <p class="text-base text-text-display">
                {t("build.empty.title")}
              </p>
              <p class="mt-2 text-sm text-text-subtle">
                {t("build.empty.body")}
              </p>
            </div>
          }
        />
        <PhaseStatusBar />
        <ChatComposer
          onSubmit={start}
          busy={busy()}
          onCancel={cancel}
          placeholder={t("build.composer.placeholder")}
          effortTiers={BUILD_EFFORT_TIERS}
          effortValue={effort()}
          onEffortChange={(v) => setEffort(v as BuildEffort)}
        />
      </div>
    </div>
  );
};
