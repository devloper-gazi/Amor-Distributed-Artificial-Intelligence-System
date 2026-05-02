import {
  type Component,
  createSignal,
  onMount,
  Show,
} from "solid-js";

interface DiffBlockProps {
  /** Unified-diff string OR a SEARCH/REPLACE block.  We auto-detect
   *  and convert SEARCH/REPLACE → unified-diff for diff2html. */
  diff: string;
  /** Filename hint for the diff header. */
  filename?: string;
  /** Side-by-side ≥ 768 px, line-by-line below.  Defaults to
   *  side-by-side; the consumer can force one mode if needed. */
  format?: "side-by-side" | "line-by-line";
}

/**
 * Lazy-loaded diff viewer.  diff2html + its CSS only ship in the
 * Build mode chunk so chat-mode entry stays tight.  Falls back to a
 * monospace ``<pre>`` if the dynamic import fails.
 *
 * SEARCH/REPLACE blocks (Aider / Cline / OpenHands convention used
 * by AMOR's diff-mode debugger) get translated into a unified diff
 * before rendering — diff2html doesn't natively understand the
 * ``<<<<<<< SEARCH / ======= / >>>>>>> REPLACE`` shape.
 */
export const DiffBlock: Component<DiffBlockProps> = (props) => {
  const [html, setHtml] = createSignal<string | null>(null);
  const [error, setError] = createSignal<string | null>(null);

  onMount(async () => {
    try {
      // Dynamic import keeps diff2html out of the chat-mode bundle.
      const [{ html: render }] = await Promise.all([
        import("diff2html"),
        import("diff2html/bundles/css/diff2html.min.css"),
      ]);
      const unified = toUnifiedDiff(props.diff, props.filename ?? "patch");
      const out = render(unified, {
        outputFormat: props.format ?? "side-by-side",
        drawFileList: false,
        matching: "lines",
      });
      setHtml(out);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  });

  return (
    <div class="rounded-md border border-border-subtle overflow-hidden">
      <Show
        when={html()}
        fallback={
          <Show
            when={!error()}
            fallback={
              <pre class="overflow-x-auto bg-bg-tertiary p-3 text-xs">
                {props.diff}
              </pre>
            }
          >
            <p class="px-3 py-2 text-xs text-text-tertiary">
              Loading diff viewer…
            </p>
          </Show>
        }
      >
        <div class="amor-diff" innerHTML={html() ?? ""} />
      </Show>
    </div>
  );
};

/**
 * Translate a SEARCH/REPLACE block (or multiple) into a single
 * unified-diff string that diff2html can render.  Plain unified
 * diffs pass through unchanged.
 */
export function toUnifiedDiff(input: string, filename: string): string {
  // Already unified diff?
  if (
    input.includes("@@") ||
    input.startsWith("---") ||
    input.startsWith("+++ ")
  ) {
    return input;
  }

  const blocks: Array<{ search: string; replace: string }> = [];
  const re =
    /<{7}\s*SEARCH\s*\n([\s\S]*?)\n={7}\s*\n([\s\S]*?)\n>{7}\s*REPLACE/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(input)) !== null) {
    blocks.push({ search: m[1] ?? "", replace: m[2] ?? "" });
  }

  if (blocks.length === 0) {
    // Best-effort fallback — render input as a single "after" hunk.
    const lines = input.split("\n");
    const header = `--- a/${filename}\n+++ b/${filename}\n@@ -1,1 +1,${lines.length} @@`;
    return `${header}\n-\n${lines.map((l) => "+" + l).join("\n")}`;
  }

  const out: string[] = [
    `--- a/${filename}`,
    `+++ b/${filename}`,
  ];
  let oldStart = 1;
  let newStart = 1;
  for (const { search, replace } of blocks) {
    const sLines = search.split("\n");
    const rLines = replace.split("\n");
    out.push(
      `@@ -${oldStart},${sLines.length} +${newStart},${rLines.length} @@`,
    );
    for (const l of sLines) out.push(`-${l}`);
    for (const l of rLines) out.push(`+${l}`);
    oldStart += sLines.length;
    newStart += rLines.length;
  }
  return out.join("\n");
}
