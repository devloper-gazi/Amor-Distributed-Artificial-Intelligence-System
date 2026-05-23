---
name: dashboard
description: Build a metrics dashboard with stat cards, a chart, and a recent-events table
when_to_use:
  - User asks for a "dashboard" or "admin panel"
  - User wants stat tiles + time-series chart + tabular data
  - User mentions analytics page or monitoring view
languages:
  - html
  - javascript
  - css
must_have_features:
  - 4 stat cards (total, success rate, latency, error count) with sparklines
  - Time-series chart (last 24h or 30d toggle) rendered with Canvas
  - Recent-events table with sortable columns + status badges
  - Mock data source baked in (no fetch dependency)
  - Refresh button + auto-refresh toggle
  - Mobile responsive (cards stack, chart scrolls)
---

# dashboard — ground rules

Single-file HTML.  Mock data lives in a top-of-file `const SAMPLES =
[...]` so the dashboard renders meaningfully on first load without
a backend.

## Layout

1. **Top bar**: title, last-refreshed timestamp, refresh button,
   auto-refresh toggle (checkbox label "Auto" — when on, polls
   every 30 s).
2. **Stat-card row** (4 cards): label, big number, delta vs
   previous period (green/red arrow), inline sparkline (canvas
   ~80×24 px).
3. **Time-series chart**: full-width canvas (~300 px tall), with
   range toggle buttons (24h / 7d / 30d).  No external chart
   library — draw with `CanvasRenderingContext2D`.
4. **Recent events table**: last 50 events, columns: timestamp,
   actor, action, status (badge: ok / warn / err).  Click column
   header to sort; click again to reverse.

## Chart rendering

* Pure Canvas: axes, gridlines, line series, hover tooltip via
  pointer events.
* Recompute on viewport resize via `ResizeObserver`.
* Skip / coarsen points if the range has > 200 samples to keep
  60 fps.

## Data shape

```js
const SAMPLES = {
  stats: {
    total: { value: 12483, delta: +124, sparkline: [...] },
    success_rate: { value: 0.978, delta: -0.003, sparkline: [...] },
    latency_p95_ms: { value: 142, delta: -8, sparkline: [...] },
    errors: { value: 27, delta: +3, sparkline: [...] },
  },
  series: [{ t: <unix-ms>, value: <number> }, ...],
  events: [{ t: ISO, actor: "...", action: "...", status: "ok" }, ...],
};
```

## Accessibility

* All canvas charts have a `<table>` fallback below (visually
  hidden via CSS but readable by screen readers).
* Status badges use icon + color, not color alone.
* Sortable headers have `aria-sort="ascending"|"descending"|"none"`.

## Anti-patterns

* DON'T pull in Chart.js / D3 / ApexCharts.
* DON'T fetch from an external API — bake samples in.
* DON'T forget the table fallback for the canvas chart.
