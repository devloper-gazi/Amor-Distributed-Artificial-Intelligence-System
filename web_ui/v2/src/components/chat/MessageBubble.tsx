import { type Component, Show } from "solid-js";
import type { ChatTurn } from "../../lib/types";
import { renderMarkdown, escapeText } from "../../lib/sanitise";
import { Avatar } from "../ui";
import { MessageActions } from "./MessageActions";
import { t } from "../../i18n";

interface MessageBubbleProps {
  turn: ChatTurn;
  /** Optional action handlers — see MessageActions.tsx.  When all
   *  handlers are undefined, the action bar still renders the copy
   *  button (always available) so users can still grab the text. */
  onEdit?: (turn: ChatTurn) => void;
  onRegenerate?: (turn: ChatTurn) => void;
  onBranch?: (turn: ChatTurn) => void;
  onRate?: (turn: ChatTurn, value: 0 | 1 | -1) => void;
}

const ROLE_LABEL: Record<ChatTurn["role"], string> = {
  user: "You",
  assistant: "AMOR",
  tool: "Tool",
  system: "System",
  // ``approval`` turns short-circuit before reaching MessageBubble
  // (MessageThread renders ApprovalPrompt directly), but TypeScript
  // wants exhaustive coverage of ChatTurn["role"].
  approval: "Approval",
};

const ROLE_VARIANT: Record<ChatTurn["role"], "user" | "system" | "model"> = {
  user: "user",
  assistant: "model",
  tool: "system",
  system: "system",
  approval: "system",
};

const ROLE_INITIALS: Record<ChatTurn["role"], string> = {
  user: "yo",
  assistant: "AM",
  tool: "TL",
  system: "SY",
  approval: "AP",
};

/**
 * One conversation turn.  User turns render as plain escaped text;
 * assistant + tool turns go through marked + DOMPurify so code
 * fences + lists + emphasis render safely.
 *
 * The wrapper ``<div class="group">`` enables the
 * ``opacity-0 group-hover:opacity-100`` pattern that ``MessageActions``
 * uses to reveal its toolbar on hover/focus.
 */
export const MessageBubble: Component<MessageBubbleProps> = (props) => {
  const isUser = () => props.turn.role === "user";
  const showActions = () =>
    props.turn.role === "user" || props.turn.role === "assistant";

  // Cycle UI v2.5 — 3 px sol mode-accent rule color.  For user turns we
  // use --color-text-subtle (mode is the COMPOSER's chosen mode at
  // submit time; the user themselves doesn't carry a mode identity).
  // For assistant/tool turns we reach for --color-mode-{mode}; falls
  // back to --color-mode-system when turn.mode is absent (legacy
  // sessions pre-Phase-3).
  const ruleColor = (): string => {
    if (props.turn.role === "user") return "var(--color-text-subtle, var(--color-text-tertiary))";
    const m = props.turn.mode;
    return m
      ? `var(--color-mode-${m}, var(--color-mode-system))`
      : "var(--color-mode-system)";
  };

  return (
    <div
      class={[
        // Cycle UI v2.6 (Karar I) — bubble nefes: gap 3→4, py 4→5
        // (16→20 px vertical), keeps text content unchanged.  Reads
        // softer at 1080p without scrollbar penalty.
        "group relative flex gap-4 px-5 py-5",
        isUser() ? "bg-bg-canvas" : "bg-bg-elevated-v25",
      ].join(" ")}
      data-role={props.turn.role}
      data-amor-turn-mode={props.turn.mode}
    >
      {/* Cycle UI v2.5 — 3 px sol mode-accent rule.  Identifies the
          turn's mode without a always-visible pill (the pill moves to
          hover-reveal below).  Absolute-positioned so it doesn't push
          the avatar; rounded-full so it reads as a soft accent rather
          than a hard divider. */}
      <span
        aria-hidden="true"
        class="pointer-events-none absolute left-0 top-3 bottom-3 w-[3px] rounded-full"
        style={{ "background-color": ruleColor() }}
        data-amor-bubble-rule=""
      />
      <Avatar
        variant={ROLE_VARIANT[props.turn.role]}
        initials={ROLE_INITIALS[props.turn.role]}
        size={24}
      />
      <div class="min-w-0 flex-1">
        <div class="flex items-baseline gap-2">
          <span class="text-xs font-semibold tracking-tight">
            {ROLE_LABEL[props.turn.role]}
          </span>
          <Show when={props.turn.mode}>
            {(mode) => {
              const tooltip = (): string => {
                const m = props.turn.classifierMeta;
                if (!m) return `mode: ${mode()}`;
                const flag = m.low_confidence ? " (low confidence)" : "";
                return (
                  `mode: ${mode()}${flag}\n` +
                  `top1: ${m.top1} ${m.top1_score.toFixed(3)}\n` +
                  `top2: ${m.top2} ${m.top2_score.toFixed(3)}`
                );
              };
              // Cycle UI v2.5 — hover-reveal: the pill was always-
              // visible in Phase 3.  Now the 3 px sol rule carries the
              // mode-identity affordance; the pill surfaces classifier
              // confidence on hover for debug visibility.
              return (
                <span
                  class="amor-hover-reveal inline-flex items-center rounded px-1.5 py-px text-[0.6rem] font-medium uppercase tracking-wide"
                  style={{
                    "background-color": `var(--color-mode-${mode()}, var(--bg-elevated))`,
                    color: "var(--color-mode-fg, var(--text-secondary))",
                  }}
                  title={tooltip()}
                  data-amor-turn-mode={mode()}
                  data-amor-turn-low-confidence={
                    props.turn.classifierMeta?.low_confidence ? "1" : "0"
                  }
                >
                  {mode()}
                </span>
              );
            }}
          </Show>
          <Show when={props.turn.tag}>
            <span class="text-[0.65rem] text-text-subtle">
              {props.turn.tag}
            </span>
          </Show>
          {/* Cycle UI v2.5 — keep the "streaming…" header label too:
              it tells the user what's happening BEFORE the first token
              arrives; the in-content DOM-node caret then takes over. */}
          <Show when={props.turn.streaming}>
            <span class="text-[0.65rem] text-text-subtle motion-safe:animate-pulse">
              streaming…
            </span>
          </Show>
          <Show
            when={
              props.turn.remembered && props.turn.remembered.count > 0
                ? props.turn.remembered
                : null
            }
          >
            {(remembered) => (
              <span
                class="inline-flex items-center gap-1 rounded-full border border-border-subtle bg-bg-elevated px-1.5 py-px text-[0.6rem] text-text-body"
                title={
                  remembered().snippets && remembered().snippets!.length > 0
                    ? `${t("message.remembered.short", { count: remembered().count })}: ${remembered().snippets!.slice(0, 3).join(" · ")}`
                    : t("message.remembered.short", { count: remembered().count })
                }
                data-amor-remembered=""
                aria-label={t("message.remembered.aria", { count: remembered().count })}
              >
                <span aria-hidden="true">●</span>
                {t("message.remembered.short", { count: remembered().count })}
              </span>
            )}
          </Show>
        </div>
        {/* Cycle UI v2.5 — wrap content + streaming caret in a single
            container.  The caret is a real DOM node (not ::after) so
            SSE delta-append re-renders don't trigger pseudo-element
            repaint quirks on Safari (Research J.3). */}
        <div
          class={[
            "mt-1 text-sm leading-relaxed",
            "prose-amor", // styled in global.css; safe even if absent
          ].join(" ")}
        >
          <span
            // Sanitised -- see ../../lib/sanitise.ts.  Plain user
            // text skips marked entirely to avoid surprising
            // formatting.
            innerHTML={
              isUser()
                ? escapeText(props.turn.content)
                : renderMarkdown(props.turn.content)
            }
          />
          <Show when={props.turn.streaming && !isUser()}>
            <span class="streaming-caret" aria-hidden="true" />
          </Show>
        </div>
        <Show when={showActions() && !props.turn.streaming}>
          <MessageActions
            turn={props.turn}
            onEdit={props.onEdit}
            onRegenerate={props.onRegenerate}
            onBranch={props.onBranch}
            onRate={props.onRate}
          />
        </Show>
      </div>
    </div>
  );
};
