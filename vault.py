from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict

NAMES = ("ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "GROQ_API_KEY", "DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_FORUM_CHANNEL_ID", "DISCORD_ALERTS_CHANNEL_ID",
         "DISCORD_BILAN_CHANNEL_ID", "DISCORD_ITEMS_CHANNEL_ID", "DISCORD_ALERT_ROLE_ID")
MAX_LENGTH = 300


class Vault:
    def __init__(self, path: Any) -> None:
        self.path, self.lock = Path(path), threading.RLock()

    def stored(self) -> Dict[str, str]:
        with self.lock:
            if not self.path.exists():
                return {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in NAMES and isinstance(v, str)}

    def get(self, name: str) -> str:
        return os.environ.get(name, "").strip() or self.stored().get(name, "").strip()

    def merged(self) -> Dict[str, str]:
        return {name: value for name in NAMES if (value := self.get(name))}

    def status(self) -> Dict[str, bool]:
        return {name: bool(self.get(name)) for name in NAMES}

    def set(self, name: Any, value: Any) -> None:
        if name not in NAMES:
            raise ValueError("Clé inconnue.")
        text = str(value or "").strip()
        if len(text) > MAX_LENGTH or any(c in text for c in "\r\n\x00"):
            raise ValueError("Valeur invalide.")
        with self.lock:
            data = self.stored()
            if text:
                data[name] = text
            else:
                data.pop(name, None)
            self.write(data)

    def write(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output)
            os.replace(str(tmp), str(self.path))
        finally:
            if tmp.exists():
                tmp.unlink()
