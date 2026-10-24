"""Guard rails for irreversible actions.

Rules are data in ``<workspace>/config/policy.yaml``. A ``deny`` match refuses
the action; a ``confirm`` match turns it into a human approval request. Once
approved, the exact same action (by fingerprint) is allowed for that run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT, get_settings, load_workspace_yaml

DEFAULT_POLICY = {
    "shell": {
        "deny": [
            r"\bformat(\.com)?\s+[a-z]:",
            r"\bdiskpart\b",
            r"\bbcdedit\b",
            r"\bcipher\s+/w",
            r"\breg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)",
            r"Remove-Item\b.*\b(C:\\Windows|C:\\Program Files)",
            r"\brm\s+-rf\s+/(\s|$)",
            r"Set-MpPreference|Disable-WindowsOptionalFeature|netsh\s+advfirewall",
        ],
        "confirm": [
            r"\bRemove-Item\b", r"\brm\s", r"\bdel\s", r"\brmdir\b", r"\brd\s",
            r"\bgit\s+push\b", r"\bStop-Computer\b", r"\bRestart-Computer\b", r"\bshutdown\b",
            r"\bStop-Process\b", r"\btaskkill\b", r"\bInvoke-WebRequest\b.*-Method\s+(Post|Put|Delete)",
        ],
    },
    "files": {
        "deny_write_under": [r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)"],
        # The frozen harness code must never be modified by the agent itself.
        "protect_code": True,
        "confirm_delete": True,
    },
}


@dataclass
class Verdict:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""


def fingerprint(tool: str, args: dict) -> str:
    blob = json.dumps({"t": tool, "a": args}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


class Policy:
    def __init__(self, rules: dict | None = None) -> None:
        self.rules = rules or load_workspace_yaml("policy.yaml", DEFAULT_POLICY)
        self.approved: set[str] = set()

    def check_shell(self, command: str, fp: str) -> Verdict:
        rules = self.rules.get("shell", {})
        for pat in rules.get("deny", []):
            if re.search(pat, command, re.I):
                return Verdict(False, reason=f"command matches deny rule /{pat}/")
        if fp in self.approved:
            return Verdict(True)
        for pat in rules.get("confirm", []):
            if re.search(pat, command, re.I):
                return Verdict(False, needs_approval=True, reason=f"command matches confirm rule /{pat}/")
        return Verdict(True)

    def check_write(self, path: Path, fp: str, delete: bool = False) -> Verdict:
        rules = self.rules.get("files", {})
        resolved = str(path.resolve()).lower()
        for root in rules.get("deny_write_under", []):
            if resolved.startswith(root.lower().rstrip("\\") + "\\"):
                return Verdict(False, reason=f"writes under {root} are not allowed")
        if rules.get("protect_code", True):
            repo = str(REPO_ROOT.resolve()).lower()
            workspace = str(get_settings().workspace).lower()
            if (resolved == repo or resolved.startswith(repo + "\\")) and not resolved.startswith(workspace):
                return Verdict(False, reason="the harness code is frozen and cannot be modified by the agent")
        if delete and rules.get("confirm_delete", True) and fp not in self.approved:
            return Verdict(False, needs_approval=True, reason=f"deleting {path}")
        return Verdict(True)

    def approve(self, fp: str) -> None:
        self.approved.add(fp)
