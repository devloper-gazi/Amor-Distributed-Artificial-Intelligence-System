import { type Component, For, Show, createSignal, lazy } from "solid-js";
import {
  Avatar,
  Badge,
  Button,
  Divider,
  IconButton,
  Input,
  Kbd,
  ProgressBar,
  Spinner,
  StatusPill,
  Textarea,
  Tooltip,
  type Status,
} from "../components/ui";

// Lazy-loaded so diff2html ships in its own chunk (ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â°Ãƒâ€¹Ã¢â‚¬Â  90 KB gz)
// only when the showcase or Build mode renders a diff.
const DiffBlock = lazy(() =>
  import("../components/chat/DiffBlock").then((m) => ({
    default: m.DiffBlock,
  })),
);

const DEMO_DIFF = `\
<<<<<<< SEARCH
def fib(n):
    if n < 2:
        return n
    return fib(n-1) + fib(n-2)
=======
def fib(n: int) -> int:
    if n < 2:
        return n
    a, b = 0, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return b
>>>>>>> REPLACE`;

/**
 * Component showcase ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â Storybook-lite preview surface.  Used to
 * verify atom rendering + theme tokens + per-mode accent shifts
 * without spinning up the real chat shell.  Lives at /showcase.
 *
 * Each section corresponds to one atom in the design doc ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§6 inventory.
 * When PR-3+ adds molecules and organisms, those get their own
 * sections.
 */
const MODES: ReadonlyArray<{
  key: string;
  label: string;
  glyph: string;
  subtitle: string;
}> = [
  { key: "research", label: "Research", glyph: "compass", subtitle: "gather, summarise, cite" },
  { key: "thinking", label: "Thinking", glyph: "brain", subtitle: "multi-step reasoning" },
  { key: "build", label: "Build", glyph: "hammer", subtitle: "code, test, debug" },
  { key: "consortium", label: "Consortium", glyph: "users-round", subtitle: "research + think + build" },
  { key: "sentinel", label: "Sentinel", glyph: "shield-half", subtitle: "governance, ledger" },
  { key: "system", label: "System", glyph: "activity", subtitle: "diagnostics, memory" },
];

const STATUSES: ReadonlyArray<Status> = ["healthy", "warming", "warning", "failed"];

export const Showcase: Component = () => {
  const [progress, setProgress] = createSignal(38);
  const [textValue, setTextValue] = createSignal(
    "Type something to see the autoresize behaviourÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦",
  );

  const toggleTheme = () => {
    const html = document.documentElement;
    const cur = html.getAttribute("data-theme") ?? "system";
    const next = cur === "dark" ? "light" : cur === "light" ? "system" : "dark";
    html.setAttribute("data-theme", next);
  };

  return (
    <main
      data-mode="system"
      class="min-h-screen bg-bg-canvas p-8 text-text-display"
    >
      <header class="mx-auto mb-8 flex max-w-5xl items-center justify-between border-b border-border-subtle pb-4">
        <div>
          <h1 class="text-2xl font-semibold tracking-tight">Component Showcase</h1>
          <p class="mt-1 text-sm text-text-body">
            12 atoms &middot; Tailwind v4 @theme &middot; per-mode accents
          </p>
        </div>
        <div class="flex items-center gap-2 text-sm">
          <a href="/v2" class="text-text-body hover:text-text-display">
            &larr; Back
          </a>
          <Button variant="secondary" size="sm" onClick={toggleTheme}>
            Toggle theme
          </Button>
        </div>
      </header>

      <div class="mx-auto max-w-5xl space-y-12">
        {/* Mode accents */}
        <Section
          title="Mode accents"
          subtitle="One CSS variable shifts per mode.  Chrome stays monochrome; only the focus ring, timeline pill, and header rule pull from --mode-accent."
        >
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3">
            <For each={MODES}>
              {(mode) => (
                <div
                  data-mode={mode.key}
                  class="rounded-lg border border-border-subtle bg-bg-elevated p-4"
                >
                  <div class="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      class="h-3 w-3 rounded-full"
                      style={{ background: "var(--mode-accent)" }}
                    />
                    <span class="font-medium">{mode.label}</span>
                  </div>
                  <p class="mt-1 text-xs text-text-subtle">{mode.subtitle}</p>
                  <p class="mt-2 font-mono text-xs text-text-body">
                    icon: {mode.glyph}
                  </p>
                </div>
              )}
            </For>
          </div>
        </Section>

        {/* Buttons */}
        <Section title="Button" subtitle="primary / secondary / ghost / danger ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â sm / md">
          <div class="space-y-3">
            <div class="flex flex-wrap items-center gap-3">
              <Button>Primary</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="danger">Danger</Button>
            </div>
            <div class="flex flex-wrap items-center gap-3">
              <Button size="sm">Small primary</Button>
              <Button size="sm" variant="secondary">
                Small secondary
              </Button>
              <Button loading>Loading</Button>
              <Button disabled>Disabled</Button>
            </div>
          </div>
        </Section>

        {/* IconButton */}
        <Section title="IconButton" subtitle="aria-label required at type level">
          <div class="flex items-center gap-2">
            <IconButton aria-label="Send" size="sm">
              <span aria-hidden="true">ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢</span>
            </IconButton>
            <IconButton aria-label="Open menu">
              <span aria-hidden="true">ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â°Ãƒâ€šÃ‚Â¡</span>
            </IconButton>
            <IconButton aria-label="Close" disabled>
              <span aria-hidden="true">ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â</span>
            </IconButton>
          </div>
        </Section>

        {/* Input */}
        <Section title="Input" subtitle="prefix / suffix slots + invalid state">
          <div class="grid max-w-md grid-cols-1 gap-3">
            <Input placeholder="Plain input" />
            <Input
              placeholder="With prefix"
              prefix={<span aria-hidden="true">@</span>}
            />
            <Input
              placeholder="Invalid"
              invalid
              suffix={<span aria-hidden="true">!</span>}
            />
            <Input placeholder="Disabled" disabled value="readonly text" />
          </div>
        </Section>

        {/* Textarea */}
        <Section
          title="Textarea"
          subtitle="autoresize ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â uses field-sizing where supported, JS fallback elsewhere"
        >
          <div class="max-w-md">
            <Textarea
              value={textValue()}
              onInput={(e) => setTextValue(e.currentTarget.value)}
              minRows={2}
              maxRows={8}
              placeholder="Type to growÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦"
            />
            <p class="mt-2 text-xs text-text-subtle">
              {textValue().length} chars
            </p>
          </div>
        </Section>

        {/* Badge */}
        <Section title="Badge" subtitle="neutral and accent variants">
          <div class="flex flex-wrap items-center gap-3">
            <Badge>Neutral</Badge>
            <Badge size="md">Neutral md</Badge>
            <Badge variant="accent">Accent</Badge>
            <Badge variant="accent" size="md">
              99+
            </Badge>
          </div>
        </Section>

        {/* StatusPill */}
        <Section title="StatusPill" subtitle="4 states; warming pulses">
          <div class="flex flex-wrap items-center gap-3">
            <For each={STATUSES}>
              {(s) => <StatusPill status={s} />}
            </For>
          </div>
          <div class="mt-3 flex flex-wrap items-center gap-3">
            <StatusPill status="healthy" size="md" label="API: 8000" />
            <StatusPill status="warming" size="md" label="Sandbox warming" />
            <StatusPill status="failed" size="md" label="Backend down" />
          </div>
        </Section>

        {/* Spinner */}
        <Section
          title="Spinner"
          subtitle="motion-safe spins; motion-reduce shows static dot"
        >
          <div class="flex items-center gap-6 text-text-display">
            <Spinner size={16} />
            <Spinner size={20} />
            <Spinner size={24} />
            <span class="text-sm text-text-subtle">LoadingÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦</span>
          </div>
        </Section>

        {/* ProgressBar */}
        <Section title="ProgressBar" subtitle="determinate + indeterminate">
          <div class="space-y-4">
            <ProgressBar value={progress()} label={`${progress()}% complete`} />
            <div class="flex items-center gap-3">
              <Button
                size="sm"
                variant="secondary"
                onClick={() => setProgress((p) => Math.max(0, p - 10))}
              >
                -10
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => setProgress((p) => Math.min(100, p + 10))}
              >
                +10
              </Button>
              <span class="text-sm text-text-subtle">value: {progress()}</span>
            </div>
            <ProgressBar value={null} label="indeterminate" />
          </div>
        </Section>

        {/* Kbd */}
        <Section title="Kbd" subtitle="auto-substitutes Mod for ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬â„¢Ãƒâ€¹Ã…â€œ / Ctrl per platform">
          <div class="flex flex-wrap items-center gap-3 text-sm text-text-body">
            <span class="inline-flex items-center gap-1.5">
              Open palette <Kbd>Mod+K</Kbd>
            </span>
            <span class="inline-flex items-center gap-1.5">
              Send <Kbd>Mod+Enter</Kbd>
            </span>
            <span class="inline-flex items-center gap-1.5">
              Close <Kbd>Esc</Kbd>
            </span>
            <span class="inline-flex items-center gap-1.5">
              Cycle <Kbd>Shift+Tab</Kbd>
            </span>
          </div>
        </Section>

        {/* Avatar */}
        <Section title="Avatar" subtitle="initials fallback + 3 variants ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â 2 sizes">
          <div class="flex items-center gap-4">
            <Avatar variant="user" initials="ad" />
            <Avatar variant="system" initials="sy" />
            <Avatar variant="model" initials="qw" />
            <Avatar variant="user" initials="ad" size={24} />
            <Avatar variant="system" initials="sy" size={24} />
          </div>
        </Section>

        {/* Tooltip */}
        <Section title="Tooltip" subtitle="hover or keyboard focus to open; 200 ms delay">
          <div class="flex items-center gap-4">
            <Tooltip label="Top placement (default)">
              <Button variant="secondary" size="sm">
                Hover top
              </Button>
            </Tooltip>
            <Tooltip label="Bottom placement" placement="bottom">
              <Button variant="secondary" size="sm">
                Hover bottom
              </Button>
            </Tooltip>
            <Tooltip label="Right placement" placement="right">
              <IconButton aria-label="Right tooltip">
                <span aria-hidden="true">i</span>
              </IconButton>
            </Tooltip>
          </div>
        </Section>

        {/* Divider */}
        <Section title="Divider" subtitle="horizontal + vertical">
          <div class="space-y-3">
            <Divider />
            <div class="flex h-12 items-center gap-3">
              <span class="text-sm">Left</span>
              <Divider orientation="vertical" />
              <span class="text-sm">Right</span>
            </div>
          </div>
        </Section>

        {/* DiffBlock ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â lazy-loaded diff2html viewer */}
        <Section
          title="DiffBlock"
          subtitle="lazy-loaded diff2html ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â accepts unified diffs OR SEARCH/REPLACE blocks"
        >
          <DiffDemo />
        </Section>
      </div>
    </main>
  );
};

const DiffDemo: Component = () => {
  const [show, setShow] = createSignal(false);
  return (
    <div>
      <Show
        when={show()}
        fallback={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setShow(true)}
          >
            Render demo diff
          </Button>
        }
      >
        <DiffBlock diff={DEMO_DIFF} filename="fib.py" format="line-by-line" />
      </Show>
    </div>
  );
};

const Section: Component<{
  title: string;
  subtitle?: string;
  children: import("solid-js").JSX.Element;
}> = (props) => (
  <section>
    <h2 class="mb-1 text-lg font-medium">{props.title}</h2>
    {props.subtitle ? (
      <p class="mb-4 text-sm text-text-body">{props.subtitle}</p>
    ) : null}
    {props.children}
  </section>
);
