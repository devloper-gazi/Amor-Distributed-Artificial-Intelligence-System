/**
 * Cycle UI 2026-05-20 — Frontend wrapper for /api/chat/classify.
 *
 * Provides:
 *  - `classifyPrompt(prompt)` — one-shot POST returning ClassifyResult
 *  - `createDebouncedClassifier()` — Solid signal-based hook that
 *    runs classify on a 150 ms debounce; cancels in-flight requests
 *    when the user keeps typing.  Used by ChatComposer to drive the
 *    auto-mode preview pill.
 *
 * Wire shape mirrors document_processor/api/chat.py:ClassifyResponse.
 * `alternatives` is the 6 (class, score) pairs sorted descending.
 *
 * Latency note: first call after a fresh app-2 boot triggers the
 * server-side MiniLM-L6 load (~3-5 s).  Subsequent calls are
 * ~10-100 ms.  The hook surfaces a `pending` signal so the UI can
 * render an optimistic mode pill on the first keystroke and refine
 * it when the classifier returns.
 */

import { createSignal, onCleanup } from "solid-js";
import type { Accessor } from "solid-js";
import { api } from "./api";

/** The 6 classes the classifier ever returns.  Kept in sync with the
 *  backend constant document_processor/services/intent_classifier.py:CLASSES.
 *  Frontend code can use this as a single source of truth for chip
 *  rendering + i18n key generation. */
export type ChatMode =
  | "build"
  | "research"
  | "thinking"
  | "consortium"
  | "sentinel"
  | "quickcode";

export const CHAT_MODES: readonly ChatMode[] = [
  "build",
  "research",
  "thinking",
  "consortium",
  "sentinel",
  "quickcode",
] as const;

export interface ClassifyResult {
  mode: ChatMode;
  top1_score: number;
  top2_score: number;
  confidence: number;
  low_confidence: boolean;
  alternatives: Array<[ChatMode, number]>;
  latency_ms: number;
}

/** Single-shot classify.  Throws on network/auth error so callers
 *  can decide what to do (e.g. surface a non-blocking error pill). */
export async function classifyPrompt(prompt: string): Promise<ClassifyResult> {
  return api.post<ClassifyResult>("/api/chat/classify", { prompt });
}

export interface DebouncedClassifierApi {
  /** Current classification result (or null before first run). */
  result: Accessor<ClassifyResult | null>;
  /** True while an HTTP call is in flight. */
  pending: Accessor<boolean>;
  /** Last network error (if any).  Stays set until next successful
   *  call OR until the caller manually clears via `setPrompt("")`. */
  error: Accessor<Error | null>;
  /** Schedule a classify on the debounce.  Empty / whitespace-only
   *  prompts immediately clear the result + cancel any pending call
   *  (the composer's "user erased input" state). */
  setPrompt: (text: string) => void;
  /** Explicit cancel — used when the composer is unmounted mid-typing. */
  cancel: () => void;
}

export interface DebouncedClassifierOptions {
  /** Milliseconds to wait after the last setPrompt before firing the
   *  classify call.  Default 150 ms — matches Claude Research J.5
   *  perceived-snappiness threshold. */
  debounceMs?: number;
  /** Minimum prompt length below which classify is skipped entirely.
   *  Default 4 — too few characters yield encoder garbage. */
  minLength?: number;
}

/** Solid-friendly debounced classifier hook.  Returns a signal-based
 *  API the composer wires up like:
 *
 *  ```tsx
 *  const cls = createDebouncedClassifier();
 *  return (
 *    <textarea onInput={(e) => cls.setPrompt(e.currentTarget.value)} />
 *    <Show when={cls.result()}>
 *      <ModePill mode={cls.result()!.mode}
 *                lowConfidence={cls.result()!.low_confidence} />
 *    </Show>
 *  );
 *  ```
 *
 *  Cancellation: when `setPrompt` is called again before the debounce
 *  fires, the pending timer is cleared.  When a classify HTTP call is
 *  in flight, the result is dropped if a newer `setPrompt` landed
 *  during the call (preventing stale-classification flicker). */
export function createDebouncedClassifier(
  options: DebouncedClassifierOptions = {},
): DebouncedClassifierApi {
  const debounceMs = options.debounceMs ?? 150;
  const minLength = options.minLength ?? 4;

  const [result, setResult] = createSignal<ClassifyResult | null>(null);
  const [pending, setPending] = createSignal<boolean>(false);
  const [error, setError] = createSignal<Error | null>(null);

  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlightToken = 0; // monotonic — used to drop stale responses
  let lastPrompt = "";

  const fire = async (prompt: string, token: number): Promise<void> => {
    setPending(true);
    setError(null);
    try {
      const res = await classifyPrompt(prompt);
      // Drop stale: if the user typed more characters after we fired,
      // a newer call is either in-flight or queued.  Discard this
      // result so the pill doesn't flicker.
      if (token !== inFlightToken) return;
      setResult(res);
    } catch (exc) {
      if (token !== inFlightToken) return;
      const err = exc instanceof Error ? exc : new Error(String(exc));
      setError(err);
      // Keep the previous result on screen — don't blank the pill
      // just because the network blipped.  Composer's manual override
      // is still functional.
    } finally {
      if (token === inFlightToken) setPending(false);
    }
  };

  const cancel = (): void => {
    if (timer != null) {
      clearTimeout(timer);
      timer = null;
    }
    // Bump the token so any in-flight call's response is dropped.
    inFlightToken += 1;
    setPending(false);
  };

  const setPrompt = (text: string): void => {
    const trimmed = (text ?? "").trim();
    lastPrompt = trimmed;
    if (timer != null) {
      clearTimeout(timer);
      timer = null;
    }
    if (trimmed.length < minLength) {
      // Empty / too short — clear the pill, cancel any pending call.
      cancel();
      setResult(null);
      return;
    }
    timer = setTimeout(() => {
      timer = null;
      inFlightToken += 1;
      const token = inFlightToken;
      // Guard against a setPrompt("") landing while the timer was
      // pending — only fire if the last seen prompt is still the
      // one that scheduled us.
      if (lastPrompt === trimmed) {
        void fire(trimmed, token);
      }
    }, debounceMs);
  };

  onCleanup(() => cancel());

  return { result, pending, error, setPrompt, cancel };
}
