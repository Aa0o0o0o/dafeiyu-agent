"""Single registered inventory for model tool schemas and capability synchronization."""

from .web_tools import WEB_TOOLS
from .qq_profiles import PROFILE_TOOL
from .browser.tools import BROWSER_TOOLS
from .qq_expressions import EXPRESSION_TOOL

MODEL_TOOLS = WEB_TOOLS + [PROFILE_TOOL] + BROWSER_TOOLS + [EXPRESSION_TOOL]
MODEL_TOOL_NAMES = frozenset(t["function"]["name"] for t in MODEL_TOOLS)
