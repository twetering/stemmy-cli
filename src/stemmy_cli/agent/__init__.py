"""
Agent module for AI-powered CLI interactions.

Provides tool registry and LLM orchestration for natural language commands.
"""

from stemmy_cli.agent.tools import TOOL_REGISTRY, get_tool, list_tools, execute_tool
from stemmy_cli.agent.orchestrator import Orchestrator

__all__ = [
    "TOOL_REGISTRY",
    "get_tool",
    "list_tools", 
    "execute_tool",
    "Orchestrator",
]
