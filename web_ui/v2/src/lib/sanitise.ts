/**
 * Markdown + HTML sanitisation pipeline.  The chat thread renders
 * model output as HTML for code blocks + lists + emphasis; every
 * untrusted source must pass through here so we never trip the
 * v1-era ``innerHTML`` XSS regression (pain point #1).
 */

import DOMPurify, { type Config as DOMPurifyConfig } from "dompurify";
import { marked } from "marked";

marked.setOptions({
  breaks: true, // newline → <br>
  gfm: true,
});

const SANITISE_CONFIG: DOMPurifyConfig = {
  ALLOWED_TAGS: [
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
  ],
  ALLOWED_ATTR: ["href", "target", "rel", "class"],
  ALLOW_UNKNOWN_PROTOCOLS: false,
};

/** Render markdown → sanitised HTML.  Always safe to dangerouslySet
 *  the result via Solid's ``innerHTML`` JSX prop. */
export function renderMarkdown(input: string): string {
  if (!input) return "";
  // marked.parse can return Promise<string> in async mode; we use
  // sync mode by passing async: false (default) so the cast is safe.
  const html = marked.parse(input, { async: false }) as string;
  return DOMPurify.sanitize(html, SANITISE_CONFIG) as unknown as string;
}

/** Render plain text (no markdown) → safe HTML preserving newlines.
 *  Use for system messages where markdown would be a footgun. */
export function escapeText(input: string): string {
  return DOMPurify.sanitize(
    input.replace(/\n/g, "<br/>"),
    { ALLOWED_TAGS: ["br"], ALLOWED_ATTR: [] },
  ) as unknown as string;
}
