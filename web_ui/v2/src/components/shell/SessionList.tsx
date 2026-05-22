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
import { t, localeUpper } from "../../i18n";
import { modeColorVar } from "../../lib/mode-color";

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

// Cycle UI v2.5 — STATUS_COLOR removed.  Activity status now
// surfaces only via italic timestamp + an sr-only label.  Kept here
// as a commented-out reference in case future polish brings back
// a colour-coded indicator (Sentinel mode, alarm states, etc.).
// const STATUS_COLOR: Record<ActivityStatus, string> = {
//   active:   "var(--color-status-healthy)",
//   recent:   "var(--color-mode-research)",
//   idle:     "var(--color-status-warming)",
//   stale:    "var(--color-text-tertiary)",
//   archived: "var(--color-text-tertiary)",
//   pinned:   "var(--color-status-warming)",
// };


// ─── Mode → color token + label ───────────────────────────────────


// Cycle UI v2.6 (Karar A) — `modeColorVar()` moved to lib/mode-color.ts
// so the Halo component can share the same vocabulary.  Import added
// at file head; this comment marks where the local function used to
// live (lines 102-112 in v2.5).

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

// Cycle UI v2.8.1 — `modeHref()` removed.  All session rows now
// route to /?c=<session_id>; UnifiedChat hydrates the transcript
// from the backend.  Mode-specific routes (/build, /research, …)
// can't accept ?c= since the v2.5 cutover.


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


// Cycle UI v2.8.3 — refined recency taxonomy.  Was: pinned / today /
// this_week / older / archived (5 groups).  Now: pinned / today /
// yesterday / past_week / past_month / older / archived (7 groups).
// The yesterday + past_month groups produce finer-grained intuition
// — operators can tell "I worked on this 4 days ago" without doing
// the mental arithmetic on a relative timestamp.
type GroupKey =
  | "pinned"
  | "today"
  | "yesterday"
  | "past_week"
  | "past_month"
  | "older"
  | "archived";

interface SessionGroup {
  key: GroupKey;
  label: string;
  items: ChatSession[];
}

export function groupSessions(items: ChatSession[], now: number = Date.now()): SessionGroup[] {
  const pinned: ChatSession[] = [];
  const today: ChatSession[] = [];
  const yesterday: ChatSession[] = [];
  const pastWeek: ChatSession[] = [];
  const pastMonth: ChatSession[] = [];
  const older: ChatSession[] = [];
  const archived: ChatSession[] = [];

  const DAY = 24 * 60 * 60 * 1000;

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
    if (delta < DAY) today.push(s);
    else if (delta < 2 * DAY) yesterday.push(s);
    else if (delta < 7 * DAY) pastWeek.push(s);
    else if (delta < 30 * DAY) pastMonth.push(s);
    else older.push(s);
  }

  const byTime = (a: ChatSession, b: ChatSession): number => {
    const ta = new Date(a.updated_at ?? a.created_at ?? 0).getTime();
    const tb = new Date(b.updated_at ?? b.created_at ?? 0).getTime();
    return tb - ta;
  };
  pinned.sort(byTime);
  today.sort(byTime);
  yesterday.sort(byTime);
  pastWeek.sort(byTime);
  pastMonth.sort(byTime);
  older.sort(byTime);
  archived.sort(byTime);

  const out: SessionGroup[] = [];
  if (pinned.length)
    out.push({ key: "pinned", label: t("sessions.status.pinned"), items: pinned });
  if (today.length)
    out.push({ key: "today", label: t("sessions.group.today"), items: today });
  if (yesterday.length)
    out.push({ key: "yesterday", label: t("sessions.group.yesterday"), items: yesterday });
  if (pastWeek.length)
    out.push({ key: "past_week", label: t("sessions.group.past_week"), items: pastWeek });
  if (pastMonth.length)
    out.push({ key: "past_month", label: t("sessions.group.past_month"), items: pastMonth });
  if (older.length)
    out.push({ key: "older", label: t("sessions.group.older"), items: older });
  if (archived.length)
    out.push({ key: "archived", label: t("sessions.group.archived"), items: archived });
  return out;
}


// ─── Density (compact ↔ comfortable) ─────────────────────────────


/** Cycle UI v2.8.3 — sessions density toggle.  "comfortable" is the
 *  v2.8.x baseline (2-line row, mode chip + timestamp).  "compact"
 *  is a Linear-style power-user mode that hides the secondary row
 *  and tightens vertical padding.  Persisted to localStorage; the
 *  signal lives at module scope so the SessionList header toggle and
 *  the rows share state without prop-drilling. */
export type SessionDensity = "compact" | "comfortable";

function loadDensity(): SessionDensity {
  if (typeof localStorage === "undefined") return "comfortable";
  try {
    return localStorage.getItem("amor.sessions.density") === "compact"
      ? "compact"
      : "comfortable";
  } catch {
    return "comfortable";
  }
}

function saveDensity(d: SessionDensity): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem("amor.sessions.density", d);
  } catch {
    /* ignore quota / disabled storage */
  }
}

const [sessionDensity, setSessionDensity] = createSignal<SessionDensity>(
  loadDensity(),
);

function toggleSessionDensity(): void {
  const next: SessionDensity =
    sessionDensity() === "comfortable" ? "compact" : "comfortable";
  setSessionDensity(next);
  saveDensity(next);
}


// ─── Mode filter chips (Cycle UI v2.8.4) ──────────────────────────


/** Modes that participate in the chip filter bar.  Order matches the
 *  composer's mode pill order so muscle memory carries over. */
export const FILTERABLE_MODES: readonly string[] = [
  "build",
  "research",
  "thinking",
  "consortium",
  "sentinel",
  "quickcode",
] as const;


// ─── Session title matcher (inline search filter) ─────────────────


export function matchSession(s: ChatSession, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  const title = (s.title ?? "").toLowerCase();
  if (title.includes(q)) return true;
  const mode = (s.mode ?? "").toLowerCase();
  if (mode.includes(q)) return true;
  // ID partial match — operators occasionally remember the first 4-6
  // characters of a session id from a CLI log.
  const id = (s.id ?? "").toLowerCase();
  if (id.startsWith(q)) return true;
  return false;
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

  // Cycle UI v2.8.3 — inline session search.  Filters in-memory by
  // title / mode / id-prefix BEFORE grouping so each group's count
  // reflects post-filter cardinality, not the raw list.
  const [searchQuery, setSearchQuery] = createSignal<string>("");
  let searchInputRef: HTMLInputElement | undefined;

  /** "/" keyboard shortcut → focus search input.  Skipped when the
   *  user is already typing in another field (composer, modal etc.)
   *  to avoid hijacking text input. */
  onMount(() => {
    if (typeof window === "undefined") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (searchInputRef && !props.collapsed) {
        e.preventDefault();
        searchInputRef.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    onCleanup(() => window.removeEventListener("keydown", onKey));
  });

  // Cycle UI v2.8.4 — mode filter chip bar.  Multi-select toggle:
  // clicking chip A adds A to the active set; clicking again removes it.
  // Empty set ⇒ no filter.  Chips combine with text-search additively
  // (both must pass).
  const [activeModes, setActiveModes] = createSignal<Set<string>>(new Set());
  const toggleModeFilter = (mode: string): void => {
    setActiveModes((prev) => {
      const next = new Set(prev);
      if (next.has(mode)) next.delete(mode);
      else next.add(mode);
      return next;
    });
  };
  const clearAllFilters = (): void => {
    setSearchQuery("");
    setActiveModes(new Set<string>());
  };

  const filteredSessions = createMemo<ChatSession[]>(() => {
    const all = q.data?.sessions ?? [];
    const query = searchQuery().trim();
    const modes = activeModes();
    if (!query && modes.size === 0) return all;
    return all.filter((s) => {
      if (modes.size > 0) {
        const canonical = s.mode === "code" ? "build" : (s.mode ?? "");
        if (!modes.has(canonical)) return false;
      }
      if (query && !matchSession(s, query)) return false;
      return true;
    });
  });

  const groups = createMemo<SessionGroup[]>(() => {
    void tickNow(); // re-group when the clock ticks
    return groupSessions(filteredSessions());
  });

  const total = createMemo<number>(() => q.data?.sessions?.length ?? 0);
  const filteredCount = createMemo<number>(() => filteredSessions().length);
  const isSearching = createMemo<boolean>(() => searchQuery().trim().length > 0);
  const isFiltering = createMemo<boolean>(
    () => searchQuery().trim().length > 0 || activeModes().size > 0,
  );

  return (
    <Show when={!props.collapsed && auth.user()}>
      {/* Cycle UI v2.8.3 — section header with inline density toggle.
          Density button is a tiny chip (40 → 44 px wide depending on
          locale).  Count chip moves to the right of density so the
          two share the trailing rail. */}
      <div class="mt-6 flex items-center justify-between gap-2 px-2 mb-1.5">
        <p class="text-[0.65rem] font-semibold tracking-widest text-text-subtle">
          {localeUpper(t("sessions.title"))}
        </p>
        <div class="flex items-center gap-1.5">
          <Show when={total() > 0}>
            <button
              type="button"
              aria-label={t("sessions.density.toggle_aria")}
              title={
                sessionDensity() === "comfortable"
                  ? t("sessions.density.switch_to_compact")
                  : t("sessions.density.switch_to_comfortable")
              }
              onClick={toggleSessionDensity}
              data-amor-density-toggle={sessionDensity()}
              class="inline-flex h-5 w-5 items-center justify-center rounded text-[0.7rem] leading-none text-text-subtle hover:bg-bg-hover hover:text-text-display focus-visible:outline-2 focus-visible:outline-offset-1"
            >
              <span aria-hidden="true">
                {sessionDensity() === "comfortable" ? "≣" : "≡"}
              </span>
            </button>
          </Show>
          <Show when={total() > 0 && !isFiltering()}>
            <span class="text-[0.6rem] text-text-subtle tabular-nums">
              {total()}
            </span>
          </Show>
          <Show when={isFiltering()}>
            <span class="text-[0.6rem] text-text-subtle tabular-nums">
              {filteredCount()}/{total()}
            </span>
          </Show>
        </div>
      </div>

      {/* Cycle UI v2.8.4 — mode filter chip bar.  Visible when there are
          ≥4 sessions (same gate as inline search; under that the list
          is short enough to scan visually).  Multi-select via click;
          clicking the same chip again deactivates.  When ANY filter
          is active, a "Tümünü temizle" reset link appears. */}
      <Show when={total() >= 4}>
        <div class="px-2 mb-1.5">
          <div class="flex flex-wrap gap-1" data-amor-session-filter-bar="">
            <For each={FILTERABLE_MODES}>
              {(m) => {
                const isActive = () => activeModes().has(m);
                return (
                  <button
                    type="button"
                    onClick={() => toggleModeFilter(m)}
                    aria-pressed={isActive()}
                    data-amor-mode-chip={m}
                    data-amor-mode-active={isActive() ? "1" : "0"}
                    title={t(`mode.${m}.label`)}
                    class="inline-flex items-center gap-1 rounded-full border px-1.5 py-px text-[0.55rem] font-medium tracking-wide transition-colors focus-visible:outline-2 focus-visible:outline-offset-1"
                    style={
                      isActive()
                        ? {
                            "border-color": modeColorVar(m),
                            background:
                              "color-mix(in oklch, " +
                              modeColorVar(m) +
                              " 22%, transparent)",
                            color: modeColorVar(m),
                          }
                        : {
                            "border-color":
                              "color-mix(in oklch, var(--color-border-subtle) 60%, transparent)",
                            color: "var(--color-text-subtle)",
                          }
                    }
                  >
                    <span aria-hidden="true">
                      {localeUpper(modeShortLabel(m))}
                    </span>
                  </button>
                );
              }}
            </For>
            <Show when={isFiltering()}>
              <button
                type="button"
                onClick={clearAllFilters}
                aria-label={t("sessions.filter.clear_all")}
                title={t("sessions.filter.clear_all")}
                class="ml-auto inline-flex items-center justify-center rounded-full px-1.5 py-px text-[0.55rem] text-text-mute hover:bg-bg-hover hover:text-text-body"
              >
                {localeUpper(t("sessions.filter.clear"))}
              </button>
            </Show>
          </div>
        </div>
      </Show>

      {/* Cycle UI v2.8.3 — inline session search.  Visible when the
          list has ≥4 sessions (small libraries don't need a filter
          and the empty input is visual noise).  Submit "/" anywhere
          to focus.  Esc clears + blurs. */}
      <Show when={total() >= 4}>
        <div class="px-2 mb-1.5">
          <div class="relative">
            <span
              class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[0.7rem] text-text-mute"
              aria-hidden="true"
            >
              ⌕
            </span>
            <input
              ref={searchInputRef}
              type="text"
              value={searchQuery()}
              onInput={(e) => setSearchQuery(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  setSearchQuery("");
                  (e.currentTarget as HTMLInputElement).blur();
                }
              }}
              placeholder={t("sessions.search.placeholder")}
              aria-label={t("sessions.search.aria")}
              data-amor-session-search=""
              class="w-full rounded-md border border-border-subtle/60 bg-bg-elevated-v25/40 pl-6 pr-2 py-1 text-[0.75rem] text-text-body placeholder:text-text-mute focus:border-border-strong focus:bg-bg-elevated/80 focus:outline-none transition-colors"
            />
            <Show when={isSearching()}>
              <button
                type="button"
                aria-label={t("sessions.search.clear")}
                onClick={() => {
                  setSearchQuery("");
                  searchInputRef?.focus();
                }}
                class="absolute right-1 top-1/2 -translate-y-1/2 inline-flex h-5 w-5 items-center justify-center rounded text-[0.65rem] text-text-subtle hover:bg-bg-hover hover:text-text-display"
              >
                <span aria-hidden="true">×</span>
              </button>
            </Show>
          </div>
        </div>
      </Show>

      <Show
        when={!q.isLoading}
        fallback={
          <p class="px-2 text-xs text-text-subtle">
            {t("sessions.loading")}
          </p>
        }
      >
        <Show
          when={total() > 0}
          fallback={
            <div class="px-2 py-5 text-center">
              <div
                class="mx-auto mb-2 flex h-9 w-9 items-center justify-center rounded-full bg-bg-elevated-v25/60 text-[1rem] text-text-subtle"
                aria-hidden="true"
              >
                ✎
              </div>
              <p class="text-xs text-text-body">{t("sessions.empty")}</p>
              <p class="mt-0.5 text-[0.65rem] text-text-mute">
                {t("sessions.empty.cta")}
              </p>
            </div>
          }
        >
          <Show
            when={groups().length > 0}
            fallback={
              <p class="px-2 py-1 text-xs italic text-text-subtle">
                {t("sessions.search.no_results")}
              </p>
            }
          >
            <div class="max-h-[50vh] overflow-y-auto pr-0.5 space-y-2">
              <For each={groups()}>
                {(group) => (
                  <section
                    data-amor-session-group={group.key}
                    aria-label={group.label}
                  >
                    <h3 class="px-2 pb-0.5 text-[0.55rem] font-semibold tracking-wider text-text-subtle/80">
                      {localeUpper(group.label)}
                    </h3>
                    <ul class="space-y-0.5">
                      <For each={group.items}>
                        {(s) => (
                          <SessionRow
                            session={s}
                            density={sessionDensity()}
                            isCurrentMode={
                              currentMode() === (s.mode === "code" ? "build" : s.mode)
                            }
                            onRename={() => setPending({ kind: "rename", session: s })}
                            onRenameInline={(title) =>
                              renameMutation.mutate({ id: s.id, title })
                            }
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
  density: SessionDensity;
  isCurrentMode: boolean;
  onRename: () => void;
  // Cycle UI v2.8.4 — inline rename without opening the modal.  Wired
  // straight to renameMutation; falls back to onRename (modal) for
  // mobile / a11y users where double-click isn't ergonomic.
  onRenameInline: (title: string) => void;
  onArchive: () => void;
  onPin: () => void;
  onDelete: () => void;
}

const SessionRow: Component<SessionRowProps> = (props) => {
  const [menuOpen, setMenuOpen] = createSignal(false);
  // Cycle UI v2.8.4 — inline edit state (double-click on title → input).
  const [editing, setEditing] = createSignal<boolean>(false);
  const [draft, setDraft] = createSignal<string>("");
  let inputRef: HTMLInputElement | undefined;
  const isCompact = (): boolean => props.density === "compact";

  const startEdit = (e?: Event): void => {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    setDraft(title());
    setEditing(true);
    // Focus + select-all on next tick so the user can immediately type.
    queueMicrotask(() => {
      inputRef?.focus();
      inputRef?.select();
    });
  };
  const commitEdit = (): void => {
    const next = draft().trim();
    setEditing(false);
    if (!next) return;
    if (next === title()) return;
    props.onRenameInline(next);
  };
  const cancelEdit = (): void => {
    setEditing(false);
    setDraft("");
  };

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
  // Cycle UI v2.5 — `isActive` no longer drives an animated dot;
  // the status is still exposed via `data-amor-session-status` and
  // the sr-only label for screen readers + e2e probes.

  // Cycle UI v2.8.1 — active state checker: URL has ?c={session.id}.
  const isActiveSession = () => {
    if (typeof window === "undefined") return false;
    const params = new URLSearchParams(window.location.search);
    return params.get("c") === props.session.id;
  };

  return (
    <li class="group relative" data-amor-density={props.density}>
      <A
        // Cycle UI v2.8.1 — Bug fix: session click no longer routes to
        // /build, /research, etc (those mode pages don't accept ?c=
        // and effectively start a fresh chat).  Always route to / with
        // the session id as a query param; UnifiedChat watches the
        // param + hydrates via GET /api/sessions/{id}/branch.  Single
        // route = single state machine = no "click my chat, get a
        // blank composer" regression.
        //
        // Cycle UI v2.8.3 — density-aware padding.  Compact mode:
        // py-1 + single-line layout (no mode chip / timestamp row).
        // Comfortable: py-2.5 + 2-line layout (v2.8.x baseline).
        href={`/?c=${encodeURIComponent(props.session.id)}`}
        data-amor-session-row=""
        data-amor-session-status={status()}
        data-amor-session-mode={modeKey()}
        class={[
          "relative flex items-start gap-2.5 rounded-md px-2 pr-12",
          isCompact() ? "py-1.5" : "py-2.5",
          "border border-transparent",
          "text-text-body hover:bg-bg-hover hover:text-text-display",
          "transition-colors duration-150",
          "focus-visible:outline-2 focus-visible:outline-offset-1",
          isActiveSession()
            ? "bg-bg-hover text-text-display border-border-subtle"
            : "",
          props.session.archived ? "opacity-60" : "",
        ].join(" ")}
        title={`${title()} · ${statusLabel()} · ${relativeTime(
          props.session.updated_at ?? props.session.created_at,
        )}`}
        aria-current={isActiveSession() ? "page" : undefined}
        onKeyDown={(e) => {
          // Cycle UI v2.8.4 — keyboard arrow navigation across rows.
          // Arrow Up/Down moves focus to the previous/next row in
          // the sidebar.  Enter follows the link (browser default).
          // Cmd/Ctrl+P toggles pin; Cmd/Ctrl+Shift+R triggers rename
          // (inline edit on the same row, no modal hop).
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            const rows = Array.from(
              document.querySelectorAll<HTMLAnchorElement>(
                "a[data-amor-session-row]",
              ),
            );
            const idx = rows.indexOf(e.currentTarget as HTMLAnchorElement);
            if (idx < 0) return;
            const nextIdx =
              e.key === "ArrowDown"
                ? Math.min(rows.length - 1, idx + 1)
                : Math.max(0, idx - 1);
            rows[nextIdx]?.focus();
          } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "p") {
            e.preventDefault();
            props.onPin();
          } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "r") {
            e.preventDefault();
            startEdit();
          }
        }}
      >
        {/* Cycle UI v2.8.1 — mode-tinted left rail visible on the
            ACTIVE session (URL ?c= match), not the active mode. */}
        <Show when={isActiveSession()}>
          <span
            class={[
              "absolute left-0 w-[2px] rounded-full",
              isCompact() ? "top-1.5 bottom-1.5" : "top-2 bottom-2",
            ].join(" ")}
            style={{ background: modeColorVar(modeKey()) }}
            aria-hidden="true"
            data-amor-session-mode-rule=""
          />
        </Show>

        <span class="sr-only" data-amor-session-status={status()}>
          {statusLabel()}
        </span>

        <span class="flex min-w-0 flex-1 flex-col">
          <span class="flex items-center gap-1.5">
            {/* Cycle UI v2.8.3 — pinned glyph still shown inline on
                pinned rows.  The clickable hover-pin button lives in
                the trailing action rail (right of the title cell) so
                the title column stays clean. */}
            <Show when={props.session.pinned}>
              <span
                class="text-[0.7rem] leading-none"
                style={{ color: "var(--color-status-warming)" }}
                aria-hidden="true"
                title={t("sessions.status.pinned")}
              >
                ★
              </span>
            </Show>
            {/* Cycle UI v2.8.3 — compact mode inlines the mode chip
                into the title row (saves a line); comfortable mode
                keeps it on the second row. */}
            <Show when={isCompact() && modeKey()}>
              <span
                class="inline-flex items-center rounded px-1 py-px text-[0.55rem] font-medium tracking-wide"
                style={{
                  background:
                    "color-mix(in oklch, " +
                    modeColorVar(modeKey()) +
                    " 14%, transparent)",
                  color: modeColorVar(modeKey()),
                }}
                aria-hidden="true"
              >
                {localeUpper(modeShortLabel(modeKey()))}
              </span>
            </Show>
            {/* Cycle UI v2.8.4 — double-click on title → inline edit
                (no modal interruption).  Enter commits, Esc cancels,
                blur commits.  Clicking inside the input doesn't
                navigate (preventDefault + stopPropagation on the input
                itself). */}
            <Show
              when={!editing()}
              fallback={
                <input
                  ref={inputRef}
                  type="text"
                  value={draft()}
                  onInput={(e) =>
                    setDraft((e.currentTarget as HTMLInputElement).value)
                  }
                  onBlur={commitEdit}
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                  }}
                  onKeyDown={(e) => {
                    e.stopPropagation();
                    if (e.key === "Enter") {
                      e.preventDefault();
                      commitEdit();
                    } else if (e.key === "Escape") {
                      e.preventDefault();
                      cancelEdit();
                    }
                  }}
                  aria-label={t("sessions.rename.placeholder")}
                  data-amor-session-rename-input=""
                  class="min-w-0 flex-1 rounded border border-border-strong bg-bg-canvas px-1 py-px text-[13px] font-medium leading-snug text-text-display focus:outline-none focus:ring-1 focus:ring-[var(--color-focus-ring)]"
                />
              }
            >
              <span
                class="truncate text-[13px] font-medium leading-snug"
                ondblclick={(e) => startEdit(e)}
                title={t("sessions.rename.hint")}
              >
                {title()}
              </span>
            </Show>
          </span>
          {/* Cycle UI v2.8.3 — second row only shown in comfortable
              density.  Compact omits the mode chip + timestamp to
              save vertical space. */}
          <Show when={!isCompact()}>
            <span class="mt-0.5 flex items-center gap-1.5 text-[0.65rem] text-text-subtle">
              <Show when={modeKey()}>
                <span
                  class="inline-flex items-center gap-1 rounded px-1 py-px text-[0.55rem] font-medium tracking-wide"
                  style={{
                    background:
                      "color-mix(in oklch, " +
                      modeColorVar(modeKey()) +
                      " 14%, transparent)",
                    color: modeColorVar(modeKey()),
                  }}
                >
                  {localeUpper(modeShortLabel(modeKey()))}
                </span>
              </Show>
              <span
                class="truncate italic tabular-nums"
                data-amor-session-timestamp=""
              >
                {relativeTime(
                  props.session.updated_at ?? props.session.created_at,
                )}
              </span>
            </span>
          </Show>
        </span>
      </A>

      {/* Cycle UI v2.8.3 — trailing action rail.  Two buttons:
          (a) one-click pin/unpin (hover-revealed when unpinned, always
              visible + gold when pinned),
          (b) ⋯ menu (rename / archive / delete).
          Compact rows shrink the rail vertically; comfortable rows
          give it more breathing room. */}
      <div
        class={[
          "absolute right-1 flex items-center gap-0.5",
          isCompact() ? "top-1" : "top-1.5",
        ].join(" ")}
      >
        <button
          type="button"
          aria-label={
            props.session.pinned
              ? t("sessions.action.unpin")
              : t("sessions.action.pin")
          }
          title={
            props.session.pinned
              ? t("sessions.action.unpin")
              : t("sessions.action.pin")
          }
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            props.onPin();
          }}
          data-amor-quick-pin={props.session.pinned ? "on" : "off"}
          class={[
            "inline-flex h-6 w-6 items-center justify-center rounded text-[0.75rem]",
            "focus-visible:outline-2 focus-visible:outline-offset-1",
            props.session.pinned
              ? "text-[var(--color-status-warming)] hover:bg-bg-hover"
              : [
                  "text-text-mute hover:bg-bg-hover hover:text-text-body",
                  // Hover-revealed when not pinned — keeps the rail
                  // clean for "you've never pinned this" rows.
                  "opacity-0 group-hover:opacity-100 focus:opacity-100",
                ].join(" "),
          ].join(" ")}
        >
          <span aria-hidden="true">{props.session.pinned ? "★" : "☆"}</span>
        </button>
        <button
          type="button"
          class={[
            "inline-flex h-6 w-6 items-center justify-center rounded text-text-subtle",
            "hover:bg-bg-elevated-v25 hover:text-text-display",
            "focus-visible:outline-2 focus-visible:outline-offset-1",
            menuOpen()
              ? "bg-bg-elevated-v25 text-text-display opacity-100"
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
      </div>
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
        class="block w-full px-3 py-1.5 text-left text-xs text-text-display hover:bg-bg-hover"
        onClick={props.onRename}
      >
        {t("sessions.action.rename")}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-display hover:bg-bg-hover"
        onClick={props.onPin}
      >
        {props.pinned
          ? t("sessions.action.unpin")
          : t("sessions.action.pin")}
      </button>
      <button
        type="button"
        role="menuitem"
        class="block w-full px-3 py-1.5 text-left text-xs text-text-display hover:bg-bg-hover"
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
