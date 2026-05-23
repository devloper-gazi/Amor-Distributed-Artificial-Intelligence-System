"""Find the Chrome window currently showing AMOR (title contains 'AMOR'
or hostname 'localhost'), bring it to the foreground, and capture its
client area via PIL.ImageGrab.

Usage:
    python tools/screenshot_amor.py docs/screenshots/v2.8.4/hero.png

The script is intentionally dependency-light: ctypes (stdlib) for the
Win32 lookup + PIL for the bitmap.  Returns a non-zero exit code if
the AMOR window can't be located so a CI pipeline can fail fast.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

try:
    from PIL import ImageGrab
except ImportError as exc:  # pragma: no cover
    print(f"ERROR: PIL not available: {exc}", file=sys.stderr)
    sys.exit(2)


user32 = ctypes.windll.user32
user32.SetProcessDPIAware()  # honor Windows DPI scaling so coords match pixels

EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def find_amor_window() -> int | None:
    """Return HWND of the first visible Chrome window whose title
    contains 'AMOR'.  Falls back to titles mentioning 'localhost'
    so the script still works while the user is logged out (login
    page also reachable via localhost)."""
    target: list[int] = []

    def _cb(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        upper = title.upper()
        # Chrome/Edge tabs render the page title in the window title.
        # AMOR's <title> is set via the v2 SPA shell + login page so
        # both pre- and post-auth states match here.
        if (
            "AMOR" in upper
            or "LOCALHOST" in upper
            or "AMOR-" in upper.replace(" ", "-")
        ):
            # Only accept windows that also contain a browser hint to
            # avoid hitting random apps that happen to have AMOR in
            # their title (e.g. VS Code with this repo open).
            if any(b in title for b in ("Chrome", "Edge", "Firefox", "Brave")):
                target.append(int(hwnd))
                return False  # stop enumeration
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return target[0] if target else None


def bring_to_front(hwnd: int) -> None:
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    # Give the WM time to bring the window forward before we grab.
    time.sleep(0.5)


def capture_window(hwnd: int, output: Path) -> tuple[int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed")
    bbox = (rect.left, rect.top, rect.right, rect.bottom)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output, format="PNG", optimize=True)
    return img.size


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: python tools/screenshot_amor.py <output.png> [--no-foreground]",
            file=sys.stderr,
        )
        return 2

    output = Path(argv[1]).resolve()
    no_foreground = "--no-foreground" in argv[2:]

    hwnd = find_amor_window()
    if hwnd is None:
        print("ERROR: no Chrome window titled 'AMOR' found", file=sys.stderr)
        return 1

    title = _window_title(hwnd)
    print(f"Found window 0x{hwnd:08x}  title={title!r}")

    if not no_foreground:
        bring_to_front(hwnd)
        # Re-fetch rect after bringing forward (multi-monitor edge case).
        time.sleep(0.3)

    size = capture_window(hwnd, output)
    print(f"Saved {output}  size={size[0]}x{size[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
