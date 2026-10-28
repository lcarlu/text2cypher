"""Tool registry.

Core tools live in graph_tools.py. To add your own tools, drop a module in tools/custom/ exposing
`create_tools(ctx: ToolContext) -> list[BaseTool]` (see tools/custom/_template.py): it is discovered at startup.
Modules whose name starts with "_" are ignored.
"""

import importlib
import logging
import pkgutil

from langchain_core.tools import BaseTool

from text2cypher_backend.tools import custom, graph_tools
from text2cypher_backend.tools.context import QueryTrace, ToolContext

logger = logging.getLogger(__name__)

__all__ = ["QueryTrace", "ToolContext", "build_tools", "custom_tool_modules"]


def custom_tool_modules() -> list:
    modules = []
    for info in pkgutil.iter_modules(custom.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{custom.__name__}.{info.name}")
        if hasattr(module, "create_tools"):
            modules.append(module)
        else:
            logger.warning("tools/custom/%s.py has no create_tools(ctx) function: ignored", info.name)
    return modules


def build_tools(ctx: ToolContext) -> list[BaseTool]:
    tools = graph_tools.create_tools(ctx)
    for module in custom_tool_modules():
        tools.extend(module.create_tools(ctx))
    names = [t.name for t in tools]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"Duplicate tool names: {duplicates}")
    return tools
