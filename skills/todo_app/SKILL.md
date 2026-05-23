---
name: todo_app
description: Build a todo list app with add, complete, delete, and localStorage persistence
when_to_use:
  - User asks for a "todo app" or "todo list"
  - User wants a single-page CRUD app over a list of items
  - User wants browser-based task tracking
languages:
  - html
  - javascript
  - css
must_have_features:
  - Add new todos via input + button + Enter key
  - Toggle complete (strike-through + checkbox)
  - Delete with explicit confirmation (no accidental loss)
  - Persist to localStorage (survives F5)
  - Filter: all / active / completed
  - Item count footer ("3 items left")
---

# todo_app — ground rules

Single-file HTML with inline `<style>` and `<script>`. Persists state
in `localStorage` under key `amor.todo.items.v1`.

## State shape

```js
[
  { id: <string-uuid>, text: <string>, done: <boolean>, created_at: <ISO-timestamp> },
  ...
]
```

Always read from + write to localStorage in the same transaction
(read → mutate → write back the full array). Don't try to write
partial diffs; the array is small enough that the whole-rewrite
cost is irrelevant.

## UI contract

* Top-of-page input + "Add" button. Enter key in the input also
  adds. Empty / whitespace-only input is rejected silently.
* List of todos below. Each row: checkbox + text + delete (×).
* Click the checkbox or the text to toggle complete. Strike-through
  + grey color for completed.
* Click × → confirm dialog ("Delete this todo?") → remove from list.
* Filter tabs above the list: All / Active / Completed.
* Footer: count of active items + "Clear completed" button.

## Accessibility

* Each row has `role="listitem"`.
* Checkbox + delete button have visible focus ring (no
  `outline: none`).
* Color is NEVER the only signifier for completed — also strike +
  greyed text.

## Persistence + migration

* Schema-versioned key (`amor.todo.items.v1`). If a future version
  changes the shape, the loader reads v1, migrates, writes v2,
  removes v1.

## Anti-patterns

* DON'T use external libraries (no React / Vue / Alpine).
* DON'T destroy todos on filter change — they're filtered in
  render, not removed from state.
* DON'T let the user submit an empty todo.
