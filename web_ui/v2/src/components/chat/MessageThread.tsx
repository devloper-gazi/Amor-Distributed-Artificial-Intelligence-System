import { type Component, For, Show, createEffect } from "solid-js";
import type { ChatTurn } from "../../lib/types";
import { ApprovalPrompt } from "./ApprovalPrompt";
import { MessageBubble } from "./MessageBubble";
// Cycle UI v2.5 Phase 2 — Build-engine tool-call card.  Replaces the
// previous behaviour of UNIFIED_REDUCER appending markdown fences
// for code_ready/test_ready/execution_result/review_ready events.
import { ToolCallCardBuild } from "./ToolCallCardBuild";

interface MessageThreadProps {
  turns: ChatTurn[];
  emptyState?: import("solid-js").JSX.Element;
  /** Optional message-action handlers forwarded to every bubble.
   *  Day 3 introduces the hover-actions bar (copy / edit /
   *  regenerate / branch / rate); each handler is independent and
   *  any can be omitted — the bubble omits the corresponding button. */
  onEdit?: (turn: ChatTurn) => void;
  onRegenerate?: (turn: ChatTurn) => void;
  onBranch?: (turn: ChatTurn) => void;
  onRate?: (turn: ChatTurn, value: 0 | 1 | -1) => void;
}

/**
 * Scrollable virtualised-friendly thread.  For PR-4 we use a plain
 * list — turn counts in a single session top out around 50 turns
 * which is fine without virtualisation.  When a session blows past
 * a few hundred turns we'll swap in @tanstack/solid-virtual.
 *
 * Auto-scrolls to the bottom on new turns UNLESS the user has
 * scrolled up.  ``createEffect`` with a ``turns.length`` dependency
 * is enough — Solid's fine-grained reactivity skips work when the
 * length doesn't change.
 */
export const MessageThread: Component<MessageThreadProps> = (props) => {
  let containerRef: HTMLDivElement | undefined;

  const isAtBottom = (): boolean => {
    if (!containerRef) return true;
    const { scrollTop, scrollHeight, clientHeight } = containerRef;
    return scrollHeight - (scrollTop + clientHeight) < 64;
  };

  let userScrolledUp = false;
  const onScroll = () => {
    userScrolledUp = !isAtBottom();
  };

  createEffect(() => {
    // Track turns.length so this effect re-runs.
    void props.turns.length;
    if (!containerRef || userScrolledUp) return;
    queueMicrotask(() => {
      if (containerRef) {
        containerRef.scrollTop = containerRef.scrollHeight;
      }
    });
  });

  return (
    <div
      ref={containerRef}
      onScroll={onScroll}
      class="flex-1 overflow-y-auto"
    >
      <Show
        when={props.turns.length > 0}
        fallback={
          <div class="flex h-full items-center justify-center px-8 py-12">
            {props.emptyState ?? (
              <div class="max-w-md text-center text-sm text-text-tertiary">
                No messages yet.  Type below to start.
              </div>
            )}
          </div>
        }
      >
        <For each={props.turns}>
          {(turn) => {
            // Cycle UI v2.5 Phase 2 — route to one of three renderers
            // depending on the turn's role + payload:
            //   1. role=="approval" + approval payload → ApprovalPrompt
            //      (Cycle F Sprint 5 inline approval card; manages own
            //      resolution via POST /api/approval/{request_id}).
            //   2. role=="tool" + buildCard → ToolCallCardBuild
            //      (Build pipeline structured tool-call card).
            //   3. everything else → MessageBubble (default).
            if (turn.role === "approval" && turn.approval) {
              return <ApprovalPrompt payload={turn.approval} />;
            }
            if (turn.role === "tool" && turn.buildCard) {
              return <ToolCallCardBuild card={turn.buildCard} />;
            }
            return (
              <MessageBubble
                turn={turn}
                onEdit={props.onEdit}
                onRegenerate={props.onRegenerate}
                onBranch={props.onBranch}
                onRate={props.onRate}
              />
            );
          }}
        </For>
        <div class="h-2" aria-hidden="true" />
      </Show>
    </div>
  );
};
