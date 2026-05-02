import { type Component, For, Show } from "solid-js";
import { createQuery } from "@tanstack/solid-query";
import { A } from "@solidjs/router";
import { auth } from "../../lib/auth";
import { sessions, type ChatSession } from "../../lib/sessions";

interface SessionListProps {
  collapsed: boolean;
}

/**
 * Sidebar section listing the user's recent chat sessions.
 *
 * Hidden when collapsed (no room for titles) and when not signed in
 * (the endpoint requires auth, would 401 every poll).  Refetches on
 * window focus so a session created in another tab shows up here
 * after a tab switch.
 */
export const SessionList: Component<SessionListProps> = (props) => {
  const q = createQuery<{ sessions: ChatSession[] }>(() => ({
    queryKey: ["sessions", "list"],
    queryFn: () => sessions.list({ limit: 20 }),
    enabled: !!auth.user() && !props.collapsed,
    refetchOnWindowFocus: true,
    staleTime: 60_000,
  }));

  const sortedSessions = (): ChatSession[] => {
    const list = q.data?.sessions ?? [];
    return [...list].sort((a, b) => {
      // Pinned first, then most recent.
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
      const ta = new Date(a.updated_at ?? a.created_at ?? 0).getTime();
      const tb = new Date(b.updated_at ?? b.created_at ?? 0).getTime();
      return tb - ta;
    });
  };

  return (
    <Show when={!props.collapsed && auth.user()}>
      <p class="mt-6 mb-1.5 px-2 text-[0.65rem] font-semibold uppercase tracking-widest text-text-tertiary">
        Sessions
      </p>
      <Show
        when={!q.isLoading}
        fallback={
          <p class="px-2 text-xs text-text-tertiary">Loading…</p>
        }
      >
        <Show
          when={sortedSessions().length > 0}
          fallback={
            <p class="px-2 text-xs text-text-tertiary">
              No sessions yet.
            </p>
          }
        >
          <ul class="space-y-0.5">
            <For each={sortedSessions().slice(0, 12)}>
              {(s) => <SessionRow session={s} />}
            </For>
          </ul>
        </Show>
      </Show>
    </Show>
  );
};

const SessionRow: Component<{ session: ChatSession }> = (props) => {
  const title = (): string => {
    const t = props.session.title?.trim();
    if (t) return t;
    return `Session ${(props.session.id ?? props.session.session_id ?? "?").slice(0, 8)}`;
  };

  const href = (): string => {
    // Route to the matching mode if known; falls back to home.
    const mode = props.session.mode;
    switch (mode) {
      case "research":
      case "thinking":
      case "build":
      case "code":
      case "consortium":
      case "sentinel":
        return `/${mode === "code" ? "build" : mode}`;
      default:
        return "/";
    }
  };

  return (
    <li>
      <A
        href={href()}
        class="flex items-center gap-2 rounded-md px-2 py-1 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
      >
        <Show when={props.session.pinned}>
          <span class="text-text-tertiary" aria-hidden="true">
            ★
          </span>
        </Show>
        <span class="truncate">{title()}</span>
      </A>
    </li>
  );
};
