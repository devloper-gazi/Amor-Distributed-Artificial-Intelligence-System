import { type Component, Show } from "solid-js";
import type { StreamStatus } from "../../lib/sse";
import { t } from "../../i18n";

interface ConnectionBannerProps {
  status: StreamStatus;
}

/**
 * Slim banner that only renders when SSE is reconnecting / offline.
 * Hidden when status is `open` or `closed` (closed = stream
 * intentionally torn down by the page leaving).
 */
export const ConnectionBanner: Component<ConnectionBannerProps> = (props) => {
  return (
    <Show when={props.status === "reconnecting" || props.status === "offline"}>
      <div
        role="status"
        aria-live="polite"
        class={[
          "flex h-8 shrink-0 items-center justify-center px-3 text-xs",
          "border-b border-border-subtle",
          props.status === "offline"
            ? "bg-status-failed/10 text-status-failed"
            : "bg-status-warming/10 text-status-warming",
        ].join(" ")}
        style={{
          color:
            props.status === "offline"
              ? "var(--color-status-failed)"
              : "var(--color-status-warming)",
        }}
      >
        <span class="motion-safe:animate-pulse motion-reduce:hidden">●</span>
        <span class="ml-2">
          {props.status === "offline"
            ? t("shell.offline_long")
            : t("shell.offline")}
        </span>
      </div>
    </Show>
  );
};
