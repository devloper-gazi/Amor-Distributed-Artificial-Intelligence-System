---
name: landing_page
description: Build a responsive landing page with hero, features grid, CTA, and footer
when_to_use:
  - User asks for a "landing page" or "marketing page"
  - User wants a single-page promotional site for a product/service
  - User mentions hero section, features grid, or call-to-action
languages:
  - html
  - css
must_have_features:
  - Hero section with H1 headline, sub-headline, primary CTA button
  - 3-column features grid (responsive collapsing to 1 column on mobile)
  - Secondary CTA with secondary styling
  - Footer with copyright, social links, legal links
  - Mobile-first responsive (375 px viewport works)
  - Semantic HTML5 (header, main, section, footer landmarks)
---

# landing_page — ground rules

Single-file HTML with inline `<style>`. No JavaScript required for
the landing page itself — link clicks scroll smoothly via
`scroll-behavior: smooth` on `html`.

## Layout (mobile-first)

1. **Header**: brand name on the left, nav links on the right
   (Features / Pricing / Sign Up).  On mobile (≤ 640 px), nav
   collapses into a hamburger placeholder (the link list is still
   accessible; just hidden behind a checkbox-toggled disclosure
   to avoid JS).
2. **Hero**: full-width, centered.  H1 (≥ 2.5 rem), sub-H2 paragraph
   (≤ 60 chars per line at desktop width), primary CTA button.
3. **Features**: 3-card grid (CSS Grid `grid-template-columns:
   repeat(auto-fit, minmax(280px, 1fr))`).  Each card has an icon
   (SVG or emoji), headline, 1-2 line description.
4. **Secondary CTA**: full-width band with contrasting background
   and a button.
5. **Footer**: copyright, social icon row, two columns of legal
   links (privacy, terms).

## Visual design

* Use CSS custom properties at `:root` for accent color, text
  color, surface color so the operator can re-skin without touching
  rules.
* Default palette is conservative — dark text on light backgrounds,
  one accent color used sparingly.
* Generous whitespace: section padding ≥ 4rem vertical at desktop,
  ≥ 2rem at mobile.

## Accessibility

* Skip-to-content link at the very top (`<a href="#main">`).
* Every interactive element has visible focus ring.
* Color contrast ≥ 4.5:1 for body text, ≥ 3:1 for large text.
* Heading hierarchy is strict (H1 → H2 → H3, no skips).
* Buttons are `<button>` or `<a>` — never `<div>` with onclick.

## Mobile

* Test viewport: 375 × 812 (iPhone reference).  Verify no horizontal
  scroll, all CTA buttons reachable with thumb.
* Touch targets ≥ 44 × 44 px.

## Anti-patterns

* DON'T use background images that don't have a CSS fallback.
* DON'T inline 5+ MB of base64 imagery — keep it lean.
* DON'T include the actual signup form here (that's a separate
  skill).  CTAs link out / scroll to a placeholder `#signup`.
