/**
 * Cycle C Sprint 4 Day 1+2 — mode-agnostic composer.
 *
 * Single textarea + a mode pill + @-mention picker + drag-drop attach
 * for the entire AMOR product surface.  Slash-command parsing routes
 * a prompt to the right mode without forcing the user to navigate to
 * a per-mode route:
 *
 *     /build snake game in HTML
 *     /research compare CRDT vs OT
 *     /think evaluate trade-offs of moving to Rust
 *
 * If the input doesn't start with a recognised slash command, the
 * active pill mode wins (defaulting to last-used mode, persisted in
 * localStorage).
 *
 * What this Day 1 + Day 2 ships:
 * * Slash-command parser ({build, research, think, consortium,
 *   sentinel, system} + aliases).
 * * Mode pill with OKLch accent per mode + listbox-style ModePicker.
 * * @-mention picker — debounced GET /api/repo/symbols, arrow / Enter
 *   navigation, inserts ``@[name](path:line)`` token at the cursor.
 * * Drag-drop attach overlay + paste-clipboard handler for images
 *   and text files; attachment chips render above the textarea.
 * * Cmd/Ctrl-Enter sends; Shift-Enter for newline (chat convention,
 *   matches existing ChatComposer).
 * * Persistent last-mode in localStorage["amor.composer.mode"].
 *
 * What Day 3-5 add:
 * * Day 3 — message hover-actions bar.
 * * Day 4 — tool-call cards.
 * * Day 5 — axe-core a11y gate.
 *
 * Existing per-mode pages (Build.tsx, Research.tsx etc) keep working
 * unchanged — UnifiedComposer is opt-in via a new /chat route.
 */

import {
  type Component,
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  on,
  onCleanup,
  onMount,
} from "solid-js";

import { Button, Textarea } from "../ui";
import { type ModeKey, MODES, type ModeMeta } from "../../lib/types";
import {
  parseSlashCommand,
  detectMention,
  modeMeta,
  MODE_GLYPH,
  type RepoSymbol,
  type ParsedInput,
} from "./composer-parsers";
import { modeLabel, t } from "../../i18n";

// Re-export the pure parsers so existing imports from
// ``UnifiedComposer`` keep working.
export { parseSlashCommand, detectMention } from "./composer-parsers";
export type { ParsedInput, RepoSymbol } from "./composer-parsers";

// localStorage keys.
const LS_KEY_LAST_MODE = "amor.composer.mode";

const DEFAULT_MODE: ModeKey = "build";

// Debounce + page-size for the @-mention symbol query.
const MENTION_DEBOUNCE_MS = 150;
const MENTION_PAGE_SIZE = 8;
// Cap one attachment payload size (display-only check; backend still
// enforces).
const ATTACH_MAX_BYTES = 10 * 1024 * 1024; // 10 MB
// Comma-separated list of MIME prefixes the paste-clipboard handler
// captures by default.  Anything outside this set goes through the
// browser's native textarea paste (text body inserted as text).
const PASTE_FILE_PREFIXES = ["image/", "text/", "application/json"];

const loadLastMode = (): ModeKey => {
  try {
    const raw = localStorage.getItem(LS_KEY_LAST_MODE);
    if (raw && MODES.some((m) => m.key === raw)) {
      return raw as ModeKey;
    }
  } catch {
    // ignore localStorage failures
  }
  return DEFAULT_MODE;
};

const saveLastMode = (mode: ModeKey): void => {
  try {
    localStorage.setItem(LS_KEY_LAST_MODE, mode);
  } catch {
    // ignore
  }
};

export interface ComposerSubmission {
  text: string;
  mode: ModeKey;
  attachments: File[];
}

export interface UnifiedComposerProps {
  /** Called when the user submits — receives final text + resolved mode. */
  onSubmit: (text: string, mode: ModeKey) => void | Promise<void>;
  /** Optional richer submit callback receiving the full submission record
   *  (text + mode + attachments).  Called *in addition to* ``onSubmit``
   *  so existing consumers don't break.  Day 4 will phase out the
   *  legacy two-arg form in favour of this. */
  onSubmitRich?: (submission: ComposerSubmission) => void | Promise<void>;
  /** Disable the send button + show Cancel instead. */
  busy?: boolean;
  onCancel?: () => void;
  /** Override the placeholder.  Default: "Ask anything — or /build, /research, /think…" */
  placeholder?: string;
  /** Initial mode override (otherwise restored from localStorage). */
  initialMode?: ModeKey;
  /** Cycle UI 2026-05-20 — Notify parent of every text change so it can
   *  feed an intent classifier or other side-effect.  Parent should
   *  treat this as a high-frequency event and debounce downstream
   *  work itself.  When omitted the composer behaves as before. */
  onTextChange?: (text: string) => void;
  /** Cycle UI 2026-05-20 — When provided, this mode replaces the
   *  composer's internal ``activeMode`` until the user picks a
   *  different mode via the ModePicker (which then "locks" the
   *  user's choice and ignores further overrides).  Used by the
   *  auto-mode classifier in UnifiedChat to suggest a mode while
   *  letting manual selection still win. */
  modeOverride?: ModeKey;
  /** Cycle UI 2026-05-20 — Optional flag rendered next to the
   *  ModePill, e.g. "auto" / "uncertain" / classifier confidence
   *  badge.  Parent owns the string so it can be localised + carry
   *  inline-classifier state.  When omitted, no badge is rendered. */
  modeBadge?: string;
}

const formatBytes = (n: number): string => {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
};

export const UnifiedComposer: Component<UnifiedComposerProps> = (props) => {
  const [text, setText] = createSignal("");
  const [activeMode, setActiveMode] = createSignal<ModeKey>(
    props.initialMode ?? DEFAULT_MODE,
  );
  // Cycle UI 2026-05-20 — once the user explicitly clicks ModePicker,
  // their choice "locks" the composer's mode and modeOverride is
  // ignored until they click a different mode (or clear via slash).
  const [userPickedMode, setUserPickedMode] = createSignal(false);
  // Effective mode: user pick > modeOverride > activeMode > DEFAULT.
  const effectiveMode = (): ModeKey => {
    if (userPickedMode()) return activeMode();
    if (props.modeOverride) return props.modeOverride;
    return activeMode();
  };
  const [pickerOpen, setPickerOpen] = createSignal(false);
  const [caret, setCaret] = createSignal(0);
  const [mentionMatches, setMentionMatches] = createSignal<RepoSymbol[]>([]);
  const [mentionSelected, setMentionSelected] = createSignal(0);
  const [mentionLoading, setMentionLoading] = createSignal(false);
  const [attachments, setAttachments] = createSignal<File[]>([]);
  const [dragActive, setDragActive] = createSignal(false);
  let textareaRef: HTMLTextAreaElement | undefined;
  let mentionAbort: AbortController | null = null;
  let mentionDebounceTimer: number | null = null;
  let fileInputRef: HTMLInputElement | undefined;

  onMount(() => {
    if (!props.initialMode) {
      setActiveMode(loadLastMode());
    }
  });

  onCleanup(() => {
    if (mentionAbort) mentionAbort.abort();
    if (mentionDebounceTimer) window.clearTimeout(mentionDebounceTimer);
  });

  // Persist mode as it changes (skip the initial load).
  createEffect(
    on(activeMode, (m) => saveLastMode(m), { defer: true }),
  );

  // Live-parsed view of what the slash command resolves to.  Used to
  // show a soft hint under the textarea ("/build → Build mode") so
  // the user gets feedback before pressing Enter.
  const livePreview = createMemo<ParsedInput>(() =>
    parseSlashCommand(text(), effectiveMode()),
  );

  // Live mention detection.  This is ``createMemo`` so the picker
  // open/close transitions reactively as caret + text change.
  const mention = createMemo(() => detectMention(text(), caret()));
  const mentionOpen = () => mention() !== null;

  // Debounced fetch when the mention query changes.
  createEffect(
    on(
      () => {
        const m = mention();
        return m ? m.query : null;
      },
      (q) => {
        // Close → clear state.
        if (q === null) {
          setMentionMatches([]);
          setMentionSelected(0);
          if (mentionAbort) mentionAbort.abort();
          if (mentionDebounceTimer) {
            window.clearTimeout(mentionDebounceTimer);
            mentionDebounceTimer = null;
          }
          return;
        }
        if (mentionDebounceTimer) {
          window.clearTimeout(mentionDebounceTimer);
        }
        mentionDebounceTimer = window.setTimeout(() => {
          fetchMentions(q);
        }, MENTION_DEBOUNCE_MS);
      },
      { defer: true },
    ),
  );

  const fetchMentions = async (q: string): Promise<void> => {
    if (mentionAbort) mentionAbort.abort();
    const ctrl = new AbortController();
    mentionAbort = ctrl;
    setMentionLoading(true);
    try {
      const url = `/api/repo/symbols?q=${encodeURIComponent(q)}&limit=${MENTION_PAGE_SIZE}`;
      const r = await fetch(url, {
        credentials: "include",
        signal: ctrl.signal,
        headers: { Accept: "application/json" },
      });
      if (!r.ok) {
        setMentionMatches([]);
        return;
      }
      const body = (await r.json()) as { items?: RepoSymbol[] };
      setMentionMatches(body.items ?? []);
      setMentionSelected(0);
    } catch (err) {
      if ((err as { name?: string }).name === "AbortError") return;
      setMentionMatches([]);
    } finally {
      if (mentionAbort === ctrl) mentionAbort = null;
      setMentionLoading(false);
    }
  };

  const insertMention = (sym: RepoSymbol): void => {
    const m = mention();
    if (!m || !textareaRef) return;
    const before = text().slice(0, m.atIndex);
    const after = text().slice(caret());
    const sep = after.startsWith(" ") || after === "" ? "" : " ";
    const next = `${before}${sym.label}${sep}${after}`;
    setText(next);
    const newCaret = before.length + sym.label.length + sep.length;
    requestAnimationFrame(() => {
      if (!textareaRef) return;
      textareaRef.focus();
      textareaRef.setSelectionRange(newCaret, newCaret);
      setCaret(newCaret);
    });
  };

  const submit = () => {
    if (props.busy) return;
    const parsed = parseSlashCommand(text(), effectiveMode());
    if (!parsed.text && attachments().length === 0) return;
    const files = attachments().slice();
    void props.onSubmit(parsed.text, parsed.mode);
    void props.onSubmitRich?.({ text: parsed.text, mode: parsed.mode, attachments: files });
    setText("");
    props.onTextChange?.("");
    setAttachments([]);
    // After submit, release the user-picked lock so the next message's
    // classifier suggestion can win again (matches Claude/ChatGPT
    // semantics: each message starts fresh in auto-mode).
    setUserPickedMode(false);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    // 1) @-mention picker hijacks arrows + Enter when open.
    if (mentionOpen() && mentionMatches().length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionSelected((i) =>
          Math.min(mentionMatches().length - 1, i + 1),
        );
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionSelected((i) => Math.max(0, i - 1));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const sym = mentionMatches()[mentionSelected()];
        if (sym) insertMention(sym);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        // Closing the picker = inserting a space after the @ token,
        // which our regex no longer matches as a live mention.
        const m = mention();
        if (m && textareaRef) {
          const at = m.atIndex;
          const before = text().slice(0, at);
          const after = text().slice(caret());
          setText(`${before}@ ${m.query}${after}`);
        }
        return;
      }
    }

    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
      return;
    }
    if (e.key === "Escape" && pickerOpen()) {
      e.preventDefault();
      setPickerOpen(false);
    }
  };

  const trackCaret = (el: HTMLTextAreaElement) => {
    setCaret(el.selectionStart);
  };

  const pickMode = (mode: ModeKey) => {
    setActiveMode(mode);
    setUserPickedMode(true);  // Cycle UI 2026-05-20 — lock the choice
    setPickerOpen(false);
    // Strip any matching slash prefix the user typed earlier — once
    // they pick a mode explicitly, the prefix is redundant noise.
    const { text: stripped } = parseSlashCommand(text(), mode);
    setText(stripped);
    props.onTextChange?.(stripped);
    requestAnimationFrame(() => textareaRef?.focus());
  };

  // Attachment helpers ------------------------------------------------

  const addFiles = (incoming: FileList | File[]): void => {
    const list = Array.from(incoming).filter((f) => f.size <= ATTACH_MAX_BYTES);
    if (list.length === 0) return;
    setAttachments((prev) => [...prev, ...list]);
  };

  const removeFile = (idx: number): void => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  };

  const onDragOver = (e: DragEvent) => {
    if (!e.dataTransfer || e.dataTransfer.types.length === 0) return;
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    setDragActive(true);
  };

  const onDragLeave = (e: DragEvent) => {
    // Only flip off when the leave event leaves the form itself —
    // child elements emit their own dragleave.
    if (e.currentTarget === e.target) {
      setDragActive(false);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragActive(false);
    if (!e.dataTransfer) return;
    if (e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files);
  };

  const onPaste = (e: ClipboardEvent) => {
    if (!e.clipboardData) return;
    const items = Array.from(e.clipboardData.items);
    const fileItems = items.filter(
      (it) =>
        it.kind === "file" &&
        PASTE_FILE_PREFIXES.some((p) => it.type.startsWith(p)),
    );
    if (fileItems.length === 0) return;
    e.preventDefault();
    const files: File[] = [];
    for (const it of fileItems) {
      const f = it.getAsFile();
      if (f) files.push(f);
    }
    if (files.length > 0) addFiles(files);
  };

  return (
    <form
      class="relative flex flex-col gap-2 border-t border-border-subtle bg-bg-secondary px-5 py-4"
      onSubmit={(e: SubmitEvent) => {
        e.preventDefault();
        submit();
      }}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      data-amor-composer="unified"
    >
      {/* Drag-drop overlay */}
      <Show when={dragActive()}>
        <div
          class="pointer-events-none absolute inset-0 z-[var(--z-overlay)] flex items-center justify-center rounded-md border-2 border-dashed border-text-primary/50 bg-bg-elevated/80 text-sm text-text-primary backdrop-blur-sm"
          aria-hidden="true"
          data-amor-overlay="drop"
        >
          Drop to attach
        </div>
      </Show>

      {/* Attachment chips */}
      <Show when={attachments().length > 0}>
        <ul
          class="flex flex-wrap gap-1.5"
          aria-label="Attachments"
          data-amor-attachments="list"
        >
          <For each={attachments()}>
            {(file, i) => (
              <li class="flex items-center gap-1.5 rounded-full border border-border-subtle bg-bg-elevated px-2.5 py-0.5 text-[0.7rem] text-text-secondary">
                <span class="truncate max-w-[14rem]" title={file.name}>
                  {file.name}
                </span>
                <span class="text-text-tertiary tabular-nums">
                  {formatBytes(file.size)}
                </span>
                <button
                  type="button"
                  onClick={() => removeFile(i())}
                  aria-label={`Remove ${file.name}`}
                  class="text-text-tertiary hover:text-text-primary"
                >
                  ×
                </button>
              </li>
            )}
          </For>
        </ul>
      </Show>

      <div class="relative">
        <Textarea
          ref={(el: HTMLTextAreaElement) => (textareaRef = el)}
          value={text()}
          onInput={(e: InputEvent & { currentTarget: HTMLTextAreaElement }) => {
            const v = e.currentTarget.value;
            setText(v);
            trackCaret(e.currentTarget);
            props.onTextChange?.(v);
          }}
          onKeyUp={(e: KeyboardEvent & { currentTarget: HTMLTextAreaElement }) =>
            trackCaret(e.currentTarget)
          }
          onClick={(e: MouseEvent & { currentTarget: HTMLTextAreaElement }) =>
            trackCaret(e.currentTarget)
          }
          onSelect={(e: Event & { currentTarget: HTMLTextAreaElement }) =>
            trackCaret(e.currentTarget)
          }
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={
            props.placeholder ?? t("chat.composer.placeholder")
          }
          minRows={2}
          maxRows={10}
          class="bg-bg-elevated"
          autofocus
          aria-label={t("common.send")}
          aria-controls={mentionOpen() ? "amor-mention-listbox" : undefined}
          aria-activedescendant={
            mentionOpen() && mentionMatches().length > 0
              ? `amor-mention-opt-${mentionSelected()}`
              : undefined
          }
        />

        {/* @-mention picker */}
        <Show when={mentionOpen()}>
          <MentionPicker
            matches={mentionMatches()}
            selected={mentionSelected()}
            loading={mentionLoading()}
            query={mention()?.query ?? ""}
            onPick={insertMention}
            onHover={(i) => setMentionSelected(i)}
          />
        </Show>
      </div>

      {/* Live slash-resolution hint */}
      <Show
        when={livePreview().slashUsed && livePreview().mode !== activeMode()}
      >
        <p
          class="text-[0.7rem] text-text-tertiary"
          role="status"
          aria-live="polite"
        >
          slash routes to {modeLabel(livePreview().mode)} (one-shot
          override)
        </p>
      </Show>

      <div class="flex items-center justify-between gap-2">
        <ModePill
          mode={effectiveMode()}
          onClick={() => setPickerOpen((o: boolean) => !o)}
          expanded={pickerOpen()}
          badge={props.modeBadge}
        />
        <button
          type="button"
          class="rounded border border-border-subtle bg-bg-elevated px-2 py-1 text-xs text-text-secondary hover:border-border-strong"
          onClick={() => fileInputRef?.click()}
          aria-label={t("common.attach")}
          data-amor-attach="trigger"
        >
          {t("common.attach")}
        </button>
        <input
          ref={(el: HTMLInputElement) => (fileInputRef = el)}
          type="file"
          multiple
          class="hidden"
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e: Event & { currentTarget: HTMLInputElement }) => {
            if (e.currentTarget.files) addFiles(e.currentTarget.files);
            e.currentTarget.value = "";
          }}
        />
        <span class="ml-2 flex-1 truncate text-xs text-text-tertiary">
          {t("chat.send_hint")}
        </span>
        <Show
          when={!props.busy}
          fallback={
            <Button
              variant="secondary"
              size="sm"
              onClick={() => props.onCancel?.()}
              type="button"
            >
              {t("common.cancel")}
            </Button>
          }
        >
          <Button
            type="submit"
            size="sm"
            disabled={!livePreview().text && attachments().length === 0}
          >
            {t("common.send")}
          </Button>
        </Show>
      </div>

      {/* Inline mode picker (Day 1 fallback — Kobalte combobox swap deferred
          to a follow-up; the listbox-style picker keeps the bundle light
          and Day 5's axe-core gate validates the ARIA bookkeeping). */}
      <Show when={pickerOpen()}>
        <ModePicker
          activeMode={activeMode()}
          onPick={pickMode}
          onClose={() => setPickerOpen(false)}
        />
      </Show>
    </form>
  );
};

const ModePill: Component<{
  mode: ModeKey;
  onClick: () => void;
  expanded: boolean;
  /** Cycle UI 2026-05-20 — optional badge text rendered to the right
   *  of the mode label, e.g. "auto" / "uncertain".  Parent owns the
   *  string so it can be localised + driven by classifier state. */
  badge?: string;
}> = (props) => {
  const meta = () => modeMeta(props.mode);
  return (
    <button
      type="button"
      onClick={props.onClick}
      class="group flex items-center gap-1.5 rounded-full border border-border-subtle bg-bg-elevated px-3 py-1 text-xs hover:border-border-strong"
      aria-haspopup="listbox"
      aria-expanded={props.expanded}
      data-amor-mode={props.mode}
    >
      <span
        class="flex h-3.5 w-3.5 items-center justify-center text-[0.85rem] leading-none"
        style={{ color: "var(--mode-accent)" }}
        aria-hidden="true"
      >
        {MODE_GLYPH[props.mode]}
      </span>
      <span class="font-medium">{modeLabel(meta())}</span>
      <Show when={props.badge}>
        <span
          class="rounded bg-bg-hover px-1.5 py-0.5 text-[0.6rem] font-medium uppercase tracking-wide text-text-tertiary"
          data-amor-mode-badge=""
        >
          {props.badge}
        </span>
      </Show>
      <span class="text-text-tertiary" aria-hidden="true">
        {props.expanded ? "▴" : "▾"}
      </span>
    </button>
  );
};

const ModePicker: Component<{
  activeMode: ModeKey;
  onPick: (mode: ModeKey) => void;
  onClose: () => void;
}> = (props) => {
  // Cycle UI Phase 4.3 — Tailwind responsive variant.  Below `md`
  // (768 px) the picker becomes a bottom-anchored sheet with
  // safe-area-inset padding so iOS Safari's home indicator doesn't
  // crop the last row.  Above `md` it's the same listbox dropdown
  // as before.
  return (
    <div
      role="listbox"
      aria-label="Pick mode"
      class={[
        "z-[var(--z-dropdown)] gap-0.5 rounded-md border border-border-subtle bg-bg-elevated p-1 shadow-md",
        // Mobile (default): fixed bottom-sheet spanning width.
        "fixed inset-x-0 bottom-0 grid grid-cols-2 gap-1 rounded-b-none pb-[max(env(safe-area-inset-bottom),0.5rem)]",
        // Desktop: absolutely positioned just above the pill.
        "md:absolute md:inset-x-auto md:bottom-[100%] md:left-5 md:mb-2 md:grid-cols-2 md:rounded-md md:pb-1",
      ].join(" ")}
      data-amor-mode-picker=""
    >
      {MODES.map((meta: ModeMeta) => (
        <button
          type="button"
          role="option"
          aria-selected={meta.key === props.activeMode}
          onClick={() => props.onPick(meta.key)}
          class="flex items-center gap-2 rounded px-3 py-1.5 text-left text-xs hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-offset-1"
          data-amor-mode={meta.key}
          data-active={meta.key === props.activeMode ? "1" : "0"}
        >
          <span
            class="flex h-3.5 w-3.5 items-center justify-center text-[0.85rem]"
            style={{ color: "var(--mode-accent)" }}
            aria-hidden="true"
          >
            {MODE_GLYPH[meta.key]}
          </span>
          <span class="font-medium">{modeLabel(meta)}</span>
          <span class="ml-auto text-[0.65rem] text-text-tertiary">
            /{meta.key === "thinking" ? "think" : meta.key}
          </span>
        </button>
      ))}
    </div>
  );
};

const MentionPicker: Component<{
  matches: RepoSymbol[];
  selected: number;
  loading: boolean;
  query: string;
  onPick: (sym: RepoSymbol) => void;
  onHover: (i: number) => void;
}> = (props) => {
  return (
    <div
      id="amor-mention-listbox"
      role="listbox"
      aria-label="Repo symbols"
      class="absolute left-0 right-0 top-full z-[var(--z-dropdown)] mt-1 max-h-72 overflow-y-auto rounded-md border border-border-subtle bg-bg-elevated p-1 shadow-md"
      data-amor-mention="picker"
    >
      <Show
        when={props.matches.length > 0}
        fallback={
          <p class="px-3 py-2 text-xs text-text-tertiary">
            {props.loading
              ? "Searching…"
              : props.query
                ? `No symbols match “${props.query}”`
                : "Type to search the codebase…"}
          </p>
        }
      >
        <For each={props.matches}>
          {(sym, i) => (
            <button
              type="button"
              role="option"
              id={`amor-mention-opt-${i()}`}
              aria-selected={i() === props.selected}
              onMouseDown={(e: MouseEvent) => {
                e.preventDefault(); // keep textarea focus
                props.onPick(sym);
              }}
              onMouseMove={() => props.onHover(i())}
              class={[
                "flex w-full items-center justify-between gap-3 rounded px-2 py-1.5 text-left text-xs",
                i() === props.selected
                  ? "bg-bg-hover text-text-primary"
                  : "text-text-secondary",
              ].join(" ")}
              data-amor-mention-kind={sym.kind}
            >
              <span class="flex min-w-0 flex-col">
                <span class="truncate font-mono text-[0.75rem] text-text-primary">
                  {sym.name}
                </span>
                <span class="truncate text-[0.65rem] text-text-tertiary">
                  {sym.path}:{sym.line}
                  {sym.parent ? ` · ${sym.parent}` : ""}
                </span>
              </span>
              <span class="text-[0.6rem] uppercase tracking-wide text-text-tertiary">
                {sym.kind}
              </span>
            </button>
          )}
        </For>
      </Show>
    </div>
  );
};
