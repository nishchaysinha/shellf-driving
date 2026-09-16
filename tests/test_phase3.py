"""Phase 3: token-efficient views, env passthrough, batched runs, richer input.

Everything here drives real programs, like the rest of the suite.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from shellf import config, server, view as _view
from shellf.terminal import TerminalSession


@pytest.fixture
def bash():
    ts = TerminalSession("bash", ["--norc", "--noprofile"], cols=80, rows=24)
    ts.wait_for_stable(quiet=0.3, timeout=3.0)
    yield ts
    ts.kill()


def _settle(ts, send):
    """Send, then wait for THIS action's repaint — wait_for_stable with no
    since_version returns immediately when the screen was already quiet."""
    v = ts.version()
    send()
    ts.wait_for_stable(quiet=0.3, timeout=3.0, since_version=v)


def _session(name, command, args=None, **kw):
    """Register a session in the server's table so the tool functions can drive it."""
    ts = TerminalSession(command, args=args, cols=kw.pop("cols", 80), rows=kw.pop("rows", 24), **kw)
    server._sessions[name] = ts
    server._views[name] = _view.ViewState()
    ts.wait_for_stable(quiet=0.4, timeout=4.0)
    return ts


def _drop(name):
    ts = server._sessions.pop(name, None)
    server._views.pop(name, None)
    if ts:
        ts.kill()


# --------------------------------------------------------------------------- #
# View modes
# --------------------------------------------------------------------------- #
def test_diff_returns_only_changed_rows():
    """In a full-screen app — where the whole point of a diff lies — one edit must
    cost one row, not a screen. (In a scrolling shell every row shifts, so a diff
    correctly stops paying; that is what `auto` is for.)"""
    try:
        ts = _session("diffvim", "vim", ["-u", "NONE"])
        state = _view.ViewState()
        full = _view.render(ts, state, "screen")       # baseline
        _settle(ts, lambda: ts.send_sequence(["i"]))
        _settle(ts, lambda: ts.send_text("marker_one"))
        diff = _view.render(ts, state, "diff")
        assert "marker_one" in diff
        assert "rows changed" in diff.splitlines()[0]
        assert len(diff) < len(full) / 2, f"diff={len(diff)} full={len(full)}"
    finally:
        _drop("diffvim")


def test_diff_reports_no_change_when_screen_is_static(bash):
    state = _view.ViewState()
    _view.render(bash, state, "screen")
    out = _view.render(bash, state, "diff")
    assert "no change" in out
    assert len(out.splitlines()) == 1      # one header line, nothing else


def test_quiet_does_not_advance_the_baseline(bash):
    """A quiet view discloses nothing, so the next diff must still show everything
    that happened while the agent was not looking."""
    state = _view.ViewState()
    _view.render(bash, state, "screen")
    _settle(bash, lambda: bash.send_text("echo alpha_marker\r"))
    quiet = _view.render(bash, state, "quiet")
    assert "alpha_marker" not in quiet
    assert len(quiet.splitlines()) == 1
    _settle(bash, lambda: bash.send_text("echo beta_marker\r"))
    diff = _view.render(bash, state, "diff")
    assert "alpha_marker" in diff and "beta_marker" in diff


def test_auto_never_costs_more_than_the_full_screen(bash):
    """The property that makes `auto` safe as the default: it is never a regression."""
    state = _view.ViewState()
    _view.render(bash, state, "screen")
    for cmd in ("echo one", "ls /nonexistent-path-xyz", "printf 'a\\nb\\nc\\n'"):
        _settle(bash, lambda c=cmd: bash.send_text(c + "\r"))
        full = _view.render(bash, _view.ViewState(), "screen")
        auto = _view.render(bash, state, "auto")
        assert len(auto) <= len(full), f"auto was larger than screen after {cmd!r}"


def test_none_view_is_a_bare_ok(bash):
    assert _view.render(bash, _view.ViewState(), "none") == "ok"


def test_unknown_view_raises(bash):
    with pytest.raises(ValueError, match="Unknown view"):
        _view.render(bash, _view.ViewState(), "verbose")


def test_max_rows_caps_the_response(bash):
    _settle(bash, lambda: bash.send_text("seq 1 60\r"))
    capped = _view.render(bash, _view.ViewState(), "screen", max_rows=10)
    assert len(capped.splitlines()) <= 12          # header + 10 rows + elision marker
    assert "elided" in capped


# --------------------------------------------------------------------------- #
# env passthrough
# --------------------------------------------------------------------------- #
def test_launch_passes_env_to_the_program():
    try:
        ts = _session("envtest", "bash", ["--norc", "--noprofile"],
                      env={"SHELLF_TEST_TOKEN": "banana42"})
        ts.send_text("echo $SHELLF_TEST_TOKEN\r")
        assert ts.wait_for_text("banana42", timeout=3.0)
    finally:
        _drop("envtest")


def test_term_defaults_to_xterm_and_env_can_override():
    """The PTY really is an xterm, whatever terminal the MCP client runs in — but an
    explicit env= still wins, so a caller can force TERM=dumb."""
    try:
        ts = _session("termtest", "bash", ["--norc", "--noprofile"])
        ts.send_text("echo TERM=$TERM\r")
        assert ts.wait_for_text("TERM=xterm-256color", timeout=3.0)
    finally:
        _drop("termtest")
    try:
        ts = _session("termtest2", "bash", ["--norc", "--noprofile"], env={"TERM": "dumb"})
        ts.send_text("echo TERM=$TERM\r")
        assert ts.wait_for_text("TERM=dumb", timeout=3.0)
    finally:
        _drop("termtest2")


def test_shellf_env_config_is_parsed():
    os.environ["SHELLF_ENV"] = "NO_COLOR=1,PAGER=cat,BROKEN,X=a=b"
    try:
        assert config.base_env() == {"NO_COLOR": "1", "PAGER": "cat", "X": "a=b"}
    finally:
        del os.environ["SHELLF_ENV"]


def test_view_config_falls_back_on_garbage():
    os.environ["SHELLF_VIEW"] = "nonsense"
    try:
        assert config.view_mode() == "auto"
    finally:
        del os.environ["SHELLF_VIEW"]


# --------------------------------------------------------------------------- #
# Batched run
# --------------------------------------------------------------------------- #
def test_run_executes_a_whole_vim_edit_in_one_call():
    path = os.path.join(tempfile.mkdtemp(), "run_edit.txt")
    try:
        _session("runvim", "vim", ["-u", "NONE", path])
        server.run(
            ["press:i", "type:written by run", "press:escape", "type::wq", "press:enter"],
            session="runvim",
        )
        time.sleep(0.6)
        with open(path) as fh:
            assert "written by run" in fh.read()
    finally:
        _drop("runvim")


def test_run_reports_the_failing_step_and_stops():
    try:
        _session("runfail", "bash", ["--norc", "--noprofile"])
        out = server.run(
            ["type:echo before_fail", "press:enter", "wait:never_gonna_appear_xyz",
             "type:echo after_fail", "press:enter"],
            session="runfail", view="screen",
        )
        assert "stopped at step 3/5" in out
        assert "FAILED" in out
        assert "after_fail" not in out     # the steps after the failure did not run
    finally:
        _drop("runfail")


def test_run_rejects_an_unknown_verb():
    try:
        _session("runverb", "bash", ["--norc", "--noprofile"])
        out = server.run(["frobnicate:x"], session="runverb")
        assert "Unknown step verb" in out
    finally:
        _drop("runverb")


def test_run_raw_step_sends_bytes():
    try:
        ts = _session("runraw", "bash", ["--norc", "--noprofile"])
        ts.send_text("echo raw_marker")
        # ESC[D is a left-arrow; harmless, proves the bytes reached the program.
        server.run([r"raw:\x1b[D", "press:enter"], session="runraw")
        assert ts.wait_for_text("raw_marker", timeout=3.0)
    finally:
        _drop("runraw")


def test_run_is_cheaper_than_the_same_steps_one_at_a_time():
    """The whole point of the tool: one screen instead of N."""
    steps = ["type:echo a", "press:enter", "type:echo b", "press:enter",
             "type:echo c", "press:enter"]
    try:
        _session("cheap1", "bash", ["--norc", "--noprofile"])
        separate = 0
        for s in steps:
            server._run_step(server._sessions["cheap1"], s, 0.12)
            separate += len(server.snapshot(session="cheap1", view="screen"))
    finally:
        _drop("cheap1")
    try:
        _session("cheap2", "bash", ["--norc", "--noprofile"])
        batched = len(server.run(steps, session="cheap2", view="auto"))
    finally:
        _drop("cheap2")
    assert batched < separate / 2, f"batched={batched} separate={separate}"


# --------------------------------------------------------------------------- #
# Input additions
# --------------------------------------------------------------------------- #
def test_wait_for_regex_matches_a_shaped_status_line(bash):
    bash.send_text("echo 'step 7 of 19 done'\r")
    assert bash.wait_for_regex(r"step \d+ of \d+", timeout=3.0) == "step 7 of 19"


def test_wait_for_regex_returns_none_on_timeout(bash):
    assert bash.wait_for_regex(r"zzz_no_such_pattern_\d+", timeout=0.5) is None


def test_send_bytes_reaches_the_program(bash):
    bash.send_bytes(b"echo raw_bytes_ok\r")
    assert bash.wait_for_text("raw_bytes_ok", timeout=3.0)


def test_mouse_gestures_emit_the_right_sgr_sequences(bash, monkeypatch):
    sent = []
    monkeypatch.setattr(bash, "_write", lambda data: sent.append(data))
    bash.send_mouse("double_click", 5, 3)
    assert sent.count(b"\x1b[<0;5;3M") == 2 and sent.count(b"\x1b[<0;5;3m") == 2
    sent.clear()
    bash.send_drag(1, 1, 5, 1, steps=2)
    assert sent[0] == b"\x1b[<0;1;1M"                  # press at the start
    assert sent[-1] == b"\x1b[<0;5;1m"                 # release at the end
    assert any(b";32;" in s or s.startswith(b"\x1b[<32;") for s in sent[1:-1])  # motion


# --------------------------------------------------------------------------- #
# Region reads + screenshot shaping
# --------------------------------------------------------------------------- #
def test_snapshot_region_reads_one_rectangle():
    try:
        ts = _session("region", "bash", ["--norc", "--noprofile"])
        ts.send_text("printf 'ROWONE\\nROWTWO\\nROWTHREE\\n'\r")
        ts.wait_for_stable(quiet=0.3, timeout=3.0)
        out = server.snapshot(session="region", region=[0, 0, 80, 2])
        assert len(out.splitlines()) <= 3      # header + 2 rows
        assert "region x=0 y=0 80x2" in out
    finally:
        _drop("region")


def test_screenshot_scale_shrinks_the_image():
    from shellf.render import Renderer
    try:
        ts = _session("shot", "bash", ["--norc", "--noprofile"])
        r = Renderer()
        full = r.render(ts.screen, ts.cursor(), None, scale=1.0)
        half = r.render(ts.screen, ts.cursor(), None, scale=0.4)
        from PIL import Image as PILImage
        import io
        assert PILImage.open(io.BytesIO(half)).size[0] < \
               PILImage.open(io.BytesIO(full)).size[0]
    finally:
        _drop("shot")
