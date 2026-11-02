"""qorqut-agentic: a standalone agentic layer (authored skills, tools, workflows).

Importing this package has no side effects.
"""

from .errors import ERROR_CODES, AgenticError
from .layer import AgenticLayer
from .catalog import Selection
from .runtime import ExecutionRequest, ExecutionResult, LocalProcessRuntime, ToolRuntime

__version__ = "0.2.0"
__all__ = ["AgenticLayer", "AgenticError", "ERROR_CODES", "Selection", "LocalProcessRuntime",
           "ToolRuntime", "ExecutionRequest", "ExecutionResult", "__version__"]
