"""qorqut-agentic: a standalone agentic layer (authored skills, tools, workflows).

Importing this package has no side effects.
"""

from .errors import ERROR_CODES, AgenticError
from .layer import AgenticLayer

__version__ = "0.1.0"
__all__ = ["AgenticLayer", "AgenticError", "ERROR_CODES", "__version__"]
