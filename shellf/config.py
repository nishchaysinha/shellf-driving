"""Server configuration from environment variables.

Every knob here exists so an operator can tune the server *once*, at registration
time, instead of the agent paying tokens to pass the same parameter on every call.
That matters: a `view="diff"` argument on 40 tool calls is 40 copies of the same
decision in the transcript. `SHELLF_VIEW=diff` is zero.

    claude mcp add shellf-driving \
        -e SHELLF_VIEW=auto -e SHELLF_ENV=NO_COLOR=1,PAGER=cat \
        -- /path/.venv/bin/python -m shellf.server

Variables
---------
SHELLF_VIEW           default response shape: auto | screen | diff | quiet | none
SHELLF_COLS/ROWS      default terminal size for `launch` (default 80x24)
SHELLF_MAX_ROWS       cap the rows any single response may contain (0 = no cap)
SHELLF_ENV            extra env for every launched program: "K=V,K2=V2"
SHELLF_SHOT_SCALE     default screenshot scale factor (e.g. 0.6 → fewer image tokens)
SHELLF_OBSERVE_PORT   localhost port for the live dashboard (unset = off)
"""

from __future__ import annotations

import os

DEFAULT_COLS = 80
DEFAULT_ROWS = 24


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def view_mode() -> str:
    """Default response shape for tools that return a screen."""
    from .view import MODES
    mode = os.environ.get("SHELLF_VIEW", "auto").strip().lower()
    return mode if mode in MODES else "auto"


def cols() -> int:
    return _int("SHELLF_COLS", DEFAULT_COLS)


def rows() -> int:
    return _int("SHELLF_ROWS", DEFAULT_ROWS)


def max_rows() -> int:
    """Hard cap on rows in a response (0 = uncapped). Guards against a 200-row grid."""
    return max(0, _int("SHELLF_MAX_ROWS", 0))


def shot_scale() -> float:
    s = _float("SHELLF_SHOT_SCALE", 1.0)
    return s if 0.1 <= s <= 4.0 else 1.0


def base_env() -> dict[str, str]:
    """Extra environment applied to every launched program ("K=V,K2=V2").

    Values may contain '=' (only the first is the separator); entries without one
    are ignored rather than raising, so a typo degrades instead of breaking launch.
    """
    raw = os.environ.get("SHELLF_ENV", "")
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v
    return out


def as_dict() -> dict:
    """Effective configuration, for diagnostics."""
    return {
        "view": view_mode(),
        "cols": cols(),
        "rows": rows(),
        "max_rows": max_rows(),
        "shot_scale": shot_scale(),
        "env": base_env(),
        "observe_port": os.environ.get("SHELLF_OBSERVE_PORT"),
    }
