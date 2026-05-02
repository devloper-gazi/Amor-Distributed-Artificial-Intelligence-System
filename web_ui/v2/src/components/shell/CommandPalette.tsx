import {
  type Component,
  createSignal,
  createMemo,
  For,
  Show,
  onMount,
  onCleanup,
  createUniqueId,
} from "solid-js";
import { useNavigate } from "@solidjs/router";
import { MODES } from "../../lib/types";
import { auth } from "../../lib/auth";
import { Kbd } from "../ui";

interface PaletteItem {
  id: string;
  label: string;
  hint?: string;
  category: string;
  glyph?: string;
  /** Action to run when the item is selected. */
  run: () => void | Promise<void>;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Hand-rolled Cmd-K command palette — no external cmdk dep.
 *
 * Categories: Mode (mode switcher), System (diagnostics, settings,
 * legacy UI, showcase), Theme (light/dark/system), Account (logout).
 * Filtering is a case-insensitive substring match across label +
 * category.  Keyboard: Esc closes; ArrowUp/Down move selection;
 * Enter fires.  Mouse hover updates selection; click also fires.
 *
 * Mounted globally; ``open`` is driven by a Cmd-K / Ctrl-K listener
 * in AppShell.  Renders nothing when closed so SSR-equivalent paint
 * cost stays at zero.
 */
export const CommandPalette: Component<CommandPaletteProps> = (props) => {
  const nav = useNavigate();
  const [query, setQuery] = createSignal("");
  const [selected, setSelected] = createSignal(0);
  const listboxId = createUniqueId();
  let inputRef: HTMLInputElement | undefined;

  const setTheme = (t: "light" | "dark" | "system"): void => {
    document.documentElement.setAttribute("data-theme", t);
    try {
      localStorage.setItem("amor.theme", t);
    } catch {
      // ignore
    }
  };

  const items = createMemo<PaletteItem[]>(() => {
    const out: PaletteItem[] = [];
    for (const mode of MODES) {
      out.push({
        id: `mode-${mode.key}`,
        label: mode.label,
        hint: mode.subtitle,
        category: "Mode",
        run: () => nav(mode.href),
      });
    }
    out.push(
      {
        id: "sys-diagnostics",
        label: "Diagnostics",
        hint: "health, sandbox, ledger",
        category: "System",
        run: () => nav("/system"),
      },
      {
        id: "sys-settings",
        label: "Settings",
        hint: "theme, account",
        category: "System",
        run: () => nav("/settings"),
      },
      {
        id: "sys-showcase",
        label: "Component showcase",
        hint: "atoms + theme tokens",
        category: "System",
        run: () => nav("/showcase"),
      },
      {
        id: "sys-legacy",
        label: "Open legacy UI",
        hint: "previous v1 monochrome chat",
        category: "System",
        run: () => {
          window.location.href = "/legacy";
        },
      },
      {
        id: "theme-light",
        label: "Theme: Light",
        category: "Theme",
        run: () => setTheme("light"),
      },
      {
        id: "theme-dark",
        label: "Theme: Dark",
        category: "Theme",
        run: () => setTheme("dark"),
      },
      {
        id: "theme-system",
        label: "Theme: System",
        category: "Theme",
        run: () => setTheme("system"),
      },
    );
    if (auth.user()) {
      out.push({
        id: "account-logout",
        label: "Sign out",
        hint: auth.user()?.username ?? "",
        category: "Account",
        run: async () => {
          await auth.logout();
          nav("/login", { replace: true });
        },
      });
    }
    return out;
  });

  const filtered = createMemo<PaletteItem[]>(() => {
    const q = query().trim().toLowerCase();
    if (!q) return items();
    return items().filter((it) =>
      `${it.label} ${it.hint ?? ""} ${it.category}`
        .toLowerCase()
        .includes(q),
    );
  });

  /** Reset state when the palette opens; focus the input. */
  const onOpenChange = (open: boolean): void => {
    if (open) {
      setQuery("");
      setSelected(0);
      queueMicrotask(() => inputRef?.focus());
    }
  };

  const fire = (item: PaletteItem): void => {
    props.onClose();
    void item.run();
  };

  const onKeyDown = (e: KeyboardEvent): void => {
    if (e.key === "Escape") {
      e.preventDefault();
      props.onClose();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((i) => Math.min(filtered().length - 1, i + 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const item = filtered()[selected()];
      if (item) fire(item);
    }
  };

  onMount(() => {
    onOpenChange(props.open);
  });

  // Re-focus input every time we transition closed → open.  Solid's
  // signals make this a one-line effect via a memo dependency.
  let lastOpen = false;
  createMemo(() => {
    if (props.open && !lastOpen) onOpenChange(true);
    lastOpen = props.open;
  });

  // Close on Escape from anywhere inside the palette.
  onCleanup(() => {
    /* no-op cleanup; the parent unmount handles it */
  });

  return (
    <Show when={props.open}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        class="fixed inset-0 z-[var(--z-modal)] flex items-start justify-center bg-black/40 px-4 pt-[12vh]"
        onClick={(e) => {
          if (e.target === e.currentTarget) props.onClose();
        }}
      >
        <div
          class="w-full max-w-xl overflow-hidden rounded-lg border border-border-default bg-bg-elevated shadow-xl"
          onKeyDown={onKeyDown}
        >
          <input
            ref={inputRef}
            type="text"
            value={query()}
            onInput={(e) => {
              setQuery(e.currentTarget.value);
              setSelected(0);
            }}
            placeholder="Type a command…"
            aria-controls={listboxId}
            aria-activedescendant={
              filtered()[selected()]
                ? `${listboxId}-${filtered()[selected()]!.id}`
                : undefined
            }
            class="block w-full bg-transparent px-4 py-3 text-sm text-text-primary placeholder:text-text-tertiary outline-none"
          />
          <div
            id={listboxId}
            role="listbox"
            class="max-h-[50vh] overflow-y-auto border-t border-border-subtle"
          >
            <Show
              when={filtered().length > 0}
              fallback={
                <p class="px-4 py-6 text-center text-sm text-text-tertiary">
                  No matches.
                </p>
              }
            >
              <For each={filtered()}>
                {(item, i) => (
                  <button
                    type="button"
                    id={`${listboxId}-${item.id}`}
                    role="option"
                    aria-selected={i() === selected()}
                    onClick={() => fire(item)}
                    onMouseMove={() => setSelected(i())}
                    class={[
                      "flex w-full items-center justify-between gap-3",
                      "px-4 py-2.5 text-left text-sm",
                      i() === selected()
                        ? "bg-bg-hover text-text-primary"
                        : "text-text-secondary",
                    ].join(" ")}
                  >
                    <span class="flex min-w-0 flex-col">
                      <span class="truncate text-text-primary">
                        {item.label}
                      </span>
                      <Show when={item.hint}>
                        <span class="truncate text-xs text-text-tertiary">
                          {item.hint}
                        </span>
                      </Show>
                    </span>
                    <span class="text-[0.65rem] uppercase tracking-wide text-text-tertiary">
                      {item.category}
                    </span>
                  </button>
                )}
              </For>
            </Show>
          </div>
          <div class="flex items-center justify-between gap-2 border-t border-border-subtle bg-bg-secondary px-4 py-2 text-[0.7rem] text-text-tertiary">
            <span class="flex items-center gap-2">
              <Kbd>ArrowUp</Kbd>
              <Kbd>ArrowDown</Kbd>
              navigate
            </span>
            <span class="flex items-center gap-2">
              <Kbd>Enter</Kbd>
              select
              <Kbd>Esc</Kbd>
              close
            </span>
          </div>
        </div>
      </div>
    </Show>
  );
};
