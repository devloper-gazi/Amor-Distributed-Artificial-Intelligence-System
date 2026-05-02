import { type Component, For, Show, createEffect } from "solid-js";
import type { ChatTurn } from "../../lib/types";
import { MessageBubble } from "./MessageBubble";

interface MessageThreadProps {
  turns: ChatTurn[];
  emptyState?: import("solid-js").JSX.Element;
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
          {(turn) => <MessageBubble turn={turn} />}
        </For>
        <div class="h-2" aria-hidden="true" />
      </Show>
    </div>
  );
};
