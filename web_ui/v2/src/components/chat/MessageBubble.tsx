import { type Component, Show } from "solid-js";
import type { ChatTurn } from "../../lib/types";
import { renderMarkdown, escapeText } from "../../lib/sanitise";
import { Avatar } from "../ui";

interface MessageBubbleProps {
  turn: ChatTurn;
}

const ROLE_LABEL: Record<ChatTurn["role"], string> = {
  user: "You",
  assistant: "AMOR",
  tool: "Tool",
  system: "System",
};

const ROLE_VARIANT: Record<ChatTurn["role"], "user" | "system" | "model"> = {
  user: "user",
  assistant: "model",
  tool: "system",
  system: "system",
};

const ROLE_INITIALS: Record<ChatTurn["role"], string> = {
  user: "yo",
  assistant: "AM",
  tool: "TL",
  system: "SY",
};

/**
 * One conversation turn.  User turns render as plain escaped text;
 * assistant + tool turns go through marked + DOMPurify so code
 * fences + lists + emphasis render safely.
 */
export const MessageBubble: Component<MessageBubbleProps> = (props) => {
  const isUser = () => props.turn.role === "user";

  return (
    <div
      class={[
        "flex gap-3 px-5 py-4",
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
      </div>
    </div>
  );
};
