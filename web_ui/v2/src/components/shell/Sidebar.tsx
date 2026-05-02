import { type Component, For, Show } from "solid-js";
import { A, useLocation } from "@solidjs/router";
import { MODES, type ModeMeta } from "../../lib/types";
import { auth } from "../../lib/auth";
import { Avatar, Tooltip, IconButton } from "../ui";

const SYSTEM_LINKS: ReadonlyArray<{
  href: string;
  label: string;
  glyph: string;
}> = [
  { href: "/settings", label: "Settings", glyph: "⚙" },
  { href: "/legacy", label: "Open legacy UI", glyph: "↗" },
];

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
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

export const Sidebar: Component<SidebarProps> = (props) => {
  const location = useLocation();
  const isActive = (href: string) =>
    location.pathname === href ||
    (href !== "/" && location.pathname.startsWith(href));

  return (
    <aside
      class={[
        "flex h-full flex-col border-r border-border-subtle bg-bg-secondary",
        "transition-[width] duration-200",
        props.collapsed ? "w-14" : "w-60",
      ].join(" ")}
      aria-label="Primary"
    >
      {/* Brand + collapse */}
      <div class="flex h-12 items-center justify-between border-b border-border-subtle px-3">
        <Show when={!props.collapsed}>
          <A href="/" class="font-semibold tracking-tight">
            AMOR
          </A>
        </Show>
        <IconButton
          aria-label={props.collapsed ? "Expand sidebar" : "Collapse sidebar"}
          size="sm"
          onClick={props.onToggle}
        >
          <span aria-hidden="true">{props.collapsed ? "›" : "‹"}</span>
        </IconButton>
      </div>

      {/* Modes */}
      <nav class="flex-1 overflow-y-auto px-2 py-3">
        <Show when={!props.collapsed}>
          <p class="mb-1.5 px-2 text-[0.65rem] font-semibold uppercase tracking-widest text-text-tertiary">
            Modes
          </p>
        </Show>
        <ul class="space-y-0.5">
          <For each={MODES}>
            {(mode) => (
              <SidebarItem mode={mode} active={isActive(mode.href)} collapsed={props.collapsed} />
            )}
          </For>
        </ul>

        <Show when={!props.collapsed}>
          <p class="mt-6 mb-1.5 px-2 text-[0.65rem] font-semibold uppercase tracking-widest text-text-tertiary">
            System
          </p>
        </Show>
        <ul class="space-y-0.5">
          <For each={SYSTEM_LINKS}>
            {(link) => (
              <li>
                <A
                  href={link.href}
                  class={[
                    "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm",
                    "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
                    isActive(link.href) ? "bg-bg-hover text-text-primary" : "",
                  ].join(" ")}
                  end={link.href === "/"}
                >
                  <span class="w-4 text-center text-text-tertiary" aria-hidden="true">
                    {link.glyph}
                  </span>
                  <Show when={!props.collapsed}>
                    <span class="truncate">{link.label}</span>
                  </Show>
                </A>
              </li>
            )}
          </For>
        </ul>
      </nav>

      {/* User card */}
      <div class="border-t border-border-subtle px-3 py-3">
        <Show
          when={auth.user()}
          fallback={
            <Show when={!props.collapsed}>
              <p class="text-xs text-text-tertiary">Signed out</p>
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
                  <p class="truncate text-[0.65rem] text-text-tertiary">
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
        "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
        props.active ? "bg-bg-hover text-text-primary" : "",
      ].join(" ")}
      data-mode={props.mode.key}
    >
      <span
        class={[
          "flex h-4 w-4 items-center justify-center text-[0.85rem]",
          props.active ? "text-text-primary" : "text-text-tertiary",
        ].join(" ")}
        aria-hidden="true"
        style={
          props.active ? { color: "var(--mode-accent)" } : undefined
        }
      >
        {GLYPH[props.mode.glyph] ?? "•"}
      </span>
      <Show when={!props.collapsed}>
        <span class="flex-1 truncate">{props.mode.label}</span>
        <Show when={!props.mode.wired}>
          <span class="rounded-full bg-bg-tertiary px-1.5 py-0.5 text-[0.55rem] uppercase tracking-wide text-text-tertiary">
            soon
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
          <Tooltip label={props.mode.label} placement="right">
            {link}
          </Tooltip>
        }
      >
        {link}
      </Show>
    </li>
  );
};
