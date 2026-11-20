"""Headless drivers for managed agents (see docs/MANAGED-RUNTIME.md)."""

from .claude_driver import ClaudeSession, ClaudeSessionError

__all__ = ["ClaudeSession", "ClaudeSessionError"]
