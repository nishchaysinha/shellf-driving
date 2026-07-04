# Shellf-Driving — Capability Spec

Everything a "Playwright for terminal UIs" might need, organized by area. This is a
planning map, not a commitment: it tracks what exists, what's missing, and how much
each thing matters for the primary goal — **an LLM agent reliably operating live TUIs**.

**Status:** ✅ done · 🟡 partial · ⬜ not started
**Priority:** **P0** correctness/blocking · **P1** important · **P2** nice-to-have

---

## 0. The critical correctness gaps (read this first)

These are not features — they're places where the current engine can silently do the
*wrong* thing or hang. They outrank every feature below.

> **✅ Phase 1 shipped (all P0s below resolved).** Mode-sniffer (`modes.py`) tracks
> DECCKM/alt-screen/mouse/bracketed-paste/focus from output; arrows now send `ESC O x`
> under DECCKM (proven end-to-end); query auto-responder answers DA/DSR so apps don't
> hang; `wait_for_stable` + baseline-version **auto-wait** baked into every action tool;
> scrollback via `HistoryScreen` (`read_history`); Alt/Meta keys. New MCP tools:
> `wait_for_stable`, `read_history`, `get_modes`. See `tests/test_phase1.py`, `tests/test_decckm_e2e.py`.
>
> **✅ Phase 2 shipped** (fixes surfaced by long real-agent sessions): bracketed-paste
> `type_text` (auto for multi-line) · kitty CSI-u sequences stripped (no `1;1u` screen
> garbage from Claude Code/neovim) · `kill` waits + escalates past ignored SIGTERM ·
> `click_text` · `wait_for_text_gone` · trimmed (token-cheap) snapshots · richer
> dashboard timeline (durations, results, errors, filtering). See `tests/test_phase2.py`.

| Gap | Why it bites | Pri |
| --- | --- | --- |
| **Application cursor keys (DECCKM)** | When an app sets mode `?1` (vim, less, fzf often do), arrow keys must be sent as `ESC O A`, **not** `ESC [ A`. We always send the latter, so arrows can break inside exactly the apps people automate most. **Verified: pyte does _not_ track this mode** (it only tracks DECAWM/DECCOLM/DECOM/DECSCNM/DECTCEM/IRM/LNM and silently drops `?1`). So we must add our own **mode-sniffer** that watches the PTY output for `ESC[?1h`/`ESC[?1l` and switch arrow encodings accordingly. | **P0** |
| **Terminal query auto-responses** | Apps send queries and *block waiting for the terminal to answer*: Device Attributes (`ESC[c`), cursor-position report (`ESC[6n`), DECRQM, color/`XTGETTCAP`, bracketed-paste probes. We never reply → some programs hang or mis-detect capabilities. Need a responder thread that scans output and writes answers back on the PTY. | **P0** |
| **"Render settled" detection** | Agents act, then read — but a TUI may still be repainting. Without a `wait_for_stable` (no screen changes for N ms) the model screenshots mid-frame and misreads state. The single highest-value sync primitive. | **P0** |
| **Keypad / DECCKM-style modes for Home/End/etc.** | Same class as DECCKM: numpad and editing keys shift sequences in application keypad mode. Lower frequency than arrows but same root cause. | **P1** |
| **Alternate screen buffer** | vim/htop/less switch to the alt buffer (`?1049`). pyte doesn't surface this mode either — confirm the active buffer is what snapshot/screenshot show and that leaving an app restores the prior screen (looked OK in tests; needs an explicit assertion). The same mode-sniffer can track it. | **P1** |

---

## 1. Observation — reading the screen

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| Plain-text snapshot | ✅ | — | `snapshot` |
| PNG screenshot (color/bold/cursor/mouse overlay) | ✅ | — | `screenshot` |
| Per-cell style query | 🟡 | P1 | `cell()` on engine; not yet an MCP tool |
| Cursor position / visibility | ✅ | — | `cursor()` |
| Find text → coordinates | ✅ | — | `find_text` |
| Region / bounding-box read | ⬜ | P1 | Read a sub-rectangle (e.g. a status bar or a pane) |
| **Scrollback / history buffer** | ✅ | — | `HistoryScreen` + `read_history` exposes output that scrolled off (verified). |
| Screen **diff** since last snapshot | ⬜ | P1 | Return only changed lines/cells → token-efficient, shows the model *what just happened* |
| Trim/compact representation | ✅ | — | every `_render` strips trailing blank rows/cols |
| Grid-coordinate overlay on screenshot | ⬜ | P1 | Ruler/gridlines so the model can pick click coords accurately |
| Terminal **title** capture (OSC 0/2) | ⬜ | P2 | Apps set the title to convey state |
| **Hyperlinks** (OSC 8) capture | ⬜ | P2 | Modern TUIs emit clickable links |
| Bell / alert detection | ⬜ | P2 | `\a` often signals errors/completion |
| Wide-char / CJK / emoji width | 🟡 | P1 | pyte handles most; verify cursor math + screenshot spacing |
| Sixel / kitty / iTerm inline images | ⬜ | P2 | Detect + optionally surface as image regions |

## 2. Input — driving the program

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| Type literal text | ✅ | — | `type_text` |
| Named keys + ctrl combos | ✅ | — | `press`, `keys.py` |
| Sequential chords / prefix keys | ✅ | — | `press`, `shortcut`, `shortcuts.py` |
| Mouse click/press/release/move/scroll | ✅ | — | `mouse`, SGR-1006 |
| **Alt / Meta modifier** | ✅ | — | `alt+x` = `ESC` prefix. Common in shells/emacs; no support yet |
| Shift / Ctrl / Alt + arrows, Home/End, nav | ✅ | — | `ESC[1;<mod><final>` encodings; unknown combos RAISE (don't type garbage into docs) |
| Shift/Ctrl + function keys | ⬜ | P2 | Modified F-keys; lower frequency |
| Mouse **drag** helper | ⬜ | P1 | press→move(s)→release in one call (text selection, sliders) |
| Double / triple click | ⬜ | P1 | Word/line selection |
| Mouse modifiers (ctrl/shift+click) | ⬜ | P2 | Modifier bits in the SGR button code |
| **Bracketed paste** | ✅ | — | `type_text` auto-pastes multi-line text when the app enabled mode 2004; `paste` param to force |
| Human-like typing cadence | ⬜ | P2 | Per-char delay; some TUIs drop fast input |
| Raw byte injection | ⬜ | P1 | Escape hatch: send arbitrary bytes |
| Focus in/out events | ⬜ | P2 | `ESC[I`/`ESC[O`; some apps redraw on focus |

## 3. Synchronization — waiting reliably

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| `wait_for_text` | ✅ | — | |
| **`wait_for_stable`** (idle N ms) | ✅ | — | See §0 |
| `wait_for_text_gone` | ✅ | — | tool + engine method |
| `wait_for_regex` | ⬜ | P1 | Match patterns, not just substrings |
| `wait_for_cursor` (position) | ⬜ | P2 | Some apps signal readiness via cursor |
| `wait_for_exit` | ✅ | — | engine method; `kill` waits for real death before returning |
| Auto-wait baked into actions | ✅ | — | Every action tool waits for its own repaint to settle (baseline-version) |

## 4. Session & lifecycle

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| Launch w/ args, env, cwd, size | ✅ | — | `launch` |
| Multiple named sessions | ✅ | — | |
| Kill / signal | ✅ | — | `sig: term|int|hup|quit|kill`, waits for death, auto-escalates to SIGKILL (shells ignore SIGTERM) |
| Restart session | ⬜ | P2 | Relaunch same spec |
| Process info (pid/tree/alive) | 🟡 | P2 | `list_sessions` has some; add pid/children |
| Auto-cleanup / idle timeout | ⬜ | P1 | Reap zombies, cap session lifetime |
| Output flood / backpressure guard | ⬜ | P1 | Rate-limit/cap reads so a `yes`-style flood can't OOM |
| Attach to existing PTY/process | ⬜ | P2 | Drive something already running |

## 5. Recording, replay & tracing (the testing/codegen side)

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| **asciinema `.cast` recording** | ⬜ | P1 | Standard format; replayable, shareable |
| Action **trace** (timeline of action+screenshot) | ⬜ | P1 | The Playwright trace-viewer analog; great for debugging agent runs |
| **Codegen** — record human → script | ⬜ | P2 | Watch a session, emit a Shellf-Driving script |
| Script replay | ⬜ | P2 | Run a saved sequence deterministically |
| Golden-snapshot / visual regression | ⬜ | P2 | For the automated-testing use case |

## 6. Terminal fidelity / emulation

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| Resize / SIGWINCH | ✅ | — | `resize` (verified reflow) |
| TERM / color depth (16/256/truecolor) | ✅ | — | pyte |
| `HardenedScreen` (tolerate private SGR) | ✅ | — | |
| DECCKM application cursor keys | ✅ | — | mode-sniffer + ESC O x; proven e2e |
| Query auto-responder (DA/DSR/…) | ✅ | — | answers primary/secondary DA, DSR 5/6 |
| Alternate screen buffer | ✅ | — | HardenedScreen implements the 47/1047/1049 buffer save+restore pyte lacks; shell restores after vim/grotto exit (verified) |
| Bracketed-paste mode tracking | ✅ | — | sniffed (mode 2004); drives auto-paste in `type_text` |
| Kitty keyboard protocol (CSI-u) | ✅ | — | sequences stripped before pyte (no more `1;1u` screen garbage); support query deliberately unanswered so apps fall back to legacy keys |
| Mouse-mode tracking | ✅ | — | `get_modes`; mouse tool warns if reporting is off |
| Tab stops / charsets | 🟡 | P2 | pyte default; rarely an issue |

## 7. Agent ergonomics (LLM-specific affordances)

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| Observe-after-act (return screen each action) | ✅ | — | Every tool returns the new screen |
| High-level `click_text("OK")` | ✅ | — | occurrence picker; warns when mouse reporting is off |
| Token-efficient snapshots | ✅ | — | trailing blanks/rows trimmed on every returned screen; diff mode still ⬜ |
| Annotated screenshots (coord grid) | ⬜ | P1 | Helps the model aim the mouse |
| Semantic/“accessibility” extraction | ⬜ | P2 | Detect menus/buttons/tables as structures — hard, high value |
| Per-action auto-retry + auto-wait | ⬜ | P1 | Fewer flaky agent steps |
| Capability hints in errors | ✅ | — | `shortcut`/`press` give guidance on unknown input |

## 8. Robustness & safety

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| Resilient to bad escape bytes | ✅ | — | Per-byte fallback in feed loop |
| Thread-safe screen access | ✅ | — | RLock |
| Timeouts on all blocking ops | 🟡 | P1 | `wait_for_text` yes; audit the rest |
| Output backpressure | ⬜ | P1 | See §4 |
| Zombie/orphan reaping | ✅ | — | `kill` escalates + waitpid reaps; session name freed for relaunch |
| Sandboxing / allowed-command policy | ⬜ | P2 | Restrict what an agent may launch |

## 9. Deployment & integration

| Capability | Status | Pri | Notes |
| --- | --- | --- | --- |
| MCP server | ✅ | — | 19 tools |
| Python library API | ✅ | — | `TerminalSession` |
| pip / editable install | ✅ | — | |
| Standalone CLI (manual poke/REPL) | ⬜ | P1 | Drive a session by hand for debugging |
| Config (default size/TERM/timeouts) | ⬜ | P2 | Central settings |
| **Live observability dashboard** | ✅ | — | `observe.py`: localhost web UI, xterm.js byte-mirror per session + tool-call timeline (SHELLF_OBSERVE_PORT). Browser resize = CSS scale-to-fit; session resize = grid event |
| Structured logging / verbosity | 🟡 | P2 | The observe event stream is a start; no file logger yet |
| pytest plugin (fixtures) | ⬜ | P2 | For the testing use case |
| Remote/containerized targets (docker/ssh) | ⬜ | P2 | Drive a TUI in another env |
| Windows (ConPTY) | ⬜ | P2 | Big lift; Unix-only today |

---

## Suggested phasing

- **Phase 1 — Correctness (P0).** DECCKM arrow keys · query auto-responder ·
  `wait_for_stable` · scrollback/history · Alt modifier. *Without these, agents fail
  inside the apps people care about most.*
- **Phase 2 — Agent ergonomics (P1).** `click_text` · screen diff + compaction ·
  grid-annotated screenshots · drag/double-click · bracketed paste · `wait_for_*`
  family · auto-wait in actions · expose `cell`/region reads.
- **Phase 3 — Tooling & scale (P1/P2).** asciinema recording + action trace · CLI ·
  signal control · backpressure/cleanup · codegen/replay · pytest plugin.
- **Phase 4 — Reach (P2).** semantic extraction · remote targets · Windows · sixel.
