/**
 * Cycle C Sprint 4 Day 3 — message hover-actions bar.
 *
 * Rendered inside ``MessageBubble``; visible on hover/focus, hidden
 * by default.  Five actions, all optional — the bubble parent decides
 * which to wire up:
 *
 *   * copy        — writes ``turn.content`` to ``navigator.clipboard``
 *                   then flashes a short "copied" indicator.
 *   * edit        — emits ``onEdit(turn)``; only sensible on user turns.
 *   * regenerate  — emits ``onRegenerate(turn)``; only sensible on
 *                   assistant turns.
 *   * branch      — emits ``onBranch(turn)``; opens a new chat from
 *                   the turn's history (Day 4 wires the dispatch).
 *   * rate ±      — emits ``onRate(turn, 1 | -1)``; persists the rating
 *                   in ``localStorage["amor.rate.<turn.id>"]`` so a
 *                   refresh re-paints the indicator and Sprint 6's
 *                   ORPO collector can pick the pairs up.
 *
 * Hidden by default via the ``opacity-0 group-hover:opacity-100`` Tailwind
 * pattern — no Solid signal needed for the show/hide.  The wrapper
 * ``MessageBubble`` already has ``class="group"`` (Day 3 change).
 *
 * Accessibility
 * -------------
 * * Every IconButton carries an ``aria-label`` (enforced by the
 *   IconButton type contract).
 * * Rate buttons toggle ``aria-pressed`` so screen readers announce
 *   the current rating.
 * * The action bar wrapper carries ``role="toolbar"`` per ARIA APG.
 */

import {
  type Component,
  Show,
  createMemo,
  createSignal,
  onCleanup,
} from "solid-js";

import { IconButton } from "../ui";
import type { ChatTurn } from "../../lib/types";
import { t } from "../../i18n";

const RATE_LS_PREFIX = "amor.rate.";

/** Read a saved ±1 rating for ``turn.id`` from localStorage. */
function loadRate(id: string): 0 | 1 | -1 {
  try {
    const raw = localStorage.getItem(`${RATE_LS_PREFIX}${id}`);
    if (raw === "1") return 1;
    if (raw === "-1") return -1;
  } catch {
    // ignore
  }
  return 0;
}

function saveRate(id: string, value: 0 | 1 | -1): void {
  try {
    if (value === 0) {
      localStorage.removeItem(`${RATE_LS_PREFIX}${id}`);
    } else {
      localStorage.setItem(`${RATE_LS_PREFIX}${id}`, String(value));
    }
  } catch {
    // ignore
  }
}

export interface MessageActionsProps {
  turn: ChatTurn;
  /** Override ``navigator.clipboard.writeText`` (test injection). */
  copyImpl?: (text: string) => Promise<void>;
  onEdit?: (turn: ChatTurn) => void;
  onRegenerate?: (turn: ChatTurn) => void;
  onBranch?: (turn: ChatTurn) => void;
  /** ``value`` is +1 (thumbs up), -1 (thumbs down) or 0 (toggle off). */
  onRate?: (turn: ChatTurn, value: 0 | 1 | -1) => void;
}

export const MessageActions: Component<MessageActionsProps> = (props) => {
  const [copied, setCopied] = createSignal(false);
  const [rating, setRating] = createSignal<0 | 1 | -1>(loadRate(props.turn.id));
  let copyTimer: number | null = null;

  onCleanup(() => {
    if (copyTimer !== null) window.clearTimeout(copyTimer);
  });

  const isUser = createMemo(() => props.turn.role === "user");
  const isAssistant = createMemo(() => props.turn.role === "assistant");

  const onCopy = async () => {
    const impl = props.copyImpl ?? defaultCopy;
    try {
      await impl(props.turn.content);
      setCopied(true);
      if (copyTimer !== null) window.clearTimeout(copyTimer);
      copyTimer = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Browsers can deny clipboard writes when not focused.  We
      // intentionally swallow — the visible state simply doesn't
      // flip.  Day 5 a11y audit will flag the missing announcement.
    }
  };

  const setRate = (next: 0 | 1 | -1) => {
    const final: 0 | 1 | -1 = rating() === next ? 0 : next;
    setRating(final);
    saveRate(props.turn.id, final);
    props.onRate?.(props.turn, final);
    // Cycle C Sprint 6 Day 1 — record the (chosen, rejected) pair so
    // the weekly ORPO trainer has data to consume.  Privacy-by-default:
    // raw text is NOT sent (opt_in_raw=false).  Only the turn id +
    // hash + mode are persisted server-side.  Errors are silently
    // swallowed — telemetry must NEVER block the UI.
    if (final === 0) return;
    const isUp = final === 1;
    const body = {
      mode: "build",
      // For now we only track which turn the user reacted to; pair
      // semantics ("chosen" vs "rejected") will tighten in Day 4 once
      // we have a "regenerate then rate" flow.  Today an up-vote on a
      // turn means "this is the chosen variant"; a down-vote means
      // "this is the rejected variant".
      chosen_turn_id: isUp ? props.turn.id : null,
      rejected_turn_id: isUp ? null : props.turn.id,
      opt_in_raw: false,
    };
    void fetch("/api/admin/training/pairs", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => {
      // ignore — the rate button still flips locally even if the
      // backend can't capture it (e.g. user signed out, network down).
    });
  };

  // Cycle UI v2.5 Phase 2 — mobile ⋯ menu split.  Below `md` the
  // hover-reveal pattern is meaningless (no hover on touch); collapse
  // the action toolbar into a native <details>/<summary> menu instead.
  // Same buttons + callbacks; just a different visual chrome.  No
  // new dep (Kobalte DropdownMenu would have been overkill here).
  //
  // The actual <IconButton> JSX is shared between mobile + desktop
  // via the `actionButtons` JSX fragment captured below so we don't
  // duplicate ~80 LOC of button blocks.
  const actionButtons = (
    <>
      <IconButton
        size="sm"
        aria-label={copied() ? t("message.copied") : t("message.copy")}
        onClick={onCopy}
        title={copied() ? t("message.copied_status") : t("message.copy")}
        data-amor-action="copy"
      >
        <span aria-hidden="true">{copied() ? "✓" : "⧉"}</span>
      </IconButton>
      <Show when={isUser() && props.onEdit}>
        <IconButton
          size="sm"
          aria-label={t("message.edit")}
          onClick={() => props.onEdit?.(props.turn)}
          title={t("message.edit")}
          data-amor-action="edit"
        >
          <span aria-hidden="true">✎</span>
        </IconButton>
      </Show>
      <Show when={isAssistant() && props.onRegenerate}>
        <IconButton
          size="sm"
          aria-label={t("message.regenerate")}
          onClick={() => props.onRegenerate?.(props.turn)}
          title={t("message.regenerate")}
          data-amor-action="regenerate"
        >
          <span aria-hidden="true">↻</span>
        </IconButton>
      </Show>
      <Show when={props.onBranch}>
        <IconButton
          size="sm"
          aria-label={t("message.branch")}
          onClick={() => props.onBranch?.(props.turn)}
          title={t("message.branch")}
          data-amor-action="branch"
        >
          <span aria-hidden="true">⎇</span>
        </IconButton>
      </Show>
      <Show when={isAssistant()}>
        <span
          class="ml-1 mr-0.5 h-4 w-px bg-border-subtle"
          aria-hidden="true"
        />
        <IconButton
          size="sm"
          aria-label={t("message.rate_up")}
          aria-pressed={rating() === 1}
          onClick={() => setRate(1)}
          title={t("message.rate_up")}
          class={rating() === 1 ? "text-text-display bg-bg-hover" : ""}
          data-amor-action="rate-up"
          data-amor-rate-state={rating() === 1 ? "on" : "off"}
        >
          <span aria-hidden="true">▲</span>
        </IconButton>
        <IconButton
          size="sm"
          aria-label={t("message.rate_down")}
          aria-pressed={rating() === -1}
          onClick={() => setRate(-1)}
          title={t("message.rate_down")}
          class={rating() === -1 ? "text-text-display bg-bg-hover" : ""}
          data-amor-action="rate-down"
          data-amor-rate-state={rating() === -1 ? "on" : "off"}
        >
          <span aria-hidden="true">▼</span>
        </IconButton>
      </Show>
    </>
  );

  return (
    <div data-amor-message-actions="">
      {/* Mobile: ⋯ menu — visible below md only. */}
      <details
        class="amor-touch md:hidden mt-2 inline-block align-middle"
        data-amor-mobile-actions=""
      >
        <summary
          class="amor-touch inline-flex h-9 w-9 items-center justify-center rounded text-text-subtle hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 list-none cursor-pointer"
          aria-label={t("message.toolbar_label", { role: props.turn.role })}
        >
          <span aria-hidden="true">⋯</span>
        </summary>
        <div
          role="toolbar"
          aria-label={t("message.toolbar_label", { role: props.turn.role })}
          class="mt-1 flex flex-wrap items-center gap-1 rounded-md border border-border-subtle bg-bg-elevated p-1 shadow-md"
          data-amor-actions-shell="mobile"
        >
          {actionButtons}
        </div>
      </details>

      {/* Desktop: original hover-reveal toolbar.  Hidden below md. */}
      <div
        role="toolbar"
        aria-label={t("message.toolbar_label", { role: props.turn.role })}
        class="hidden md:flex mt-2 items-center gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-within:opacity-100"
        data-amor-actions-shell="desktop"
      >
        {actionButtons}

      <Show when={copied()}>
        <span
          class="ml-2 text-[0.65rem] text-text-subtle"
          role="status"
          aria-live="polite"
        >
          {t("message.copied_status")}
        </span>
      </Show>
      </div>
    </div>
  );
};

async function defaultCopy(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard) {
    await navigator.clipboard.writeText(text);
    return;
  }
  // Fallback for environments without the Clipboard API (e.g. file://
  // origins, headless browsers).  Spec-compliant browsers use the
  // promise path above; this is purely a no-throw escape valve.
  throw new Error("clipboard unavailable");
}
