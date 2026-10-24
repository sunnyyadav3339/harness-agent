"""Secrets and vault templating.

The model never sees secret values. It writes placeholders such as
``{{secret.IRCTC_PASSWORD}}`` into tool arguments; the placeholder is resolved
only at the moment the tool runs, and every log line is redacted afterwards.
"""

from __future__ import annotations

import os
import re
from typing import Any

SECRET_RE = re.compile(r"\{\{\s*secret\.([A-Za-z0-9_]+)\s*\}\}")
VAULT_RE = re.compile(r"\{\{\s*vault\.([A-Za-z0-9_.\-]+)\s*\}\}")
KEYRING_SERVICE = "agenticps"


class MissingSecret(KeyError):
    pass


class SecretStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def get(self, name: str) -> str | None:
        name = name.upper()
        value = os.environ.get(f"SECRET_{name}")
        if not value:
            try:
                import keyring

                value = keyring.get_password(KEYRING_SERVICE, name)
            except Exception:
                value = None
        if value:
            self._seen.add(value)
        return value or None

    def available_names(self) -> list[str]:
        names = {k[len("SECRET_"):] for k, v in os.environ.items() if k.startswith("SECRET_") and v}
        return sorted(names)

    def resolve(self, text: str) -> str:
        def _sub(m: re.Match) -> str:
            value = self.get(m.group(1))
            if value is None:
                raise MissingSecret(m.group(1))
            return value

        return SECRET_RE.sub(_sub, text)

    def redact(self, text: str) -> str:
        if not text:
            return text
        values = set(self._seen)
        values.update(v for k, v in os.environ.items() if k.startswith("SECRET_") and v)
        # Longest first so a secret containing another is fully masked.
        for value in sorted(values, key=len, reverse=True):
            if len(value) >= 3:
                text = text.replace(value, "***")
        return text


def lookup(data: Any, dotted: str) -> Any:
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(dotted)
    return cur


def render_vault(text: str, vault: dict) -> str:
    def _sub(m: re.Match) -> str:
        try:
            return str(lookup(vault, m.group(1)))
        except KeyError:
            return m.group(0)  # leave unresolved; the tool reports it

    return VAULT_RE.sub(_sub, text)


def render_args(args: Any, vault: dict, secrets: SecretStore) -> Any:
    """Resolve vault and secret placeholders recursively inside tool args."""
    if isinstance(args, str):
        return secrets.resolve(render_vault(args, vault))
    if isinstance(args, dict):
        return {k: render_args(v, vault, secrets) for k, v in args.items()}
    if isinstance(args, list):
        return [render_args(v, vault, secrets) for v in args]
    return args


SECRETS = SecretStore()
