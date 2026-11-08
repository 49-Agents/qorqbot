"""Project preparation and small Claude hook/header adapters, never a runner."""

from .preparation import prepare_agent, prepare_claude_agent, compatibility_report

__all__ = ["prepare_agent", "prepare_claude_agent", "compatibility_report"]
