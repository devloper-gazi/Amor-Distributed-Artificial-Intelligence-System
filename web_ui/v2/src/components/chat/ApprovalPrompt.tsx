/**
 * Cycle F Sprint 5 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â inline approval card.
 *
 * Renders an `ApprovalPayload` (see `lib/types.ts`) in the chat
 * thread when an `approval_required` SSE event lands.  Visual
 * conventions match `ToolCallCard.tsx` (mode-accent left border,
 * status pill, `<details>` for arguments) so the surfaces feel
 * threaded.
 *
 * Lifecycle
 * ---------
 * * `pending` ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â both Approve / Deny buttons enabled; countdown
 *   shows remaining seconds.
 * * `in-flight` (Approve or Deny clicked) ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â buttons disabled,
 *   pill says "submitting".
 * * `approved` / `denied` / `timeout` ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â terminal; pill shows
 *   final state.
 *
 * Accessibility
 * -------------
 * * Card root is `role="group"` + `aria-live="polite"`.
 * * Buttons have visible focus rings via Tailwind's `focus-visible`.
 * * `aria-busy` is set during the POST so AT users hear it.
 */

import { type Component, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js";

import { api } from "../../lib/api";
import { t } from "../../i18n";
import type { ApprovalPayload } from "../../lib/types";


export interface ApprovalPromptProps {
  payload: ApprovalPayload;
  /** Called after the POST resolves so the parent can update the
   *  turn's status.  Receives the new status string. */
  onStatusChange?: (status: ApprovalPayload["status"]) => void;
}


const STATUS_TONE: Record<ApprovalPayload["status"], string> = {
  pending: "text-text-body",
  approved: "text-status-success",
  denied: "text-status-error",
  timeout: "text-text-subtle",
  error: "text-status-error",
};


const STATUS_KEY: Record<ApprovalPayload["status"], string> = {
  pending: "approval.status.pending",
  approved: "approval.status.approved",
  denied: "approval.status.denied",
  timeout: "approval.status.timeout",
  error: "approval.status.error",
};


function prettyJson(v: unknown): string {
  if (v === undefined || v === null) return "";
  try {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}


export const ApprovalPrompt: Component<ApprovalPromptProps> = (props) => {
  // Local mutable mirror so the card can transition without waiting
  // for the parent to re-pass props.  Re-syncs to props.payload on
  // initial mount; subsequent updates flow through `setStatus`.
  const [status, setStatus] = createSignal<ApprovalPayload["status"]>(
    props.payload.status,
  );
  const [inFlight, setInFlight] = createSignal(false);
  const [errorMsg, setErrorMsg] = createSignal<string | null>(
    props.payload.error ?? null,
  );

  // Countdown timer: tick once per second; transition to timeout
  // when remaining hits 0 (and the user hasn't decided yet).
  const startMs = Date.now();
  const [elapsedS, setElapsedS] = createSignal(0);
  let timer: ReturnType<typeof setInterval> | undefined;
  onMount(() => {
    timer = setInterval(() => {
      setElapsedS(Math.floor((Date.now() - startMs) / 1000));
      if (
        status() === "pending"
        && elapsedS() >= props.payload.timeout_s
      ) {
        setStatus("timeout");
        props.onStatusChange?.("timeout");
        if (timer !== undefined) {
          clearInterval(timer);
          timer = undefined;
        }
      }
    }, 1000);
  });
  onCleanup(() => {
    if (timer !== undefined) clearInterval(timer);
  });

  const remaining = createMemo(() =>
    Math.max(0, props.payload.timeout_s - elapsedS()),
  );

  const submit = async (approved: boolean): Promise<void> => {
    if (inFlight() || status() !== "pending") return;
    setInFlight(true);
    setErrorMsg(null);
    try {
      await api.post(
        `/api/approval/${encodeURIComponent(props.payload.request_id)}`,
        { approved },
      );
      const nextStatus: ApprovalPayload["status"] = approved
        ? "approved"
        : "denied";
      setStatus(nextStatus);
      props.onStatusChange?.(nextStatus);
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : String(err ?? "submit failed");
      setErrorMsg(msg);
      setStatus("error");
      props.onStatusChange?.("error");
    } finally {
      setInFlight(false);
    }
  };

  const isTerminal = () => status() !== "pending";
  const buttonsDisabled = () => isTerminal() || inFlight();

  // Compact category label for the i18n lookup, falling back to
  // raw category when no translation exists.
  const categoryLabel = createMemo(() => {
    const cat = (props.payload.category || "unclassified").toLowerCase();
    const key = `approval.category.${cat}`;
    const translated = t(key);
    // `t()` returns the key itself when missing ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â surface raw cat in
    // that case so we don't show "approval.category.foo".
    return translated === key ? cat : translated;
  });

  return (
    <div
      role="group"
      aria-live="polite"
      aria-busy={inFlight() ? "true" : "false"}
      aria-label={t("approval.title")}
      class="my-2 rounded-md border border-border-subtle bg-bg-elevated"
      style={{ "border-left": "2px solid var(--mode-accent)" }}
      data-amor-approval=""
      data-amor-request-id={props.payload.request_id}
      data-amor-status={status()}
    >
      <header class="flex items-center justify-between gap-2 px-3 py-2 text-xs">
        <div class="flex min-w-0 items-center gap-2">
          <span
            class={`text-[0.95rem] leading-none ${STATUS_TONE[status()]}`}
            aria-hidden="true"
          >
            {status() === "approved"
              ? "ÃƒÂ¢Ã…â€œÃ¢â‚¬Å“"
              : status() === "denied"
                ? "ÃƒÂ¢Ã…â€œÃ¢â‚¬â€"
                : status() === "timeout"
                  ? "ÃƒÂ¢Ã‚ÂÃ‚Â±"
                  : status() === "error"
                    ? "!"
                    : "?"}
          </span>
          <div class="flex min-w-0 flex-col gap-0.5">
            <strong class="truncate text-text-display">
              {t("approval.title")}
            </strong>
            <code class="truncate font-mono text-[0.7rem] text-text-body">
              {props.payload.tool_name}
              <Show when={props.payload.actor_role}>
                <span class="ml-1 text-text-subtle">
                  ({props.payload.actor_role})
                </span>
              </Show>
            </code>
          </div>
        </div>
        <div class="flex flex-col items-end gap-0.5">
          <span
            class={`text-[0.65rem] uppercase tracking-wide ${STATUS_TONE[status()]}`}
          >
            {t(STATUS_KEY[status()])}
          </span>
          <Show when={!isTerminal()}>
            <span
              class={`text-[0.65rem] ${remaining() <= 10 ? "text-status-warning" : "text-text-subtle"}`}
            >
              {t("approval.timeout_warning", { seconds: String(remaining()) })}
            </span>
          </Show>
        </div>
      </header>

      <p class="px-3 pb-1 text-[0.8rem] text-text-body">
        {t("approval.subtitle", {
          category: categoryLabel(),
          tool: props.payload.tool_name,
        })}
      </p>

      <Show
        when={
          props.payload.arguments
          && Object.keys(props.payload.arguments).length > 0
        }
      >
        <details class="border-t border-border-subtle">
          <summary class="cursor-pointer px-3 py-1.5 text-[0.7rem] text-text-subtle hover:text-text-body">
            {t("approval.arguments")}
          </summary>
          <pre class="max-h-40 overflow-auto px-3 pb-2 font-mono text-[0.7rem] text-text-body">
            {prettyJson(props.payload.arguments)}
          </pre>
        </details>
      </Show>

      <Show when={errorMsg()}>
        <p class="border-t border-border-subtle px-3 py-1.5 text-[0.7rem] text-status-error">
          {errorMsg()}
        </p>
      </Show>

      <Show when={!isTerminal()}>
        <div class="flex items-center justify-end gap-2 border-t border-border-subtle px-3 py-2">
          <button
            type="button"
            class="amor-touch rounded border border-border-subtle bg-bg-base px-3 py-1 text-xs text-text-body hover:bg-bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-mode-accent disabled:cursor-not-allowed disabled:opacity-50"
            disabled={buttonsDisabled()}
            onClick={() => void submit(false)}
            data-amor-deny=""
          >
            {t("approval.deny")}
          </button>
          <button
            type="button"
            class="amor-touch rounded border border-mode-accent bg-mode-accent/10 px-3 py-1 text-xs font-semibold text-mode-accent hover:bg-mode-accent/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-mode-accent disabled:cursor-not-allowed disabled:opacity-50"
            disabled={buttonsDisabled()}
            onClick={() => void submit(true)}
            data-amor-approve=""
          >
            {t("approval.approve")}
          </button>
        </div>
      </Show>
    </div>
  );
};
