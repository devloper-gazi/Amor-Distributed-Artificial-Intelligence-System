import {
  type Component,
  For,
  Show,
  createSignal,
  createMemo,
  createEffect,
  onCleanup,
  onMount,
} from "solid-js";
import {
  createQuery,
  useQueryClient,
  createMutation,
} from "@tanstack/solid-query";
import { A, useLocation } from "@solidjs/router";
import { auth } from "../../lib/auth";
import { sessions, type ChatSession } from "../../lib/sessions";
import { Modal, Button, Input } from "../ui";
import { t } from "../../i18n";

interface SessionListProps {
  collapsed: boolean;
}

/**
 * Cycle D — Sessions list polish.
 *
 * The previous build rendered every session as an unstyled row with
 * no status differentiation, no mode tint, and no recency grouping.
 * The user reported it as "Test · code · 12d ago" with no signal as
 * to whether the session was active, completed, or archived.
 *
 * The backend has NO server-side ``status`` field — sessions just
 * have ``updated_at`` / ``archived`` / ``pinned``.  We derive a
 * client-side activity status from those fields:
 *
 *   pinned                          → "pinned"  (gold ★, top group)
 *   archived                        → "archived" (◌ chip, dimmed)
 *   updated_at < 60s                → "active"   (pulsing emerald)
 *   updated_at < 1h                 → "recent"   (cool blue)
 *   updated_at < 24h                → "idle"     (amber)
 *   else                            → "stale"    (subdued gray)
 *
 * Mode chip colours are pulled from ``--color-mode-*`` tokens already
 * present in ``styles/theme.css`` — the chip dot exposes the mode at
 * a glance without opening the row.
 *
 * Active highlight: rows whose ``mode`` matches the current route
 * are tagged with a left accent bar so the operator can immediately
 * see "this is the page you're on".
 */


// ─── Status taxonomy ──────────────────────────────────────────────


export type ActivityStatus =
  | "active"
  | "recent"
  | "idle"
  | "stale"
  | "archived"
  | "pinned";

const ACTIVE_THRESHOLD_MS = 60 * 1000;       //  1 min
const RECENT_THRESHOLD_MS = 60 * 60 * 1000;  //  1 hour
const IDLE_THRESHOLD_MS   = 24 * 60 * 60 * 1000; // 24 hours

export function deriveActivityStatus(
  s: ChatSession,
  now: number = Date.now(),
): ActivityStatus {
  if (s.pinned) return "pinned";
  if (s.archived) return "archived";
  const ts = new Date(s.updated_at ?? s.created_at ?? 0).getTime();
  if (!Number.isFinite(ts) || ts <= 0) return "stale";
  const delta = now - ts;
  if (delta < ACTIVE_THRESHOLD_MS) return "active";
  if (delta < RECENT_THRESHOLD_MS) return "recent";
  if (delta < IDLE_THRESHOLD_MS) return "idle";
  return "stale";
}

const STATUS_COLOR: Record<ActivityStatus, string> = {
  active:   "var(--color-status-healthy)",
  recent:   "var(--color-mode-research)",
  idle:     "var(--color-status-warming)",
  stale:    "var(--color-text-tertiary)",
  archived: "var(--color-text-tertiary)",
  pinned:   "var(--color-status-warming)",
};


// ─── Mode → color token + label ───────────────────────────────────


function modeColorVar(mode: string | undefined): string {
  switch (mode) {
    case "research":   return "var(--color-mode-research)";
    case "thinking":   return "var(--color-mode-thinking)";
    case "build":
    case "code":       return "var(--color-mode-build)";
    case "consortium": return "var(--color-mode-consortium)";
    case "sentinel":   return "var(--color-mode-sentinel)";
    default:           return "var(--color-text-tertiary)";
  }
}

function modeShortLabel(mode: string | undefined): string {
  // 3-4 char compact label for the chip badge.
  switch (mode) {
    case "research":   return t("mode.research.label").slice(0, 4);
    case "thinking":   return t("mode.thinking.label").slice(0, 4);
    case "build":
    case "code":       return t("mode.build.label").slice(0, 4);
    case "consortium": return t("mode.consortium.label").slice(0, 4);
    case "sentinel":   return t("mode.sentinel.label").slice(0, 4);
    case "system":     return t("mode.system.label").slice(0, 4);
    default:           return mode ? mode.slice(0, 4) : "—";
  }
}

function modeHref(mode: string | undefined): string {
  switch (mode) {
    case "research":
    case "thinking":
    case "build":
    case "consortium":
    case "sentinel":
      return `/${mode}`;
    case "code":
      return "/build";
    default:
      return "/";
  }
}


// ─── Localized relative-time ──────────────────────────────────────


export function relativeTime(iso: string | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return "";
  const diff = (now - ts) / 1000;
  if (diff < 30)        return t("time.just_now");
  if (diff < 60)        return t("time.seconds_ago", { n: Math.floor(diff) });
  if (diff < 3600)      return t("time.minutes_ago", { n: Math.floor(diff / 60) });
  if (diff < 86400)     return t("time.hours_ago",   { n: Math.floor(diff / 3600) });
  if (diff < 86400 * 14) return t("time.days_ago",   { n: Math.floor(diff / 86400) });
  return new Date(iso).toLocaleDateString();
}


// ─── Recency grouping ─────────────────────────────────────────────


type GroupKey = "pinned" | "today" | "this_week" | "older" | "archived";

interface SessionGroup {
  key: GroupKey;
  label: string;
  items: ChatSession[];
}

function groupSessions(items: ChatSession[], now: number = Date.now()): SessionGroup[] {
  const pinned: ChatSession[] = [];
  const today: ChatSession[] = [];
  const week: ChatSession[] = [];
  const older: ChatSession[] = [];
  const archived: ChatSession[] = [];

  for (const s of items) {
    if (s.archived) {
      archived.push(s);
      continue;
    }
    if (s.pinned) {
      pinned.push(s);
      continue;
    }
    const ts = new Date(s.updated_at ?? s.created_at ?? 0).getTime();
    if (!Number.isFinite(ts) || ts <= 0) {
      older.push(s);
      continue;
    }
    const delta = now - ts;
    if (delta < 24 * 60 * 60 * 1000) today.push(s);
    else if (delta < 7 * 24 * 60 * 60 * 1000) week.push(s);
    else older.push(s);
  }

  const byTime = (a: ChatSession, b: ChatSession): number => {
    const ta = new Date(a.updated_at ?? a.created_at ?? 0).getTime();
    const tb = new Date(b.updated_at ?? b.created_at ?? 0).getTime();
    return tb - ta;
  };
  pinned.sort(byTime);
  today.sort(byTime);
  week.sort(byTime);
  older.sort(byTime);
  archived.sort(byTime);

  const out: SessionGroup[] = [];
  if (pinned.length)
    out.push({ key: "pinned", label: t("sessions.status.pinned"), items: pinned });
  if (today.length)
    out.push({ key: "today", label: t("sessions.group.today"), items: today });
  if (week.length)
    out.push({ key: "this_week", label: t("sessions.group.this_week"), items: week });
  if (older.length)
    out.push({ key: "older", label: t("sessions.group.older"), items: older });
  if (archived.length)
    out.push({ key: "archived", label: t("sessions.group.archived"), items: archived });
  return out;
}


// ─── Auto-tick clock for "active" pulse + relative time ──────────


/** Refresh the activity-status derivation every 30 s so the
 *  "Active" pulse fades to "Recent" without a manual reload.
 *  Uses a single shared signal so the tick rate is paid once for
 *  the whole list, not per row. */
const [tickNow, setTickNow] = createSignal<number>(Date.now());
let tickerTimer: ReturnType<typeof setInterval> | null = null;
function ensureTicker(): void {
  if (tickerTimer) return;
  tickerTimer = setInterval(() => setTickNow(Date.now()), 30_000);
}


type Pending =
  | { kind: "rename"; session: ChatSession }
  | { kind: "delete"; session: ChatSession }
  | null;


// ─── Main component ───────────────────────────────────────────────


export const SessionList: Component<SessionListProps> = (props) => {
  const qc = useQueryClient();
  const location = useLocation();
  const [pending, setPending] = createSignal<Pending>(null);

  onMount(ensureTicker);
  onCleanup(() => {
    // Don't kill the shared ticker — other instances may still need it.
    // Kept here as a no-op so removing this list later doesn't leak the
    // interval (the module-level guard handles re-creation).
  });

  const q = createQuery<{ sessions: ChatSession[] }>(() => ({
    queryKey: ["sessions", "list"],
    queryFn: () => sessions.list({ limit: 100, archived: true }),
    enabled: !!auth.user() && !props.collapsed,
    refetchOnWindowFocus: true,
    // Cycle D — short staleTime + 15 s background poll so a session
    // started in another tab / by another route shows up without a
    // manual refresh.  Pipeline ``done`` events also trigger an
    // explicit invalidate via ``invalidateSessionsList()``, so the
    // typical case lands in <1 s.
    staleTime: 5_000,
    refetchInterval: 15_000,
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

  /** Current route → which mode is "active" (lit accent bar). */
  const currentMode = createMemo<string>(() => {
    const path = location.pathname.replace(/^\/+/, "").split("/")[0] ?? "";
    return path;
  });

  const groups = createMemo<SessionGroup[]>(() => {
    void tickNow(); // re-group when the clock ticks
    const list = q.data?.sessions ?? [];
    return groupSessions(list);
  });

  const total = createMemo<number>(() => q.data?.sessions?.length ?? 0);

  return (
    <Show when={!props.collapsed && auth.user()}>
      <div class="mt-6 flex items-center justify-between px-2 mb-1.5">
        <p class="text-[0.65rem] font-semibold uppercase tracking-widest text-text-tertiary">
          {t("sessions.title")}
        </p>
        <Show when={total() > 0}>
          <span class="text-[0.6rem] text-text-tertiary tabular-nums">
            {total()}
          </span>
        </Show>
      </div>
      <Show
        when={!q.isLoading}
        fallback={
          <p class="px-2 text-xs text-text-tertiary">
            {t("sessions.loading")}
          </p>
        }
      >
        <Show
          when={total() > 0}
          fallback={
            <p class="px-2 text-xs text-text-tertiary">
              {t("sessions.empty")}
            </p>
          }
        >
          <div class="max-h-[44vh] overflow-y-auto pr-0.5 space-y-2">
            <For each={groups()}>
              {(group) => (
                <section
                  data-amor-session-group={group.key}
                  aria-label={group.label}
                >
                  <h3 class="px-2 pb-0.5 text-[0.55rem] font-semibold uppercase tracking-wider text-text-tertiary/80">
                    {group.label}
                  </h3>
                  <ul class="space-y-0.5">
                    <For each={group.items}>
                      {(s) => (
                        <SessionRow
                          session={s}
                          isCurrentMode={
                            currentMode() === (s.mode === "code" ? "build" : s.mode)
                          }
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
                </section>
              )}
            </For>
          </div>
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


// ─── Single row ───────────────────────────────────────────────────


interface SessionRowProps {
  session: ChatSession;
  isCurrentMode: boolean;
  onRename: () => void;
  onArchive: () => void;
  onPin: () => void;
  onDelete: () => void;
}

const SessionRow: Component<SessionRowProps> = (props) => {
  const [menuOpen, setMenuOpen] = createSignal(false);

  const title = (): string => {
    const tt = props.session.title?.trim();
    if (tt) return tt;
    const id8 = (props.session.id ?? props.session.session_id ?? "?").slice(0, 8);
    return `${t("sessions.unnamed")} ${id8}`;
  };

  const status = createMemo<ActivityStatus>(() => {
    void tickNow();
    return deriveActivityStatus(props.session);
  });

  const statusLabel = (): string => {
    switch (status()) {
      case "active":   return t("sessions.status.active");
      case "recent":   return t("sessions.status.recent");
      case "idle":     return t("sessions.status.idle");
      case "stale":    return t("sessions.status.stale");
      case "archived": return t("sessions.status.archived");
      case "pinned":   return t("sessions.status.pinned");
    }
  };

  const fire = (e: MouseEvent, fn: () => void): void => {
    e.preventDefault();
    e.stopPropagation();
    setMenuOpen(false);
    fn();
  };

  const modeKey = (): string => props.session.mode ?? "";
  const isActive = (): boolean => status() === "active";

  return (
    <li class="group relative">
      <A
        href={modeHref(modeKey())}
        data-amor-session-row=""
        data-amor-session-status={status()}
        data-amor-session-mode={modeKey()}
        class={[
          "relative flex items-start gap-2 rounded-md px-2 py-1.5 pr-7 text-xs",
          "border border-transparent",
          "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
          "focus-visible:outline-2 focus-visible:outline-offset-1",
          props.isCurrentMode ? "bg-bg-hover/50 border-border-subtle" : "",
          props.session.archived ? "opacity-60" : "",
        ].join(" ")}
        title={`${title()} · ${statusLabel()} · ${relativeTime(
          props.session.updated_at ?? props.session.created_at,
        )}`}
        aria-current={props.isCurrentMode ? "page" : undefined}
      >
        {/* Mode-tinted left accent bar — visible only on the
            currently-selected mode, gives "you are here" cue. */}
        <Show when={props.isCurrentMode}>
          <span
            class="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r-full"
            style={{ background: modeColorVar(modeKey()) }}
            aria-hidden="true"
          />
        </Show>

        {/* Status dot — pulses when "active". */}
        <span
          class={[
            "mt-1 inline-block h-1.5 w-1.5 shrink-0 rounded-full",
            isActive() ? "motion-safe:animate-pulse" : "",
          ].join(" ")}
          style={{ background: STATUS_COLOR[status()] }}
          aria-label={statusLabel()}
          role="img"
        />

        <span class="flex min-w-0 flex-1 flex-col">
          <span class="flex items-center gap-1.5">
            <Show when={props.session.pinned}>
              <span
                class="text-[0.65rem] leading-none"
                style={{ color: "var(--color-status-warming)" }}
                aria-hidden="true"
                title={t("sessions.status.pinned")}
              >
                ★
              </span>
            </Show>
            <span class="truncate font-medium">{title()}</span>
          </span>
          <span class="mt-0.5 flex items-center gap-1.5 text-[0.6rem] text-text-tertiary">
            {/* Mode chip */}
            <Show when={modeKey()}>
              <span
                class="inline-flex items-center gap-1 rounded px-1 py-px text-[0.55rem] font-medium uppercase tracking-wide"
                style={{
                  background: "color-mix(in oklch, " + modeColorVar(modeKey()) + " 14%, transparent)",
                  color: modeColorVar(modeKey()),
                }}
              >
                {modeShortLabel(modeKey())}
              </span>
            </Show>
            <span class="truncate">
              {relativeTime(
                props.session.updated_at ?? props.session.created_at,
              )}
            </span>
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
        aria-label={t("sessions.actions_label")}
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


// ─── Action menu ──────────────────────────────────────────────────


const SessionMenu: Component<{
  onRename: (e: MouseEvent) => void;
  onArchive: (e: MouseEvent) => void;
  onPin: (e: MouseEvent) => void;
  onDelete: (e: MouseEvent) => void;
  archived: boolean;
  pinned: boolean;
  onClose: () => void;
}> = (props) => {
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
        {t("sessions.action.rename")}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-primary hover:bg-bg-hover"
        onClick={props.onPin}
      >
        {props.pinned
          ? t("sessions.action.unpin")
          : t("sessions.action.pin")}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-primary hover:bg-bg-hover"
        onClick={props.onArchive}
      >
        {props.archived
          ? t("sessions.action.restore")
          : t("sessions.action.archive")}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs hover:bg-bg-hover"
        style={{ color: "var(--color-status-failed)" }}
        onClick={props.onDelete}
      >
        {t("sessions.action.delete")}
      </button>
    </div>
  );
};


// ─── Modals ───────────────────────────────────────────────────────


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

  const initialTitle = (): string => {
    const s = session();
    return s?.title?.trim()
      || `${t("sessions.unnamed")} ${s?.id?.slice(0, 8) ?? ""}`;
  };

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
      title={t("sessions.rename.title")}
      description={t("sessions.rename.description")}
      size="md"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={props.onClose}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" onClick={submit} disabled={!draft().trim()}>
            {t("sessions.rename.save")}
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
          placeholder={t("sessions.rename.placeholder")}
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

  const description = (): string => {
    const s = session();
    if (!s) return "";
    const title =
      s.title?.trim()
      || `${t("sessions.unnamed")} ${s.id?.slice(0, 8) ?? ""}`;
    return t("sessions.delete.description", { title });
  };

  return (
    <Modal
      open={open()}
      onClose={props.onClose}
      title={t("sessions.delete.title")}
      description={description()}
      size="md"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={props.onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="danger" size="sm" onClick={props.onConfirm}>
            {t("sessions.action.delete")}
          </Button>
        </>
      }
    />
  );
};
