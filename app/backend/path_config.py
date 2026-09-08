"""Resource and writable-data roots for local and packaged deployments."""
from __future__ import annotations

import os
from pathlib import Path


def resource_root() -> Path:
    """Return the read-only application resource root."""
    default = Path(__file__).resolve().parents[1]
    return Path(os.environ.get("S2_RESOURCE_ROOT", str(default))).absolute()


def release_mode() -> bool:
    return os.environ.get("S2_RELEASE_MODE", "false").casefold() == "true"


def user_data_root() -> Path:
    """Return the writable per-install user-data root.

    Existing development/production launches retain their historical paths.
    A packaged launch opts in explicitly through S2_RELEASE_MODE and may
    override the location for portable or test installations.
    """
    configured = os.environ.get("S2_USER_DATA_ROOT")
    if configured:
        return Path(configured).absolute()
    if not release_mode():
        return resource_root() / "data" / "multi_user"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "S2Dashboard"


def writable_logs_root() -> Path:
    if not release_mode():
        return resource_root() / "logs"
    return user_data_root() / "logs"


def writable_runtime_root() -> Path:
    if not release_mode():
        return resource_root() / "runtime"
    return user_data_root() / "runtime"
