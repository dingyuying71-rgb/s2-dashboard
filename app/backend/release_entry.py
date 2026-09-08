"""Packaged entry point for an isolated S2 Dashboard installation."""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path


def _configure_release_environment() -> None:
    # Prefer PyInstaller's resolved resource directory on both platforms.
    meipass = getattr(sys, "_MEIPASS", None)
    executable_root = Path(sys.executable).resolve().parent
    if meipass:
        bundle_root = Path(meipass)
    elif sys.platform == "darwin" and (executable_root.parent / "Resources").is_dir():
        bundle_root = executable_root.parent / "Resources"
    else:
        bundle_root = executable_root / "_internal" if (executable_root / "_internal").is_dir() else executable_root
    os.environ.setdefault("S2_RELEASE_MODE", "true")
    os.environ.setdefault("S2_RESOURCE_ROOT", str(bundle_root))
    if sys.platform == "darwin":
        default_data_root = Path.home() / "Library" / "Application Support" / "S2Dashboard"
    else:
        default_data_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "S2Dashboard"
    os.environ.setdefault("S2_USER_DATA_ROOT", str(default_data_root))
    os.environ.setdefault("S2_DASHBOARD_HOST", "127.0.0.1")
    os.environ.setdefault("S2_DASHBOARD_PORT", "8767")
    port = os.environ["S2_DASHBOARD_PORT"]
    os.environ.setdefault("S2_ALLOWED_ORIGINS", f"http://127.0.0.1:{port},http://localhost:{port}")
    os.environ.setdefault("S2_IDENTITY_MODE", "LOCAL_OWNER")


def main() -> None:
    _configure_release_environment()
    try:
        import uvicorn

        from backend.main import app

        host = os.environ["S2_DASHBOARD_HOST"]
        port = int(os.environ["S2_DASHBOARD_PORT"])

        def open_browser_when_ready() -> None:
            url = f"http://127.0.0.1:{port}"
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f"{url}/api/health", timeout=1):
                        webbrowser.open(url)
                        return
                except Exception:
                    time.sleep(0.5)

        if os.environ.get("S2_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes"}:
            threading.Thread(target=open_browser_when_ready, daemon=True).start()

        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
        )
    except Exception:
        # noconsole builds need a persistent diagnostic when a machine lacks
        # a bundled runtime component or a resource file is malformed.
        try:
            root = Path(os.environ["S2_USER_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            (root / "release_startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            raise


if __name__ == "__main__":
    main()
