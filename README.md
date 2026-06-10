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
  terminal.py   PTY host + pyte screen emulation + key & mouse input
  keys.py       key-name → terminal escape-sequence map
  render.py     screen → PNG (colors, bold, cursor outline, mouse marker)
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

`launch` · `snapshot` · `screenshot` · `type_text` · `press` · `mouse` ·
`shortcut` · `list_shortcuts` · `define_shortcut` ·
`wait_for_text` · `wait_for_stable` · `find_text` · `read_history` · `get_modes` ·
`resize` · `kill` · `list_sessions`

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

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# register with Claude Code (run inside this project dir)
claude mcp add shellf-driving -- "$PWD/.venv/bin/python" -m shellf.server
```

## Tests

```bash
.venv/bin/python test_engine.py        # drives bash, vim, htop directly
.venv/bin/python test_shortcuts.py     # resize reflow + tmux prefix chords
.venv/bin/python test_phase1.py        # mode-sniffer, query responder, stable, history
.venv/bin/python test_decckm_e2e.py    # proves arrows arrive as ESC O x under DECCKM
.venv/bin/python test_mcp_client.py    # drives the MCP server over stdio
```
