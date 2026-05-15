---
name: snake_game_builder
description: Build a single-file HTML snake game with arrow-key controls, score, and replay
when_to_use:
  - User asks for "snake game"
  - User wants a browser-playable arcade game with simple controls
  - User mentions snake, classic arcade game, or grid-based movement
languages:
  - html
  - javascript
  - css
must_have_features:
  - HTML5 canvas (single file, no external assets)
  - 20x20 grid with arrow-key controls (WASD also accepted)
  - Score counter visible at all times
  - Game-over screen with replay button
  - Collision detection (self + wall)
  - Pause-on-blur / resume-on-focus
---

# snake_game_builder — ground rules

Produce a single `index.html` containing inline `<style>` and `<script>` so
the user can save once and open in a browser. No CDN imports, no
external assets.

## Game loop contract

* 20 × 20 grid; canvas size = 400 × 400 (cell = 20 px).
* Snake starts length 3, centered, moving right.
* Food spawns at a random empty cell after each eat.
* Tick: `setInterval(update, 100)` — 10 FPS is the canonical snake feel.
* Speed bump: every 5 food eaten, decrease interval by 8 ms (floor 40 ms).

## Controls

* Arrow keys + WASD both bound. Opposite-direction key presses are
  rejected (no instant 180° turns into self).
* `R` restarts after game over.
* `Space` pauses / resumes (and the canvas dims when paused).

## Rendering

* Snake head: distinct color from body (operator-tweakable via CSS
  custom-props at `:root`).
* Food: a contrasting color, 1-cell square.
* HUD: score top-left in `font-family: monospace`, big-and-readable.

## Game over

* Detect: snake head leaves the grid OR enters a cell already
  occupied by the snake body.
* Show a modal overlay (semi-transparent backdrop + centered card):
  - "Game Over" title.
  - Final score.
  - "Play Again" button (also bound to `R`).
* Reset the snake / food / score on replay (do not reload the page).

## Polish (must-have, not nice-to-have)

* Use `requestAnimationFrame` ONLY for drawing; logic stays on the
  `setInterval` clock for deterministic timing.
* Keyboard handler uses `keydown`, not `keypress`.
* `preventDefault()` on the bound arrow keys so the page doesn't
  scroll under the game.
* The canvas has `tabindex="0"` and grabs focus on click so a
  second player can resume without re-clicking.

## Anti-patterns

* DON'T use external JavaScript files or CDN scripts.
* DON'T use `prompt()` or `alert()` — game-over UI is in-canvas / DOM.
* DON'T allow the snake to wrap around the grid (it must die on
  wall collision).
* DON'T forget the favicon (`<link rel="icon" href="data:,">` is
  acceptable to silence the browser warning).
