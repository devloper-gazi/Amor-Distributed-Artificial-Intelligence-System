import {
  type Component,
  For,
  Show,
  createSignal,
  createEffect,
  onCleanup,
  onMount,
} from "solid-js";
import {
  createQuery,
  useQueryClient,
  createMutation,
} from "@tanstack/solid-query";
import { A } from "@solidjs/router";
import { auth } from "../../lib/auth";
import { sessions, type ChatSession } from "../../lib/sessions";
import { Modal, Button, Input } from "../ui";

interface SessionListProps {
  collapsed: boolean;
}

/** Relative-time helper. */
function relativeTime(iso: string | undefined): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const diff = (Date.now() - t) / 1000;
  if (diff < 30) return "just now";
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 14) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

type Pending =
  | { kind: "rename"; session: ChatSession }
  | { kind: "delete"; session: ChatSession }
  | null;

/**
 * Sidebar section listing the user's chat sessions.  Now shows
 * ALL sessions (was capped at 20 in the prior build), scrolls in
 * a fixed-height container, and uses in-app modals for rename +
 * delete instead of the browser-native ``confirm()`` / ``prompt()``
 * which surfaced ugly "localhost:8000 says" banners.
 *
 * Backend listing already filters by ``user_id`` only (not by
 * ``X-Client-Id``) so sessions from older v1 sessions / different
 * tabs of the same user account stay visible.  When the user has
 * >100 sessions the fetch caps at 100 — pagination can be added
 * later if anyone hits the cap routinely.
 */
export const SessionList: Component<SessionListProps> = (props) => {
  const qc = useQueryClient();
  const [pending, setPending] = createSignal<Pending>(null);

  const q = createQuery<{ sessions: ChatSession[] }>(() => ({
    queryKey: ["sessions", "list"],
    queryFn: () => sessions.list({ limit: 100, archived: true }),
    enabled: !!auth.user() && !props.collapsed,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  }));

  const renameMutation = createMutation(() => ({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      sessions.update(id, { title }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions", "list"] }),
  }));

  const archiveMutation = createMutation(() => ({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) =>
      sessions.update(id, { archived }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions", "list"] }),
  }));

  const pinMutation = createMutation(() => ({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) =>
      sessions.update(id, { pinned }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions", "list"] }),
  }));

  const deleteMutation = createMutation(() => ({
    mutationFn: (id: string) => sessions.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions", "list"] }),
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
      <div class="mt-6 flex items-center justify-between px-2 mb-1.5">
        <p class="text-[0.65rem] font-semibold uppercase tracking-widest text-text-tertiary">
          Sessions
        </p>
        <Show when={(q.data?.sessions?.length ?? 0) > 0}>
          <span class="text-[0.6rem] text-text-tertiary tabular-nums">
            {q.data!.sessions.length}
          </span>
        </Show>
      </div>
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
          <ul class="max-h-[40vh] overflow-y-auto space-y-0.5 pr-0.5">
            <For each={sortedSessions()}>
              {(s) => (
                <SessionRow
                  session={s}
                  onRename={() => setPending({ kind: "rename", session: s })}
                  onArchive={() =>
                    archiveMutation.mutate({
                      id: s.id,
                      archived: !s.archived,
                    })
                  }
                  onPin={() =>
                    pinMutation.mutate({ id: s.id, pinned: !s.pinned })
                  }
                  onDelete={() => setPending({ kind: "delete", session: s })}
                />
              )}
            </For>
          </ul>
        </Show>
      </Show>

      {/* Rename + Delete modals — in-app, NOT browser-native. */}
      <RenameModal
        pending={pending()?.kind === "rename" ? pending() : null}
        onClose={() => setPending(null)}
        onConfirm={(title) => {
          const p = pending();
          if (p?.kind === "rename") {
            renameMutation.mutate({ id: p.session.id, title });
          }
          setPending(null);
        }}
      />
      <ConfirmDeleteModal
        pending={pending()?.kind === "delete" ? pending() : null}
        onClose={() => setPending(null)}
        onConfirm={() => {
          const p = pending();
          if (p?.kind === "delete") {
            deleteMutation.mutate(p.session.id);
          }
          setPending(null);
        }}
      />
    </Show>
  );
};

interface SessionRowProps {
  session: ChatSession;
  onRename: () => void;
  onArchive: () => void;
  onPin: () => void;
  onDelete: () => void;
}

const SessionRow: Component<SessionRowProps> = (props) => {
  const [menuOpen, setMenuOpen] = createSignal(false);

  const title = (): string => {
    const t = props.session.title?.trim();
    if (t) return t;
    return `Session ${(props.session.id ?? props.session.session_id ?? "?").slice(0, 8)}`;
  };

  const href = (): string => {
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

  const fire = (e: MouseEvent, fn: () => void): void => {
    e.preventDefault();
    e.stopPropagation();
    setMenuOpen(false);
    fn();
  };

  return (
    <li class="group relative">
      <A
        href={href()}
        class={[
          "flex items-center gap-2 rounded-md px-2 py-1.5 pr-7 text-xs",
          "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
          props.session.archived ? "opacity-60" : "",
        ].join(" ")}
        title={`${title()} · ${props.session.mode ?? "unknown"} · ${relativeTime(
          props.session.updated_at ?? props.session.created_at,
        )}${props.session.archived ? " · archived" : ""}`}
      >
        <Show when={props.session.pinned}>
          <span class="text-text-tertiary" aria-hidden="true">
            ★
          </span>
        </Show>
        <Show when={props.session.archived}>
          <span class="text-text-tertiary" aria-hidden="true" title="Archived">
            ◌
          </span>
        </Show>
        <span class="flex min-w-0 flex-1 flex-col">
          <span class="truncate">{title()}</span>
          <span class="truncate text-[0.6rem] text-text-tertiary">
            {props.session.mode ?? "—"} ·{" "}
            {relativeTime(
              props.session.updated_at ?? props.session.created_at,
            )}
          </span>
        </span>
      </A>
      <button
        type="button"
        class={[
          "absolute right-1 top-1.5 flex h-6 w-6 items-center justify-center",
          "rounded text-text-tertiary hover:bg-bg-tertiary hover:text-text-primary",
          "focus-visible:outline-2 focus-visible:outline-offset-1",
          menuOpen()
            ? "bg-bg-tertiary text-text-primary opacity-100"
            : "opacity-0 group-hover:opacity-100 focus:opacity-100",
        ].join(" ")}
        aria-label="Session actions"
        aria-haspopup="menu"
        aria-expanded={menuOpen()}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setMenuOpen((o) => !o);
        }}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      <Show when={menuOpen()}>
        <SessionMenu
          archived={!!props.session.archived}
          pinned={!!props.session.pinned}
          onClose={() => setMenuOpen(false)}
          onRename={(e) => fire(e, props.onRename)}
          onArchive={(e) => fire(e, props.onArchive)}
          onPin={(e) => fire(e, props.onPin)}
          onDelete={(e) => fire(e, props.onDelete)}
        />
      </Show>
    </li>
  );
};

const SessionMenu: Component<{
  onRename: (e: MouseEvent) => void;
  onArchive: (e: MouseEvent) => void;
  onPin: (e: MouseEvent) => void;
  onDelete: (e: MouseEvent) => void;
  archived: boolean;
  pinned: boolean;
  onClose: () => void;
}> = (props) => {
  /** Outside-click + Esc dismissal.  Critical: attach the document
   *  listener AFTER the click that opened the menu finishes (one
   *  microtask delay) so the trigger click doesn't immediately
   *  fire onClose.  And REMOVE the listener on unmount so a stale
   *  leaked handler from a previous open can't auto-close the
   *  next-opened instance. */
  const onDocClick = (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    if (!target?.closest("[data-amor-session-menu]")) props.onClose();
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") props.onClose();
  };

  let attachTimer: ReturnType<typeof setTimeout> | null = null;
  let attached = false;

  onMount(() => {
    attachTimer = setTimeout(() => {
      document.addEventListener("click", onDocClick);
      document.addEventListener("keydown", onKey);
      attached = true;
    }, 0);
  });
  onCleanup(() => {
    if (attachTimer) clearTimeout(attachTimer);
    if (attached) {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKey);
    }
  });

  return (
    <div
      data-amor-session-menu
      role="menu"
      class="absolute right-1 top-9 z-[var(--z-dropdown)] min-w-32 overflow-hidden rounded-md border border-border-subtle bg-bg-elevated shadow-md"
    >
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-primary hover:bg-bg-hover"
        onClick={props.onRename}
      >
        Rename
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-primary hover:bg-bg-hover"
        onClick={props.onPin}
      >
        {props.pinned ? "Unpin" : "Pin"}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-primary hover:bg-bg-hover"
        onClick={props.onArchive}
      >
        {props.archived ? "Restore" : "Archive"}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs hover:bg-bg-hover"
        style={{ color: "var(--color-status-failed)" }}
        onClick={props.onDelete}
      >
        Delete
      </button>
    </div>
  );
};

const RenameModal: Component<{
  pending: Pending;
  onClose: () => void;
  onConfirm: (title: string) => void;
}> = (props) => {
  const [draft, setDraft] = createSignal("");
  const open = (): boolean =>
    props.pending !== null && props.pending.kind === "rename";

  const session = (): ChatSession | null => {
    const p = props.pending;
    return p && p.kind === "rename" ? p.session : null;
  };

  // Initial value on open.
  const initialTitle = (): string => {
    const s = session();
    return s?.title?.trim() || `Session ${s?.id?.slice(0, 8) ?? ""}`;
  };

  // Re-seed draft on every open transition.  ``createEffect`` re-runs
  // when ``open()`` changes, so the input pre-fills with the
  // session's current title each time the modal appears (the prior
  // ``ensureSeeded`` ran once at component init and missed every
  // subsequent open).
  let lastOpen = false;
  createEffect(() => {
    const isOpen = open();
    if (isOpen && !lastOpen) setDraft(initialTitle());
    lastOpen = isOpen;
  });

  const submit = () => {
    const next = draft().trim();
    if (next.length === 0) {
      props.onClose();
      return;
    }
    props.onConfirm(next);
  };

  return (
    <Modal
      open={open()}
      onClose={props.onClose}
      title="Rename session"
      description="Pick a name that helps you find this conversation later."
      size="md"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={props.onClose}>
            Cancel
          </Button>
          <Button size="sm" onClick={submit} disabled={!draft().trim()}>
            Save
          </Button>
        </>
      }
    >
      <Show when={open()}>
        <Input
          autofocus
          value={draft()}
          onInput={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") props.onClose();
          }}
          placeholder="Session title…"
        />
      </Show>
    </Modal>
  );
};

const ConfirmDeleteModal: Component<{
  pending: Pending;
  onClose: () => void;
  onConfirm: () => void;
}> = (props) => {
  const open = (): boolean =>
    props.pending !== null && props.pending.kind === "delete";
  const session = (): ChatSession | null => {
    const p = props.pending;
    return p && p.kind === "delete" ? p.session : null;
  };

  return (
    <Modal
      open={open()}
      onClose={props.onClose}
      title="Delete this session?"
      description={
        session()
          ? `"${session()?.title || `Session ${session()?.id?.slice(0, 8)}`}" will be removed permanently.  This cannot be undone.`
          : ""
      }
      size="md"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={props.onClose}>
            Cancel
          </Button>
          <Button variant="danger" size="sm" onClick={props.onConfirm}>
            Delete
          </Button>
        </>
      }
    />
  );
};
