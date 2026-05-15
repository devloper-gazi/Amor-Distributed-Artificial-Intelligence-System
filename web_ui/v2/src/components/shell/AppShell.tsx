import {
  type Component,
  type JSX,
  Show,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import { Sidebar } from "./Sidebar";
import { CommandPalette } from "./CommandPalette";
import { MobileShell } from "./MobileShell";
import { useViewport } from "../../lib/viewport";

interface AppShellProps {
  children: JSX.Element;
}

/**
 * Top-level layout.  Sidebar + main content + a globally-mounted
 * command palette opened by ⌘+K / Ctrl+K.  The collapse state
 * persists in localStorage so the user's chrome density carries
 * across reloads.
 *
 * Cycle C Sprint 11 Day 2 — below the 768 px breakpoint we delegate
 * to ``MobileShell`` which trades the always-visible Sidebar for a
 * drawer + top app-bar.  The same ``CommandPalette`` mounts in both
 * modes; the children + palette open-state are shared.
 */
export const AppShell: Component<AppShellProps> = (props) => {
  const [collapsed, setCollapsed] = createSignal(false);
  const [paletteOpen, setPaletteOpen] = createSignal(false);
  const viewport = useViewport();

  onMount(() => {
    try {
      const saved = localStorage.getItem("amor.sidebar.collapsed");
      if (saved === "1") setCollapsed(true);
    } catch {
      // ignore — localStorage may be unavailable in private modes
    }
  });

  const toggleSidebar = (): void => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("amor.sidebar.collapsed", next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  };

  /** Cmd-K (mac) / Ctrl-K (others) opens the command palette.
   *  Allow opening from inside text inputs too — power users want
   *  to escape from a stuck composer via the palette.  Only ignore
   *  when an isContentEditable target is captured. */
  const onKeyDown = (e: KeyboardEvent): void => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      setPaletteOpen((o) => !o);
    }
  };

  onMount(() => {
    window.addEventListener("keydown", onKeyDown);
  });
  onCleanup(() => {
    window.removeEventListener("keydown", onKeyDown);
  });

  return (
    <Show
      when={viewport().isMobile}
      fallback={
        <div class="flex h-full bg-bg-primary text-text-primary">
          <Sidebar
            collapsed={collapsed()}
            onToggle={toggleSidebar}
            onOpenPalette={() => setPaletteOpen(true)}
          />
          <main class="flex min-w-0 flex-1 flex-col">{props.children}</main>
          <CommandPalette
            open={paletteOpen()}
            onClose={() => setPaletteOpen(false)}
          />
        </div>
      }
    >
      <>
        <MobileShell onOpenPalette={() => setPaletteOpen(true)}>
          {props.children}
        </MobileShell>
        <CommandPalette
          open={paletteOpen()}
          onClose={() => setPaletteOpen(false)}
        />
      </>
    </Show>
  );
};
