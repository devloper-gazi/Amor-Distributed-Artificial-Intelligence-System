"""Entry point: `python -m tools.setup <cmd> [args]`.

We keep this module tiny so `import tools.setup` is cheap.  All logic
lives in `cli.py`.

Windows note: the default code page (cp1252) on older terminals can't
render the Unicode glyphs (✓, ✗, ▶, ⠋…) we use for status lines.
Python 3.7+ exposes `sys.stdout.reconfigure(encoding=...)`; we use it
to force UTF-8 BEFORE any module-level color/glyph code runs.
"""

from __future__ import annotations

import sys


def _force_utf8_stdio() -> None:
    """Best-effort: switch stdout/stderr to UTF-8 on Windows.

    No-op on POSIX (which is UTF-8 by default in any non-ancient
    locale) and on terminals that already speak UTF-8.
    """

    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _main() -> int:
    _force_utf8_stdio()
    from tools.setup.cli import main  # local import keeps __init__ tiny

    try:
        return main(sys.argv[1:])
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_main())
