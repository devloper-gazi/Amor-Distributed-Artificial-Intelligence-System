/**
 * Cycle C Sprint 11 Day 2 ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â mobile-first shell.
 *
 * Active below ``MOBILE_BREAKPOINT_PX`` (768 px).  Replaces the
 * always-visible desktop Sidebar with:
 *
 * * a top app-bar carrying the brand + a hamburger that toggles a
 *   drawer-mounted Sidebar (slides in from the left over a
 *   semi-transparent backdrop)
 * * a content scroll region with safe-area-bottom padding so the
 *   composer / page footer doesn't sit under the iOS home indicator
 *
 * The desktop Sidebar component is reused inside the drawer so we
 * have a single source of truth for navigation; only the framing
 * changes per breakpoint.  When the user navigates (e.g. clicks a
 * mode link) the drawer auto-closes ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â that's tracked by listening
 * to ``location.pathname`` changes.
 *
 * Accessibility
 * -------------
 * * Drawer overlay is ``role="dialog"`` + ``aria-modal="true"``.
 * * Backdrop click + Escape close the drawer.
 * * The hamburger button carries the right aria-expanded state.
 */

import {
  type Component,
  type JSX,
  Show,
  createEffect,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";
import { useLocation } from "@solidjs/router";

import { Sidebar } from "./Sidebar";
import { IconButton } from "../ui";
import { t } from "../../i18n";


interface MobileShellProps {
  children: JSX.Element;
  onOpenPalette: () => void;
}


export const MobileShell: Component<MobileShellProps> = (props) => {
  const [drawerOpen, setDrawerOpen] = createSignal(false);
  const location = useLocation();

  // Close the drawer on route change so navigating from a sidebar
  // link doesn't leave the overlay covering the destination page.
  createEffect(() => {
    void location.pathname;
    setDrawerOpen(false);
  });

  // Lock body scroll while the drawer is open ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â iOS Safari leaks
  // backgrounding gestures otherwise.
  createEffect(() => {
    if (typeof document === "undefined") return;
    document.body.style.overflow = drawerOpen() ? "hidden" : "";
  });

  // Escape closes the drawer.
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && drawerOpen()) {
      e.preventDefault();
      setDrawerOpen(false);
    }
  };
  onMount(() => window.addEventListener("keydown", onKey));
  onCleanup(() => {
    window.removeEventListener("keydown", onKey);
    if (typeof document !== "undefined") {
      document.body.style.overflow = "";
    }
  });

  return (
    <div
      class="flex h-full flex-col bg-bg-canvas text-text-display amor-safe-x"
      data-amor-shell="mobile"
    >
      {/* Top bar ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â brand + hamburger + palette trigger */}
      <header
        class="flex h-12 shrink-0 items-center gap-2 border-b border-border-subtle bg-bg-elevated-v25 px-3 amor-safe-top"
        role="banner"
      >
        <IconButton
          aria-label={
            drawerOpen() ? t("sidebar.collapse") : t("sidebar.expand")
          }
          aria-expanded={drawerOpen()}
          aria-controls="amor-mobile-drawer"
          size="md"
          onClick={() => setDrawerOpen((o) => !o)}
          class="amor-touch"
          data-amor-action="drawer-toggle"
        >
          <span aria-hidden="true">{drawerOpen() ? "ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¢" : "ÃƒÆ’Ã‚Â¢Ãƒâ€¹Ã…â€œÃƒâ€šÃ‚Â°"}</span>
        </IconButton>
        <a href="/" class="font-semibold tracking-tight">
          {t("sidebar.brand")}
        </a>
        <button
          type="button"
          onClick={props.onOpenPalette}
          class="ml-auto flex h-9 items-center gap-2 rounded-md border border-border-subtle bg-bg-elevated px-2 text-xs text-text-subtle hover:bg-bg-hover amor-touch"
          aria-label={t("sidebar.palette_open")}
        >
          <span aria-hidden="true">ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬â„¢Ãƒâ€¹Ã…â€œ</span>
          <span>{t("sidebar.search")}</span>
        </button>
      </header>

      {/* Main content + safe-area-bottom padding so a fixed
          composer / footer doesn't sit under the home indicator. */}
      <main class="flex min-w-0 flex-1 flex-col overflow-hidden">
        {props.children}
      </main>

      {/* Drawer + backdrop ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â Tailwind-only, no transition lib */}
      <Show when={drawerOpen()}>
        {/* Backdrop */}
        <button
          type="button"
          aria-label={t("sidebar.collapse")}
          class="fixed inset-0 z-[var(--z-modal)] bg-black/40"
          onClick={() => setDrawerOpen(false)}
          data-amor-mobile-backdrop=""
        />
        {/* Drawer dialog */}
        <aside
          id="amor-mobile-drawer"
          role="dialog"
          aria-modal="true"
          aria-label={t("palette.dialog_label")}
          class="fixed inset-y-0 left-0 z-[var(--z-modal)] flex w-72 max-w-[80vw] flex-col bg-bg-elevated-v25 shadow-xl amor-safe-y"
          data-amor-mobile-drawer=""
        >
          <Sidebar
            collapsed={false}
            onToggle={() => setDrawerOpen(false)}
            onOpenPalette={props.onOpenPalette}
          />
        </aside>
      </Show>
    </div>
  );
};
