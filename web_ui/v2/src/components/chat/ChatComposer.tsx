import {
  type Component,
  For,
  Show,
  createSignal,
} from "solid-js";
import { Button, Textarea } from "../ui";
import { t } from "../../i18n";

/**
 * One option in an effort/depth segmented control.  Caller supplies
 * the i18n keys; the composer renders ``t(label_key)`` + the
 * description as a ``title`` tooltip.
 */
export interface EffortTierOption {
  value: string;
  label_key: string;
  description_key: string;
}

export interface ChatComposerProps {
  onSubmit: (text: string) => void;
  /** When true, the send button is replaced with a Cancel that
   *  invokes ``onCancel``. */
  busy?: boolean;
  onCancel?: () => void;
  placeholder?: string;
  /** Shown next to the send button — usually "⌘+Enter to send". */
  hint?: string;
  /** Cycle C polish — segmented control above the textarea.  When
   *  ``effortTiers`` is provided, the composer renders a row of
   *  buttons (one per tier) that call ``onEffortChange`` on click.
   *  Pass-through pattern: the route owns the signal so persistence
   *  + default selection live there.  When omitted, the composer
   *  renders exactly the same as before — backward compatible. */
  effortTiers?: ReadonlyArray<EffortTierOption>;
  effortValue?: string;
  onEffortChange?: (next: string) => void;
  /** Localised group label rendered above the segmented control.
   *  Defaults to ``t("composer.effort")`` so callers usually omit. */
  effortLabelKey?: string;
}

/**
 * Chat composer.  Cmd/Ctrl-Enter sends; Shift-Enter newline; plain
 * Enter newline (chat convention — accidental sends are worse than
 * an extra keystroke).
 *
 * Cycle C polish — optional effort segmented control above the
 * textarea, rendered when ``effortTiers`` is provided.  Used by
 * Research today; Build/Thinking can opt in by passing the same
 * three props (``effortTiers`` / ``effortValue`` / ``onEffortChange``).
 */
export const ChatComposer: Component<ChatComposerProps> = (props) => {
  const [text, setText] = createSignal("");

  const submit = () => {
    const value = text().trim();
    if (!value || props.busy) return;
    props.onSubmit(value);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent) => {
    const isSendCombo =
      e.key === "Enter" && (e.metaKey || e.ctrlKey);
    if (isSendCombo) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form
      class="flex flex-col gap-2 border-t border-border-subtle bg-bg-elevated-v25 px-5 py-4"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <Show when={props.effortTiers && props.effortTiers.length > 0}>
        <div
          class="flex flex-wrap items-center gap-2"
          role="radiogroup"
          aria-label={t(props.effortLabelKey ?? "composer.effort_aria")}
          data-amor-effort-group=""
        >
          <span class="text-[0.7rem] font-medium uppercase tracking-wide text-text-subtle">
            {t(props.effortLabelKey ?? "composer.effort")}
          </span>
          <div class="flex flex-wrap gap-1 rounded-md border border-border-subtle bg-bg-elevated p-0.5">
            <For each={props.effortTiers}>
              {(tier) => (
                <button
                  type="button"
                  role="radio"
                  aria-checked={tier.value === props.effortValue}
                  title={t(tier.description_key)}
                  onClick={() => props.onEffortChange?.(tier.value)}
                  class={[
                    "amor-touch rounded px-3 py-1 text-xs font-medium transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2",
                    tier.value === props.effortValue
                      ? "bg-bg-hover text-text-display"
                      : "text-text-body hover:bg-bg-hover hover:text-text-display",
                  ].join(" ")}
                  data-amor-effort={tier.value}
                  data-amor-effort-active={
                    tier.value === props.effortValue ? "1" : "0"
                  }
                >
                  {t(tier.label_key)}
                </button>
              )}
            </For>
          </div>
        </div>
      </Show>
      <Textarea
        value={text()}
        onInput={(e) => setText(e.currentTarget.value)}
        onKeyDown={onKeyDown}
        placeholder={props.placeholder ?? "Ask anything…"}
        minRows={2}
        maxRows={10}
        class="bg-bg-elevated"
        autofocus
      />
      <div class="flex items-center justify-between gap-2">
        <span class="text-xs text-text-subtle">
          {props.hint ?? "⌘+Enter to send · Shift+Enter for newline"}
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
            disabled={!text().trim()}
          >
            {t("composer.send")}
          </Button>
        </Show>
      </div>
    </form>
  );
};
