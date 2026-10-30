"""Small, per-user preferences independent of an EXE's installation folder."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def settings_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".config"))
    return base / "PaperDownloader" / "settings.json"


def load_settings() -> dict[str, str]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, str)}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict[str, str]) -> None:
    destination = settings_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix="settings_", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
