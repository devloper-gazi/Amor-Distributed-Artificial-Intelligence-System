import { type Component, For, Show, createSignal, onMount, onCleanup } from "solid-js";
import { A, useLocation, useNavigate } from "@solidjs/router";
import { MODES, type ModeMeta } from "../../lib/types";
import { auth } from "../../lib/auth";
import { Avatar, Tooltip, IconButton, Kbd } from "../ui";
import { SessionList } from "./SessionList";
import { modeLabel, t, localeUpper } from "../../i18n";

interface SystemLink {
  href: string;
  /** i18n key — looked up via ``t()`` at render time. */
  label_key: string;
  glyph: string;
  external?: boolean;
}

const SYSTEM_LINKS: ReadonlyArray<SystemLink> = [
  { href: "/settings", label_key: "sidebar.system.settings", glyph: "⚙" },
  // Cycle C Sprint 0 Day 3 — baseline corpus dashboard.  Lives under
  // System on purpose: it's an operator/owner view, not a per-mode
  // workspace.  When Sprint 6's training tab lands it'll join here.
  { href: "/admin/baselines", label_key: "sidebar.system.baselines", glyph: "▦" },
  // Cycle C Sprint 1 Day 4 — LLM backend dashboard (resident models,
  // swap events, p50/p95 latency).
  { href: "/admin/llm", label_key: "sidebar.system.llm", glyph: "◆" },
  // Cycle C Sprint 2 Day 5 — eval harness dashboard.
  { href: "/admin/evals", label_key: "sidebar.system.evals", glyph: "▸" },
];

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  onOpenPalette: () => void;
}

/** Lucide-style glyph fallback — we render an emoji-class character
 *  per mode until we drop a real Lucide-Solid binding in.  Same
 *  visual weight, zero deps. */
const GLYPH: Record<string, string> = {
  compass: "◎",
  hammer: "▲",
  brain: "◊",
  "users-round": "❖",
  "shield-half": "◐",
  activity: "≈",
};

/** Cycle UI v2.6 (Karar D) — legacy_dense_sidebar localStorage flag.
 *  When true (operator opt-in), MODLAR + SİSTEM sections start
 *  expanded (v2.5 behaviour).  When false (default), they're
 *  collapsed accordion sections — "alan" feel.
 *
 *  Settings UI toggle in D8 emits "amor:sidebar-legacy-toggle"
 *  CustomEvent; this signal listens + persists. */
function loadLegacyDense(): boolean {
  if (typeof localStorage === "undefined") return false;
  try {
    return localStorage.getItem("amor.sidebar.legacy_dense") === "1";
  } catch {
    return false;
  }
}

export const Sidebar: Component<SidebarProps> = (props) => {
  const location = useLocation();
  const navigate = useNavigate();
  const isActive = (href: string) =>
    location.pathname === href ||
    (href !== "/" && location.pathname.startsWith(href));

  /** Cycle UI v2.8.2 — "Yeni sohbet" handler.  Previously dispatched
   *  a Cmd+N keyboard event that only UnifiedChat's onMount listener
   *  consumed; on /settings (or any other route where UnifiedChat
   *  isn't mounted) the click silently no-op'd.  Now:
   *   1. SolidJS router navigate("/") — works from ANY route.
   *   2. Drop the ?c= deep-link so the destination is the fresh
   *      empty-state thread (not a hydrated past session).
   *   3. Dispatch the existing CustomEvent so UnifiedChat (when it
   *      mounts post-nav) tears down any cached stream + clears the
   *      composer textarea.  Two-stage = works regardless of source
   *      route.
   */
  const startNewChat = () => {
    // Strip ?c= from URL — fresh chat means empty state.
    navigate("/", { replace: false });
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("amor:new-chat"));
    }
  };

  const [legacyDense, setLegacyDense] = createSignal<boolean>(loadLegacyDense());
  onMount(() => {
    if (typeof window === "undefined") return;
    const onToggle = (ev: Event) => {
      const next = !legacyDense();
      setLegacyDense(next);
      try {
        localStorage.setItem("amor.sidebar.legacy_dense", next ? "1" : "0");
      } catch {
        // ignore SSR / storage-disabled
      }
      // Tell other parts of the surface (Settings page label etc.).
      window.dispatchEvent(
        new CustomEvent("amor:sidebar-legacy-changed", { detail: { value: next } }),
      );
      void ev;
    };
    window.addEventListener("amor:sidebar-legacy-toggle", onToggle);
    onCleanup(() =>
      window.removeEventListener("amor:sidebar-legacy-toggle", onToggle),
    );
  });

  return (
    <aside
      class={[
        // Cycle UI v2.6.1 — translucent sidebar so the halo
        // backdrop bleeds through and the surface feels lighter.
        // 70% opacity + backdrop-blur lifts the chat canvas
        // forward.  Border softens to subtle/30.
        "flex h-full flex-col border-r border-border-subtle/60 bg-bg-elevated-v25/70 backdrop-blur-md",
        "transition-[width] duration-200",
        props.collapsed ? "w-14" : "w-60",
      ].join(" ")}
      aria-label="Primary"
    >
      {/* Brand + collapse */}
      <div class="flex h-12 items-center justify-between border-b border-border-subtle px-3">
        <Show when={!props.collapsed}>
          <A href="/" class="font-semibold tracking-tight">
            {t("sidebar.brand")}
          </A>
        </Show>
        <IconButton
          aria-label={props.collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
          size="sm"
          onClick={props.onToggle}
        >
          <span aria-hidden="true">{props.collapsed ? "›" : "‹"}</span>
        </IconButton>
      </div>

      {/* Cycle UI v2.8 — Gemini/Claude pattern: top-level action
          buttons replace the old MODLAR/SİSTEM accordions.  Modes
          remain reachable via the composer pill + slash overlay; the
          sidebar focuses on session-life actions (new chat, search,
          library, settings).  legacyDense() flag still toggles the
          v2.5 dense view for power users. */}

      {/* Action: New chat */}
      <Show when={!props.collapsed}>
        <button
          type="button"
          onClick={startNewChat}
          class="mx-3 mt-3 flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-text-body hover:bg-bg-hover hover:text-text-display"
          aria-label={t("sidebar.new_chat")}
          data-amor-action="new-chat"
        >
          <span class="w-4 text-center text-text-subtle" aria-hidden="true">+</span>
          <span class="truncate font-medium">{t("sidebar.new_chat")}</span>
          <Kbd class="ml-auto">Mod+N</Kbd>
        </button>
      </Show>
      <Show when={props.collapsed}>
        <div class="mx-2 mt-3 flex justify-center">
          <Tooltip label={`${t("sidebar.new_chat")} (⌘N)`} placement="right">
            <IconButton aria-label={t("sidebar.new_chat")} size="sm" onClick={startNewChat}>
              <span aria-hidden="true">+</span>
            </IconButton>
          </Tooltip>
        </div>
      </Show>

      {/* Action: Search (command palette) */}
      <Show when={!props.collapsed}>
        <button
          type="button"
          onClick={props.onOpenPalette}
          class="mx-3 mt-1 flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-text-body hover:bg-bg-hover hover:text-text-display"
          aria-label={t("sidebar.palette_open")}
        >
          <span class="w-4 text-center text-text-subtle" aria-hidden="true">⌕</span>
          <span class="truncate font-medium">{t("sidebar.search")}</span>
          <Kbd class="ml-auto">Mod+K</Kbd>
        </button>
      </Show>
      <Show when={props.collapsed}>
        <div class="mx-2 mt-1 flex justify-center">
          <Tooltip label={`${t("palette.dialog_label")} (⌘K)`} placement="right">
            <IconButton aria-label={t("sidebar.palette_open")} size="sm" onClick={props.onOpenPalette}>
              <span aria-hidden="true">⌘</span>
            </IconButton>
          </Tooltip>
        </div>
      </Show>

      {/* Action: Settings (Gemini-pattern top-level shortcut) */}
      <Show when={!props.collapsed}>
        <A
          href="/settings"
          class="mx-3 mt-1 flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-text-body hover:bg-bg-hover hover:text-text-display"
          aria-label={t("sidebar.system.settings")}
        >
          <span class="w-4 text-center text-text-subtle" aria-hidden="true">⚙</span>
          <span class="truncate font-medium">{t("sidebar.system.settings")}</span>
        </A>
      </Show>
      <Show when={props.collapsed}>
        <div class="mx-2 mt-1 flex justify-center">
          <Tooltip label={t("sidebar.system.settings")} placement="right">
            <A href="/settings" class="inline-flex h-7 w-7 items-center justify-center rounded-md text-text-subtle hover:bg-bg-hover hover:text-text-display">
              <span aria-hidden="true">⚙</span>
            </A>
          </Tooltip>
        </div>
      </Show>

      {/* Modes + System — collapsed-by-default in v2.8.  Power users
          who want the v2.5 dense view enable `legacy_dense_sidebar`
          flag (Settings). */}
      <nav class="flex-1 overflow-y-auto px-2 py-3">
        <Show when={!props.collapsed && legacyDense()} fallback={
          <Show when={props.collapsed}>
            <ul class="space-y-0.5">
              <For each={MODES}>
                {(mode) => (
                  <SidebarItem mode={mode} active={isActive(mode.href)} collapsed={true} />
                )}
              </For>
            </ul>
          </Show>
        }>
          <details
            open={legacyDense()}
            class="amor-sidebar-section"
            data-amor-section="modes"
          >
            <summary class="mb-1.5 cursor-pointer list-none px-2 py-1 text-[0.65rem] font-semibold tracking-widest text-text-subtle hover:text-text-body select-none">
              {localeUpper(t("sidebar.section.modes"))}
              <span class="ml-1 inline-block text-text-mute transition-transform" aria-hidden="true">▾</span>
            </summary>
            <ul class="space-y-0.5">
              <For each={MODES}>
                {(mode) => (
                  <SidebarItem mode={mode} active={isActive(mode.href)} collapsed={props.collapsed} />
                )}
              </For>
            </ul>
          </details>

          <details
            open={legacyDense()}
            class="amor-sidebar-section mt-4"
            data-amor-section="system"
          >
            <summary class="mb-1.5 cursor-pointer list-none px-2 py-1 text-[0.65rem] font-semibold tracking-widest text-text-subtle hover:text-text-body select-none">
              {localeUpper(t("sidebar.section.system"))}
              <span class="ml-1 inline-block text-text-mute transition-transform" aria-hidden="true">▾</span>
            </summary>
            <ul class="space-y-0.5">
              <For each={SYSTEM_LINKS}>
                {(link) => (
                  <li>
                    <Show
                      when={link.external}
                      fallback={
                        <A
                          href={link.href}
                          class={[
                            "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
                            "text-text-body hover:bg-bg-hover hover:text-text-display",
                            isActive(link.href)
                              ? "bg-bg-hover text-text-display"
                              : "",
                          ].join(" ")}
                          end={link.href === "/"}
                        >
                          <span
                            class="w-4 text-center text-text-subtle"
                            aria-hidden="true"
                          >
                            {link.glyph}
                          </span>
                          <span class="truncate">{t(link.label_key)}</span>
                        </A>
                      }
                    >
                      <a
                        href={link.href}
                        class={[
                          "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
                          "text-text-body hover:bg-bg-hover hover:text-text-display",
                        ].join(" ")}
                      >
                        <span
                          class="w-4 text-center text-text-subtle"
                          aria-hidden="true"
                        >
                          {link.glyph}
                        </span>
                        <span class="truncate">{t(link.label_key)}</span>
                      </a>
                    </Show>
                  </li>
                )}
              </For>
            </ul>
          </details>
        </Show>

        {/* Sessions */}
        <SessionList collapsed={props.collapsed} />
      </nav>

      {/* User card */}
      <div class="border-t border-border-subtle px-3 py-3">
        <Show
          when={auth.user()}
          fallback={
            <Show when={!props.collapsed}>
              <p class="text-xs text-text-subtle">{t("sidebar.signed_out")}</p>
            </Show>
          }
        >
          {(u) => (
            <div class="flex items-center gap-2">
              <Avatar
                variant="user"
                initials={(u().display_name ?? u().username).slice(0, 2)}
                size={24}
              />
              <Show when={!props.collapsed}>
                <div class="min-w-0 flex-1">
                  <p class="truncate text-xs font-medium">
                    {u().display_name ?? u().username}
                  </p>
                  <p class="truncate text-[0.65rem] text-text-subtle">
                    {u().email}
                  </p>
                </div>
              </Show>
            </div>
          )}
        </Show>
      </div>
    </aside>
  );
};

const SidebarItem: Component<{
  mode: ModeMeta;
  active: boolean;
  collapsed: boolean;
}> = (props) => {
  const link = (
    <A
      href={props.mode.href}
      class={[
        "group flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
        "text-text-body hover:bg-bg-hover hover:text-text-display",
        props.active ? "bg-bg-hover text-text-display" : "",
      ].join(" ")}
      data-mode={props.mode.key}
    >
      <span
        class={[
          "flex h-4 w-4 items-center justify-center text-[0.85rem]",
          props.active ? "text-text-display" : "text-text-subtle",
        ].join(" ")}
        aria-hidden="true"
        style={
          props.active ? { color: "var(--mode-accent)" } : undefined
        }
      >
        {GLYPH[props.mode.glyph] ?? "•"}
      </span>
      <Show when={!props.collapsed}>
        <span class="flex-1 truncate">{modeLabel(props.mode)}</span>
        <Show when={!props.mode.wired}>
          <span class="rounded-full bg-bg-elevated-v25 px-1.5 py-0.5 text-[0.55rem] tracking-wide text-text-subtle">
            {localeUpper(t("sidebar.mode.soon"))}
          </span>
        </Show>
      </Show>
    </A>
  );

  return (
    <li>
      <Show
        when={!props.collapsed}
        fallback={
          <Tooltip label={modeLabel(props.mode)} placement="right">
            {link}
          </Tooltip>
        }
      >
        {link}
      </Show>
    </li>
  );
};
