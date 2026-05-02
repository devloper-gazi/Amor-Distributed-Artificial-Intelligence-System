import {
  type Component,
  createSignal,
  Show,
} from "solid-js";
import { Button, Textarea } from "../ui";

export interface ChatComposerProps {
  onSubmit: (text: string) => void;
  /** When true, the send button is replaced with a Cancel that
   *  invokes ``onCancel``. */
  busy?: boolean;
  onCancel?: () => void;
  placeholder?: string;
  /** Shown next to the send button — usually "⌘+Enter to send". */
  hint?: string;
}

/**
 * Chat composer.  Cmd/Ctrl-Enter sends; Shift-Enter newline; plain
 * Enter newline (chat convention — accidental sends are worse than
 * an extra keystroke).
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
      class="flex flex-col gap-2 border-t border-border-subtle bg-bg-secondary px-5 py-4"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
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
        <span class="text-xs text-text-tertiary">
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
              Cancel
            </Button>
          }
        >
          <Button
            type="submit"
            size="sm"
            disabled={!text().trim()}
          >
            Send
          </Button>
        </Show>
      </div>
    </form>
  );
};
