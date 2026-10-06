"""M04 local preparation/evidence only; native acceptance remains pending."""

from .clients import ClaudeAdapter, CodexAdapter
from .types import (AdapterError, ManagedRoots, MCPBinding, NativeBinding,
    PreparationInput, RunBinding)

__all__ = ["ClaudeAdapter", "CodexAdapter", "AdapterError", "ManagedRoots", "MCPBinding",
    "NativeBinding", "PreparationInput", "RunBinding"]
