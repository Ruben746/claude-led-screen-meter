"""Windows entry point for manual and background launches."""
import asyncio
import contextlib
import ctypes
import os
from pathlib import Path
import sys
import traceback
import webbrowser


def run():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    os.environ.setdefault("LED_IMG_PATH", str(root / "preview.png"))
    open_panel = "--background" not in sys.argv or "--open" in sys.argv
    os.environ["LED_OPEN_BROWSER"] = "1" if open_panel else "0"
    import claude_meter as meter

    # A fixed per-session mutex prevents manual and startup launches overlapping.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, "Local\\LED-Meter")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_running = ctypes.get_last_error() == 183
    try:
        if already_running:
            if open_panel:
                webbrowser.open(f"http://localhost:{meter.WEB_PORT}")
            return
        asyncio.run(meter.main())
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    if "--background" in sys.argv:
        log = Path(__file__).with_name("meter.log")
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(log.with_suffix(".log.old"))
        with log.open("a", encoding="utf-8", buffering=1) as stream:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                try:
                    run()
                except Exception:
                    traceback.print_exc()
    else:
        try:
            run()
        except KeyboardInterrupt:
            print("Stopped.")
