"""Record a multi-route GIF tour of the AMOR v2.8.4 UI.

Drives the AMOR Chrome window through 3 routes (/, /settings,
/admin/llm) via Windows SendKeys, capturing frames between
navigations.  Output: animated GIF saved under
docs/screenshots/v2.8.4/.

Dependencies: PIL (Pillow).  No ffmpeg / imageio / numpy required.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab


user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


def find_amor_window() -> int | None:
    """Find Chrome window titled 'AMOR'."""
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    target: list[int] = []

    def _cb(hwnd: wintypes.HWND, _l: wintypes.LPARAM) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if "AMOR" in title and any(b in title for b in ("Chrome", "Edge")):
            target.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return target[0] if target else None


def bring_to_front(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)


def window_bbox(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def navigate_via_keys(url: str) -> None:
    """Use PowerShell SendKeys to focus URL bar + type + Enter."""
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.SendKeys]::SendWait('^l'); "
        "Start-Sleep -Milliseconds 400; "
        "[System.Windows.Forms.SendKeys]::SendWait('{DELETE}'); "
        "Start-Sleep -Milliseconds 200; "
        f"[System.Windows.Forms.SendKeys]::SendWait('{url}'); "
        "Start-Sleep -Milliseconds 300; "
        "[System.Windows.Forms.SendKeys]::SendWait('{ENTER}');"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True, check=False,
    )


def dismiss_overlays() -> None:
    """Press Escape to dismiss any extension popups (Google Translate,
    autofill prompts, etc) BEFORE capturing frames so they don't
    appear in the demo GIF."""
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.SendKeys]::SendWait('{ESC}');"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True, check=False,
    )


def capture_burst(
    bbox: tuple[int, int, int, int],
    n_frames: int,
    fps: int,
    target_width: int = 1024,
    crop_chrome_top: int = 115,
    crop_chrome_bottom: int = 0,
) -> list[Image.Image]:
    """Grab n_frames at fps over the bbox area, then crop browser
    chrome (title bar + tab strip + URL bar = ~115 px on Chrome's
    default density), resize, and palette-quantize.  Result frames
    show ONLY the AMOR page content (no Chrome UI).
    """
    frames: list[Image.Image] = []
    start = time.time()
    for i in range(n_frames):
        deadline = start + (i / fps)
        rem = deadline - time.time()
        if rem > 0:
            time.sleep(rem)
        img = ImageGrab.grab(bbox=bbox)
        w, h = img.size
        # Strip browser chrome — only the AMOR page content is wanted.
        if crop_chrome_top or crop_chrome_bottom:
            img = img.crop(
                (0, crop_chrome_top, w, h - crop_chrome_bottom)
            )
            w, h = img.size
        if w > target_width:
            ratio = target_width / w
            img = img.resize(
                (target_width, int(h * ratio)), Image.LANCZOS
            )
        frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=128))
    return frames


def main() -> int:
    hwnd = find_amor_window()
    if hwnd is None:
        print("ERROR: no AMOR Chrome window found", file=sys.stderr)
        return 1

    bring_to_front(hwnd)
    bbox = window_bbox(hwnd)
    print(f"window 0x{hwnd:08x}  bbox={bbox}")

    out_dir = Path(os.path.dirname(os.path.abspath(__file__))).parent / "docs" / "screenshots" / "v2.8.4"
    out_dir.mkdir(parents=True, exist_ok=True)

    fps = 10
    frame_ms = 1000 // fps

    all_frames: list[Image.Image] = []

    # Scene 1: empty state at / for 2.5s
    print("Scene 1: navigate to /  (empty state)")
    navigate_via_keys("http://localhost:8000/")
    time.sleep(2.5)  # let render settle
    bring_to_front(hwnd)
    dismiss_overlays()
    time.sleep(0.4)
    s1 = capture_burst(bbox, n_frames=25, fps=fps)
    all_frames.extend(s1)
    print(f"  captured {len(s1)} frames")

    # Scene 2: Settings for 2s
    print("Scene 2: navigate to /settings")
    navigate_via_keys("http://localhost:8000/settings")
    time.sleep(2.5)
    bring_to_front(hwnd)
    time.sleep(0.3)
    s2 = capture_burst(bbox, n_frames=20, fps=fps)
    all_frames.extend(s2)
    print(f"  captured {len(s2)} frames")

    # Scene 3: Admin LLM for 2s
    print("Scene 3: navigate to /admin/llm")
    navigate_via_keys("http://localhost:8000/admin/llm")
    time.sleep(2.5)
    bring_to_front(hwnd)
    time.sleep(0.3)
    s3 = capture_burst(bbox, n_frames=20, fps=fps)
    all_frames.extend(s3)
    print(f"  captured {len(s3)} frames")

    # Scene 4: back to /
    print("Scene 4: back to /")
    navigate_via_keys("http://localhost:8000/")
    time.sleep(2.0)
    bring_to_front(hwnd)
    time.sleep(0.3)
    s4 = capture_burst(bbox, n_frames=15, fps=fps)
    all_frames.extend(s4)
    print(f"  captured {len(s4)} frames")

    print(f"total {len(all_frames)} frames; encoding GIF...")
    out_path = out_dir / "demo-tour.gif"
    all_frames[0].save(
        str(out_path),
        save_all=True,
        append_images=all_frames[1:],
        duration=frame_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )
    size_kb = out_path.stat().st_size / 1024
    print(f"saved {out_path}")
    print(
        f"  {len(all_frames)} frames @ {fps} FPS  "
        f"duration={len(all_frames)/fps:.1f}s  size={size_kb:.0f} KB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
