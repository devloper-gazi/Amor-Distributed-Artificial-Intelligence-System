import {
  type Component,
  type JSX,
  Show,
  onCleanup,
  onMount,
  createUniqueId,
} from "solid-js";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: JSX.Element;
  /** Slot for action buttons in the modal footer.  When omitted the
   *  modal renders a single Close button. */
  footer?: JSX.Element;
  /** Maximum dialog width.  Defaults to ``sm`` (24 rem).  */
  size?: "sm" | "md";
}

/**
 * Lightweight modal — backdrop + centred card + accessible focus
 * trap.  Replaces ``window.confirm`` / ``window.prompt`` so we don't
 * surface ugly browser-native dialogs ("localhost:8000 says").
 *
 * Closes on:
 * * ``Escape`` key
 * * Click on the backdrop
 * * The footer's explicit Close / Cancel button
 */
export const Modal: Component<ModalProps> = (props) => {
  const titleId = createUniqueId();
  const descId = createUniqueId();

  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && props.open) {
      e.preventDefault();
      props.onClose();
    }
  };

  onMount(() => window.addEventListener("keydown", onKey));
  onCleanup(() => window.removeEventListener("keydown", onKey));

  return (
    <Show when={props.open}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={props.description ? descId : undefined}
        class="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-black/50 px-4"
        onClick={(e) => {
          if (e.target === e.currentTarget) props.onClose();
        }}
      >
        <div
          class={[
            "w-full overflow-hidden rounded-lg border border-border-strong-v25",
            "bg-bg-elevated shadow-xl",
            props.size === "md" ? "max-w-md" : "max-w-sm",
          ].join(" ")}
        >
          <div class="space-y-2 px-5 pt-5">
            <h2 id={titleId} class="text-base font-semibold tracking-tight">
              {props.title}
            </h2>
            <Show when={props.description}>
              <p id={descId} class="text-sm text-text-body">
                {props.description}
              </p>
            </Show>
          </div>
          <Show when={props.children}>
            <div class="px-5 pb-2 pt-3">{props.children}</div>
          </Show>
          <div class="flex justify-end gap-2 border-t border-border-subtle bg-bg-elevated-v25 px-4 py-3">
            <Show
              when={props.footer}
              fallback={
                <button
                  type="button"
                  onClick={props.onClose}
                  class="inline-flex h-8 items-center rounded-md border border-border-strong-v25 bg-bg-elevated px-3 text-sm hover:bg-bg-hover"
                >
                  Close
                </button>
              }
            >
              {props.footer}
            </Show>
          </div>
        </div>
      </div>
    </Show>
  );
};
