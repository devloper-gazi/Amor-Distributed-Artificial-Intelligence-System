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
import { modeLabel, modeSubtitle, t } from "../../i18n";

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
 * Hand-rolled Cmd-K command palette ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â no external cmdk dep.
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
    // ``t()`` reads the locale signal ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â invoking it inside this memo
    // means the palette re-renders on every locale flip without any
    // per-item bookkeeping.
    const cat = {
      mode: t("palette.category.mode"),
      system: t("palette.category.system"),
      theme: t("palette.category.theme"),
      account: t("palette.category.account"),
    };
    const out: PaletteItem[] = [];
    for (const mode of MODES) {
      out.push({
        id: `mode-${mode.key}`,
        label: modeLabel(mode),
        hint: modeSubtitle(mode),
        category: cat.mode,
        run: () => nav(mode.href),
      });
    }
    out.push(
      {
        id: "sys-diagnostics",
        label: t("palette.item.diagnostics.label"),
        hint: t("palette.item.diagnostics.hint"),
        category: cat.system,
        run: () => nav("/system"),
      },
      {
        id: "sys-settings",
        label: t("palette.item.settings.label"),
        hint: t("palette.item.settings.hint"),
        category: cat.system,
        run: () => nav("/settings"),
      },
      {
        id: "sys-baselines",
        label: t("palette.item.baselines.label"),
        hint: t("palette.item.baselines.hint"),
        category: cat.system,
        run: () => nav("/admin/baselines"),
      },
      {
        id: "sys-llm",
        label: t("palette.item.llm.label"),
        hint: t("palette.item.llm.hint"),
        category: cat.system,
        run: () => nav("/admin/llm"),
      },
      {
        id: "sys-evals",
        label: t("palette.item.evals.label"),
        hint: t("palette.item.evals.hint"),
        category: cat.system,
        run: () => nav("/admin/evals"),
      },
      {
        id: "sys-training",
        label: t("palette.item.training.label"),
        hint: t("palette.item.training.hint"),
        category: cat.system,
        run: () => nav("/admin/training"),
      },
      {
        id: "sys-memory",
        label: t("palette.item.memory.label"),
        hint: t("palette.item.memory.hint"),
        category: cat.system,
        run: () => nav("/admin/memory"),
      },
      {
        id: "mode-agent",
        label: t("palette.item.agent.label"),
        hint: t("palette.item.agent.hint"),
        category: cat.mode,
        run: () => nav("/agent"),
      },
      {
        id: "mode-chat",
        label: t("palette.item.chat.label"),
        hint: t("palette.item.chat.hint"),
        category: cat.mode,
        run: () => nav("/chat"),
      },
      {
        id: "sys-showcase",
        label: t("palette.item.showcase.label"),
        hint: t("palette.item.showcase.hint"),
        category: cat.system,
        run: () => nav("/showcase"),
      },
      {
        id: "theme-light",
        label: t("palette.item.theme_light.label"),
        category: cat.theme,
        run: () => setTheme("light"),
      },
      {
        id: "theme-dark",
        label: t("palette.item.theme_dark.label"),
        category: cat.theme,
        run: () => setTheme("dark"),
      },
      {
        id: "theme-system",
        label: t("palette.item.theme_system.label"),
        category: cat.theme,
        run: () => setTheme("system"),
      },
    );
    if (auth.user()) {
      out.push({
        id: "account-logout",
        label: t("palette.item.signout.label"),
        hint: auth.user()?.username ?? "",
        category: cat.account,
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
    // Tokenize on whitespace + each token must hit somewhere.  Then
    // SCORE so label-matches outrank hint-only matches ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â the user
    // typing "theme system" wants the "Theme: System" command, not
    // the Settings page (whose hint happens to contain "theme,
    // account").
    const tokens = q.split(/\s+/).filter(Boolean);
    const scored: Array<{ item: PaletteItem; score: number }> = [];
    for (const it of items()) {
      const label = it.label.toLowerCase();
      const hint = (it.hint ?? "").toLowerCase();
      const cat = it.category.toLowerCase();
      const haystack = `${label} ${hint} ${cat}`;
      if (!tokens.every((t) => haystack.includes(t))) continue;

      let score = 0;
      // +50 per token that hits the LABEL.
      for (const t of tokens) {
        if (label.includes(t)) score += 50;
        else if (hint.includes(t)) score += 10;
        else if (cat.includes(t)) score += 5;
      }
      // Bonus when the FULL query is a contiguous substring of the
      // label (e.g. "theme system" finds "theme: system" as one
      // chunk after stripping the colon).
      const labelStripped = label.replace(/[:,]/g, " ");
      if (labelStripped.includes(q)) score += 100;
      // Tiny stable-sort tiebreak: prefer shorter labels (more
      // specific commands).
      score -= label.length * 0.01;

      scored.push({ item: it, score });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.map((s) => s.item);
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

  // Re-focus input every time we transition closed ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ open.  Solid's
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
        aria-label={t("palette.dialog_label")}
        class="fixed inset-0 z-[var(--z-modal)] flex items-start justify-center bg-black/40 px-4 pt-[12vh]"
        onClick={(e) => {
          if (e.target === e.currentTarget) props.onClose();
        }}
      >
        <div
          class="w-full max-w-xl overflow-hidden rounded-lg border border-border-strong-v25 bg-bg-elevated shadow-xl"
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
            placeholder={t("palette.placeholder")}
            aria-controls={listboxId}
            aria-activedescendant={
              filtered()[selected()]
                ? `${listboxId}-${filtered()[selected()]!.id}`
                : undefined
            }
            class="block w-full bg-transparent px-4 py-3 text-sm text-text-display placeholder:text-text-subtle outline-none"
          />
          <div
            id={listboxId}
            role="listbox"
            class="max-h-[50vh] overflow-y-auto border-t border-border-subtle"
          >
            <Show
              when={filtered().length > 0}
              fallback={
                <p class="px-4 py-6 text-center text-sm text-text-subtle">
                  {t("palette.empty")}
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
                        ? "bg-bg-hover text-text-display"
                        : "text-text-body",
                    ].join(" ")}
                  >
                    <span class="flex min-w-0 flex-col">
                      <span class="truncate text-text-display">
                        {item.label}
                      </span>
                      <Show when={item.hint}>
                        <span class="truncate text-xs text-text-subtle">
                          {item.hint}
                        </span>
                      </Show>
                    </span>
                    <span class="text-[0.65rem] uppercase tracking-wide text-text-subtle">
                      {item.category}
                    </span>
                  </button>
                )}
              </For>
            </Show>
          </div>
          <div class="flex items-center justify-between gap-2 border-t border-border-subtle bg-bg-elevated-v25 px-4 py-2 text-[0.7rem] text-text-subtle">
            <span class="flex items-center gap-2">
              <Kbd>ArrowUp</Kbd>
              <Kbd>ArrowDown</Kbd>
              {t("palette.kbd.navigate")}
            </span>
            <span class="flex items-center gap-2">
              <Kbd>Enter</Kbd>
              {t("palette.kbd.select")}
              <Kbd>Esc</Kbd>
              {t("palette.kbd.close")}
            </span>
          </div>
        </div>
      </div>
    </Show>
  );
};
