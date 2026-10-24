"""Importing this package registers every tool on REGISTRY."""

from . import browser, control, desktop, system, vision  # noqa: F401
from .base import REGISTRY, ToolResult

__all__ = ["REGISTRY", "ToolResult"]
