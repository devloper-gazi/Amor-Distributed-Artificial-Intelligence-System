---
name: blog_post
description: Build a single-post blog page with header, article body, code blocks, and dark-mode toggle
when_to_use:
  - User asks for a "blog post" or "article page"
  - User wants a readable single-post layout
  - User mentions markdown rendering, syntax highlighting, or reading time
languages:
  - html
  - css
  - javascript
must_have_features:
  - Header with title, byline, publish date, reading time
  - Article body with H2/H3 hierarchy, paragraphs, blockquotes, code blocks
  - Syntax highlighting on code blocks (use Prism.js minimal CDN OR inline highlighting)
  - Table of contents auto-generated from H2/H3
  - Dark-mode toggle persisted in localStorage
  - Responsive (max-width 720 px reading column on desktop, fluid on mobile)
---

# blog_post — ground rules

Single-file HTML. Place the prose ONLY in the article section —
the page chrome (header, TOC, toggle) is structural.

## Layout

1. **Header**: title (H1), byline + date + reading-time pill,
   subtle 1-pixel rule.
2. **Aside (TOC)**: floats right on desktop ≥ 1024 px, collapses
   to a `<details>` block at top on mobile.  Auto-generated from
   the article's H2 / H3 elements.
3. **Article**: prose column, `max-width: 720px`, line-height
   ≥ 1.6, font-size ≥ 1.05 rem.
4. **Footer**: prev / next links (placeholder), share-icon row.

## Typography

* Serif (or careful sans) for body, sans for chrome.
* Generous paragraph spacing (≥ 1em margin-top).
* `<blockquote>`: left border accent + italic.
* `<code>` inline: subtle background + monospace.
* `<pre><code>`: full-width block, dark background regardless of
  page theme, language label top-right.

## Reading-time

* Calculate as `Math.ceil(article.text-content-word-count / 200)`
  on page load.  200 wpm is the conventional adult reading speed.
* Show as "X min read" in the header pill.

## Dark mode

* Class on `<html>` (`html.dark`) flips a CSS custom-prop palette.
* Toggle button in the header (sun / moon emoji or inline SVG).
* Persist preference in `localStorage["amor.blog.theme"]` (light /
  dark / system).  Default = system → respect `prefers-color-scheme`.

## Accessibility

* Skip-to-content link.
* `<main>` wraps the article.
* Each section heading has a stable anchor `id` for the TOC.
* Color contrast ≥ 4.5:1 in BOTH themes.

## Anti-patterns

* DON'T render Markdown at runtime — the input is the user's
  authored HTML / prose.  If markdown rendering is requested,
  flag it as a separate skill.
* DON'T include the comment system (that's a separate skill).
* DON'T use Google Fonts — bundle a system-font stack.
