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

  return (
    <div
      class={[
        "group flex gap-3 px-5 py-4",
        isUser() ? "bg-bg-primary" : "bg-bg-secondary",
      ].join(" ")}
      data-role={props.turn.role}
    >
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
          <Show when={props.turn.tag}>
            <span class="text-[0.65rem] text-text-tertiary">
              {props.turn.tag}
            </span>
          </Show>
          <Show when={props.turn.streaming}>
            <span class="text-[0.65rem] text-text-tertiary motion-safe:animate-pulse">
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
                class="inline-flex items-center gap-1 rounded-full border border-border-subtle bg-bg-elevated px-1.5 py-px text-[0.6rem] text-text-secondary"
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
        <div
          class={[
            "mt-1 text-sm leading-relaxed",
            "prose-amor", // styled in global.css; safe even if absent
          ].join(" ")}
          // Sanitised — see ../../lib/sanitise.ts.  Plain user text
          // skips marked entirely to avoid surprising formatting.
          innerHTML={
            isUser()
              ? escapeText(props.turn.content)
              : renderMarkdown(props.turn.content)
          }
        />
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
