/**
 * Cycle UI Phase 4 — BranchNavigator: `< N/M >` sibling arrows
 * under user messages that have multiple regeneration branches.
 *
 * Backed by GET /api/sessions/{id}/siblings/{parent_id} and
 * POST /api/sessions/{id}/leaf for the navigation.  Renders nothing
 * when the message has 0 or 1 siblings (the common single-branch
 * conversation case).
 *
 * Keyboard:
 *   * ArrowLeft  → previous sibling (wraps)
 *   * ArrowRight → next sibling (wraps)
 *   * Enter / Space on the counter → no-op (counter is informational)
 *
 * Accessibility:
 *   * Buttons have aria-label so screen readers announce them as
 *     "Previous version" / "Next version".
 *   * The counter has role="status" + aria-live="polite" so the
 *     position update is announced.
 *   * 44×44 touch targets via the .amor-touch utility (Sprint 11).
 */

import {
  type Component,
  Show,
  createSignal,
} from "solid-js";
import { t } from "../../i18n";

export interface BranchNavigatorProps {
  /** Index of the currently-shown branch (1-based for display). */
  current: number;
  /** Total number of sibling branches under the same parent. */
  total: number;
  /** Called when the user arrows left / right.  Receives the
   *  1-based target index.  Caller is responsible for the
   *  POST /api/sessions/{id}/leaf round-trip + thread refresh. */
  onSelect: (targetIndex: number) => void | Promise<void>;
  /** Optional — when true, the navigator renders but its buttons
   *  are disabled (e.g. while a flip is in flight). */
  busy?: boolean;
}

export const BranchNavigator: Component<BranchNavigatorProps> = (props) => {
  const [pending, setPending] = createSignal(false);

  const go = async (delta: -1 | 1) => {
    if (pending() || props.busy) return;
    const next = props.current + delta;
    // Wrap [1, total].
    const wrapped =
      next < 1 ? props.total : next > props.total ? 1 : next;
    if (wrapped === props.current) return;
    setPending(true);
    try {
      await props.onSelect(wrapped);
    } finally {
      setPending(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      void go(-1);
      return;
    }
    if (e.key === "ArrowRight") {
      e.preventDefault();
      void go(1);
      return;
    }
  };

  return (
    <Show when={props.total > 1}>
      <div
        class="inline-flex items-center gap-1 rounded-md border border-border-subtle bg-bg-elevated px-1 py-0.5 text-[0.7rem] text-text-tertiary"
        role="group"
        aria-label={t("branch.navigator_aria")}
        onKeyDown={onKeyDown}
        data-amor-branch-nav=""
      >
        <button
          type="button"
          class="amor-touch flex h-6 w-6 items-center justify-center rounded hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 disabled:opacity-40"
          aria-label={t("branch.previous_aria")}
          disabled={pending() || props.busy}
          onClick={() => void go(-1)}
          data-amor-branch-prev=""
        >
          ◂
        </button>
        <span
          role="status"
          aria-live="polite"
          class="min-w-[2.5rem] text-center tabular-nums font-medium"
          data-amor-branch-counter=""
        >
          {props.current}/{props.total}
        </span>
        <button
          type="button"
          class="amor-touch flex h-6 w-6 items-center justify-center rounded hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 disabled:opacity-40"
          aria-label={t("branch.next_aria")}
          disabled={pending() || props.busy}
          onClick={() => void go(1)}
          data-amor-branch-next=""
        >
          ▸
        </button>
      </div>
    </Show>
  );
};

export default BranchNavigator;
