---
name: game-restart
description: Restart a running browser game on agentnumberone.io (or any /play page) when the agent is stuck or dead — pause menu, New World, Play New World, back into the game. Runs an ordered input sequence over CDP or at OS level, with a screenshot after every step.
---

# Game Restart

A **sequence replayer**, not a policy. Fixed input order, screenshot verification, never tries to play.

## Two transports, same sequence

| backend | needs | precision | use when |
|---|---|---|---|
| `cdp` (default) | Chrome started with `--remote-debugging-port=9222` | DOM-exact (finds controls by text) | you can relaunch the browser |
| `os` | Accessibility + Screen Recording permission, `swiftc` once | pixel coords (window-relative) | no debug port, or a canvas game |

`os` needs its helper compiled once:

```sh
swiftc -O scripts/os-helper.swift -o scripts/os-helper
```

## Default sequence

| # | action | why |
|---|---|---|
| 1 | `Cmd+Esc` | opens the in-game pause menu; the operator modifier is what marks this as human input rather than agent input |
| 2 | click **New World** | title-screen entry point |
| 3 | click **Play New World** | confirms and starts generation |
| 4 | `Esc` (no modifier) | dismisses the overlay, hands control back |

```sh
node scripts/restart.mjs --backend cdp --port 9222
node scripts/restart.mjs --backend os  --window "Chrome" --steps my-game.json
```

```
{ "backend":"cdp", "ok":true,
  "results":[{"step":1,"action":"Cmd+Escape","ok":true,"shot":"…/1-pause-menu.png"}, …] }
```

Exit `0` all verified · `2` target not found (no port / no tab / no window) · `3` a step failed. **The sequence stops at the first failure** — it never clicks blind on an unknown screen.

## Sequence file

```json
[
  {"type":"key","key":"Escape","operator":true,"settle":900,"name":"1-pause-menu"},
  {"type":"click","match":"new world","operator":true,"settle":1800,"name":"2-new-world"},
  {"type":"click","match":"play new world|play selected|play","operator":true,"settle":3500,"name":"3-play"},
  {"type":"key","key":"Escape","settle":800,"name":"4-back-in-game"},
  {"type":"wait","ms":1500,"name":"5-settle"}
]
```

Types: `key` (`key`, `operator`), `click` (`match` regex **or** `x`/`y`), `wait` (`ms`), `clear-chat` (CDP only).

`x`/`y` are **window-relative** on the `os` backend and viewport-relative on `cdp`. The `os` backend *requires* them — it cannot see the DOM, so read the target off the previous screenshot.

## When a step fails

| symptom | cause | fix |
|---|---|---|
| `no DevTools endpoint` | Chrome launched without the flag | relaunch with `--remote-debugging-port=9222` |
| `no window matching` | wrong `--window` substring | run `./scripts/os-helper windows` and pick from the list |
| `control not found` (cdp) | canvas-drawn menu | use `x`/`y` from `1-pause-menu.png` |
| `needs coordinates` (os) | step has `match` only | add `x`/`y` |
| step 1 closes the menu instead of opening it | `Esc` toggles and the menu was already open | re-run from step 2, or add a guard step |

## Rules

- **Never run while the world is healthy** — this destroys progress.
- **Release the agent's held inputs first.** A key held across the transition walks the new world into the same wall.
- **`Esc` toggles.** Screenshots are how you catch an overshoot.
- **`--clear-chat` is off by default** and CDP-only; a fresh world plus a reconnected agent normally drops the old goal.
- **The OS backend can't grant its own permissions.** Accessibility and Screen Recording are clicked by a human once.
