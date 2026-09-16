# Shellf-Driving 🚗📺

A **self-driving shell** — Playwright, but for terminal UIs, built for LLM agents.

Shellf-Driving spawns a TUI program (vim, htop, lazygit, k9s, an installer wizard…)
inside a real pseudo-terminal, emulates its ANSI output into a structured screen, and
exposes a small set of MCP tools so an LLM can *look at* the terminal and *drive* it —
typing, special keys, and **mouse** (clicks, drags, scroll) — just like a human.

## Why

Browsers have Playwright/Puppeteer; terminals didn't have a clean agent-facing
equivalent. Shellf-Driving is that layer: the "browser + page" for text UIs.

| Playwright concept | Shellf-Driving |
| --- | --- |
| Browser page | A `TerminalSession` (PTY-hosted program) |
| DOM | pyte screen buffer (grid of styled cells) |
| `page.screenshot()` | `screenshot` → PNG with cursor + mouse overlay |
| `locator.click()` | `mouse(action="click", x, y)` (SGR-1006) |
| `page.keyboard` | `type_text` / `press` |
| `expect(...).toBeVisible()` | `wait_for_text` / `find_text` |

## Architecture

```
shellf/
  terminal.py   PTY host + pyte screen emulation + key/mouse/paste input
  modes.py      DEC private-mode sniffer + terminal-query auto-responder
  keys.py       key-name → terminal escape-sequence map (DECCKM-aware)
  shortcuts.py  named prefix-chord registry (tmux, screen, emacs, vim, …)
  view.py       response shaping (auto / screen / diff / quiet / none)
  config.py     env-var configuration (SHELLF_VIEW, SHELLF_ENV, …)
  render.py     screen → PNG (colors, bold, cursor outline, mouse marker, grid)
  observe.py    live observability dashboard (SSE + xterm.js byte mirror)
  server.py     FastMCP server exposing the tools
```

- **PTY host** (`pty.fork`): the program gets a true controlling terminal, correct
  `TERM`, and `SIGWINCH` on resize, so it renders exactly as in a real terminal.
- **Emulator** (`pyte`): a background thread drains the PTY into a `HardenedScreen`
  (tolerates the private SGR sequences real apps like vim emit), under a lock.
- **Input**: high-level intents → the raw bytes a terminal sends. Mouse uses the
  modern SGR(1006) protocol, which most current TUIs understand.
- **Image preview**: renders the cell grid to a PNG with Pillow + DejaVu Sans Mono,
  drawing the block cursor and a marker where the mouse last acted — because LLMs
  read images, and color/layout often beats plain text for busy TUIs.

## MCP tools

`launch` · `snapshot` · `screenshot` · `type_text` · `press` · **`run`** · `mouse` ·
`click_text` · `shortcut` · `list_shortcuts` · `define_shortcut` ·
`wait_for_text` · `wait_for_text_gone` · `wait_for_stable` · `find_text` ·
`read_history` · `get_modes` · `resize` · `kill` · `list_sessions`

Multiple named sessions run at once, so an agent can juggle several TUIs.

### Correctness foundation (Phase 1)

What makes the agent reliable inside *real* apps, not just toy demos:

- **Mode-sniffer** (`modes.py`) watches the program's output for the DEC private modes
  pyte ignores — DECCKM, alt-screen, mouse, bracketed-paste, focus — via `get_modes`.
- **DECCKM-correct arrows**: once an app enables application cursor keys, arrows/Home/End
  auto-switch to the `ESC O x` form, so navigation works in vim/less/fzf. Plus **Alt/Meta**
  (`alt+x` → `ESC x`).
- **Query auto-responder**: answers Device-Attributes and cursor/status reports
  (`ESC[c`, `ESC[>c`, `ESC[5n`, `ESC[6n`) so programs that query the terminal don't hang.
- **Auto-wait**: every action tool captures a repaint baseline, then waits for *its own*
  change to settle (`wait_for_stable`) before returning — Playwright-style, no fixed sleeps.
- **Scrollback**: `HistoryScreen` retains output that scrolled off; `read_history` reads it.

### Correctness, continued (Phase 2)

Fixes born from long, real agent sessions driving vim, Claude Code, and other
modern TUIs through the server:

- **Bracketed paste**: multi-line `type_text` is auto-delivered as a bracketed paste
  (`ESC[200~…201~`) when the app has enabled paste mode — editors insert it verbatim
  instead of autoindent-mangling every line. Force with `paste=true/false`.
- **Kitty keyboard protocol (CSI-u)**: modern TUIs (Claude Code, neovim, fish) emit
  progressive-enhancement sequences pyte mis-parses, leaving `1;1u` garbage on the
  screen. They're now stripped — even when split across read boundaries — and the
  `CSI ? u` support query is correctly left unanswered so apps fall back to legacy keys.
- **Kill that actually kills**: interactive shells ignore SIGTERM, which used to leave
  immortal sessions blocking their name. `kill` now waits for real death, escalates to
  SIGKILL, and offers `sig: term|int|hup|quit|kill`.
- **`click_text("OK")`**: find text on screen and click its center — no coordinate math.
- **`wait_for_text_gone`**: wait for a spinner/“Loading…” to clear.
- **Token-efficient snapshots**: trailing whitespace and blank rows are trimmed from
  every returned screen (the header keeps the true cols×rows).

### Token economy (Phase 3)

Driving a TUI is a loop of *act, then look*, and the looking is what costs money. Every
action used to return the whole grid, so a 10-step edit shipped ten near-identical
80×24 screens into the conversation. Two changes fix that.

**1. `view` — return what changed, not everything.** Every screen-returning tool takes
`view`, and the server-wide default is `SHELLF_VIEW`:

| `view` | Returns |
| --- | --- |
| `auto` *(default)* | the changed rows, or the full screen — **whichever is actually smaller** |
| `screen` | the full trimmed grid (the old behavior) |
| `diff` | only rows that changed since you last saw the screen |
| `quiet` | the header alone: size, liveness, cursor, how many rows moved |
| `none` | `ok` |

`auto` measures both renderings and returns the shorter one, so a diff can never be a
regression — only a win. That matters more than it sounds: on a sparse vim screen the
row-number prefixes make a diff *bigger* than the screen it describes, and on htop —
which repaints every row on a timer — a diff is pure noise. `auto` quietly does the
right thing in both cases, and the agent never has to know which kind of app it is driving.

A `quiet` view discloses nothing, so it does not advance the diff baseline: after a run
of quiet steps the next `diff` still reports everything that happened while you weren't
looking.

**2. `run` — several actions, one screen.** Five tool calls means five screens in the
transcript, four of which were only a stepping stone. `run` takes `"verb:payload"` steps
and returns the final state once:

```python
run(["press:i", "type:hello world", "press:escape", "type::wq", "press:enter"])
```

Verbs: `type` · `paste` · `press` · `click` · `mouse` · `wait` · `waitgone` · `regex` ·
`stable` · `sleep` · `raw`. It stops at the first failing step and tells you which one,
so a half-applied sequence is never silent.

**Measured on this machine** (`bench/token_bench.py`, real programs, eight realistic
tasks — vim, less, python, bash, htop, btop, nano, tmux):

| | characters returned | vs. before |
| --- | --- | --- |
| before (full screen every call) | 59,052 | — |
| `view=auto` | 12,594 | **−79%** |
| `view=auto` + batched `run` | 11,992 | **−80%**, and 8× fewer round trips |

The per-scenario range for `auto` is −58% (a short bash session, already cheap) to −86%
(btop, which redraws constantly). Batching saves fewer *characters* than it looks —
`auto` already collapsed the intermediate screens — but it removes the round trips
themselves, which is where the model's own reasoning tokens go.

### Environment control

`launch(env={...})` passes environment straight to the program — the other lever on
cost and predictability, since the cheapest screen is the one the app never draws:

```python
launch("git", ["log"], env={"PAGER": "cat", "NO_COLOR": "1"})
```

`SHELLF_ENV="NO_COLOR=1,PAGER=cat"` applies a set to *every* launch, so the agent pays
zero tokens per call for it. `TERM` now defaults to `xterm-256color` regardless of what
terminal the MCP client is running in — the PTY really is an xterm — and an explicit
`env={"TERM": ...}` still wins.

Other `SHELLF_*` knobs: `SHELLF_VIEW`, `SHELLF_COLS`/`SHELLF_ROWS`, `SHELLF_MAX_ROWS`
(cap rows per response, eliding the middle), `SHELLF_SHOT_SCALE`. `get_modes` reports the
effective config.

```bash
claude mcp add shellf-driving \
  -e SHELLF_VIEW=auto -e SHELLF_ENV=NO_COLOR=1,PAGER=cat \
  -- "$PWD/.venv/bin/python" -m shellf.server
```

### Also in Phase 3

- **Region reads**: `snapshot(region=[x, y, w, h])` for one status bar or one tmux pane.
- **Cheaper screenshots**: `screenshot(scale=0.5)` quarters the image tokens;
  `screenshot(grid=True)` overlays a labelled 1-based ruler so the model can read click
  coordinates off the picture instead of counting cells.
- **`wait_for_text(..., regex=True)`**: status lines are usually shaped, not fixed —
  `"step \d+ of \d+"`, `"\[#+\s*\] \d+%"`.
- **Mouse gestures**: `double_click` (word), `triple_click` (line), and `drag` with
  `to_x`/`to_y` — one call, because apps track the motion between the ends.
- **Raw bytes**: `run(["raw:\\x1b[A"])` / `send_bytes` for sequences with no name here.

### Sequential shortcuts (prefix chords)

Many TUIs use a prefix key followed by more keys — tmux `Ctrl+B d`, screen
`Ctrl+A d`, Emacs `Ctrl+X Ctrl+S`. These are first-class:

- `press(["ctrl+b", "d"])` — ad-hoc: sends the keys in order with an inter-step
  delay (`step_delay`) so the app registers the prefix before the next key.
- `shortcut("tmux", "detach")` — named, discoverable chords from a registry covering
  tmux, screen, emacs, vim, nano, less. `list_shortcuts()` shows them all.
- `define_shortcut("tmux", "vsplit", ["ctrl+b", "%"])` — extend the registry at runtime.

### Resize

`resize(cols, rows)` sends a real `SIGWINCH` to the program and resizes the emulator,
so the TUI reflows exactly as it would when you drag a terminal window edge.

## Live observability dashboard

So a human can watch what the agent is doing, the server can serve a local web page
with a **pixel-perfect live mirror** of each session plus a timeline of every MCP tool
call. The mirror tees the raw PTY bytes straight into xterm.js — same bytes the app
emits — so it's exact, not a re-render.

The timeline shows each call's **arguments, duration, and result** (first line of the
returned screen), with errors highlighted; click an entry to expand the full args.
Live counters track calls / errors / sessions plus per-tool call counts, a filter box
narrows the stream, and session tabs carry live/dead status and the current grid size.

```bash
SHELLF_OBSERVE_PORT=7331 python -m shellf.server     # then open http://127.0.0.1:7331
```

Off by default; binds to `127.0.0.1` only. To enable it for the MCP server:

```bash
claude mcp add shellf-driving -e SHELLF_OBSERVE_PORT=7331 -- "$PWD/.venv/bin/python" -m shellf.server
```

A byte-mirror is dimensionally rigid — the app draws for an exact `cols×rows`, so the
dashboard terminal is **locked to the PTY's grid**, never the browser window:

- **Browser window resize** → the fixed-grid terminal is scaled to fit (letterboxed);
  cols/rows are never changed, so the mirror can't desync.
- **Session resize** (agent calls `resize` → SIGWINCH) → the engine emits a resize
  event and the dashboard `resize()`s xterm to the new grid, then re-fits.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# register with Claude Code (run inside this project dir)
claude mcp add shellf-driving -- "$PWD/.venv/bin/python" -m shellf.server
```

## Tests

Everything runs against *real programs* (bash, vim, htop, tmux) — no mocks:

```bash
pip install -e ".[dev]"
pytest
```

| File | Covers |
| --- | --- |
| `tests/test_engine.py` | drives bash, vim, htop directly (keys, mouse, alt-screen) |
| `tests/test_shortcuts.py` | resize reflow + tmux prefix chords |
| `tests/test_phase1.py` | mode-sniffer, query responder, wait_for_stable, scrollback |
| `tests/test_phase2.py` | kitty CSI-u stripping, bracketed paste, kill escalation |
| `tests/test_phase3.py` | view modes, env passthrough, batched `run`, regex waits, gestures |
| `tests/test_decckm_e2e.py` | proves arrows arrive as `ESC O x` under DECCKM |
| `tests/test_mcp_client.py` / `test_mcp_drive.py` | full round-trip over MCP stdio |
| `tests/test_observe.py` | dashboard: schema integrity + live SSE data flow |

## License

MIT — see [LICENSE](LICENSE).
