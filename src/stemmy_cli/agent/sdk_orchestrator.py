"""
Claude Agent SDK Orchestrator for Stemmy CLI.

Uses the official Claude Agent SDK with skills and proper tool handling.
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

console = Console()


# Define Stemmy-specific subagents
STEMMY_AGENTS = {
    "fragment-searcher": {
        "description": "Searches audio fragments by text content. Use when looking for specific words, phrases, or topics in podcast transcripts.",
        "prompt": """You are a fragment search specialist for the Stemmy audio platform.

Your task is to search the database for audio fragments matching user queries.

Available tools:
- search_fragments: Search fragment text
- get_database_stats: Get counts

When searching:
1. Use broad searches first, then narrow down
2. Report count and sample results
3. Note audio URLs for extraction

Be concise and focus on actionable results.""",
        "tools": ["Read", "Bash", "Glob"],
    },
    "entity-finder": {
        "description": "Finds named entities (people, companies, locations) in transcripts. Use when looking for mentions of specific people, organizations, or places.",
        "prompt": """You are an entity search specialist for Stemmy.

Your task is to find named entities in podcast transcripts.

Entity types available:
- person_name: People's names
- organization: Companies, institutions
- location: Places, cities, countries
- money_amount: Financial amounts
- date: Date mentions
- time: Time mentions

When searching:
1. Search by name or partial name
2. Filter by entity type if specified
3. Report confidence scores
4. Note timing information for extraction""",
        "tools": ["Read", "Bash", "Glob"],
    },
    "compilation-creator": {
        "description": "Creates audio compilations from fragments. Use when making supercuts, word compilations, or entity montages.",
        "prompt": """You are an audio compilation specialist.

Your task is to create audio compilations from podcast fragments.

Workflow:
1. Search for matching fragments/entities/words
2. Extract audio segments using precise timings
3. Concatenate clips into final compilation
4. Report output file location

Use the Stemmy CLI commands:
- stemmy fragments search "query"
- stemmy entities search "query"
- stemmy audio batch-extract --input segments.json
- stemmy audio concat --input-dir clips/

Focus on creating high-quality, well-timed compilations.""",
        "tools": ["Read", "Bash", "Glob", "Write"],
    },
    "lexical-analyzer": {
        "description": "Analyzes linguistic patterns in transcripts. Use for finding alliteration, word patterns, questions, conclusions, etc.",
        "prompt": """You are a lexical analysis specialist.

Available analysis modes:
- starts_with, ends_with, contains, exact, regex
- alliteration, palindrome, rhyme
- questions, exclamations, conclusions
- filler_words, formal, informal
- long_words, short_words

Use: stemmy analyze words --mode MODE --pattern PATTERN
Or: stemmy analyze text --mode MODE

Provide detailed analysis with examples.""",
        "tools": ["Read", "Bash", "Glob"],
    },
}


class SDKOrchestrator:
    """
    Orchestrates natural language requests using Claude Agent SDK.
    
    Provides access to Stemmy CLI capabilities through skills and subagents.
    """
    
    def __init__(
        self,
        model: str = "sonnet",
        verbose: bool = True,
    ):
        self.model = model
        self.verbose = verbose
        self.session_id: Optional[str] = None
        self.conversation_history: List[Dict[str, str]] = []
        
        # Get project root for skills
        from stemmy_cli.paths import get_project_root
        self.project_root = get_project_root()
        
        # Check API key
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not set. Please set it in .env or environment.")
    
    def _get_system_prompt(self) -> str:
        """Build comprehensive system prompt with Stemmy knowledge."""
        return """You are Stemmy AI, an intelligent assistant for the Stemmy audio content platform CLI.

## Your Capabilities

You have access to a powerful audio platform with:
- 19,810+ audio fragments from podcast transcripts
- 55,266+ named entities (people, companies, locations, etc.)
- 24 podcast formats with 237+ episodes
- 47 analysis modes for linguistic patterns

## Available Tools (via CLI)

### Search & Browse
- `stemmy fragments search "query"` - Search transcript text
- `stemmy entities search "query" [--type TYPE]` - Search named entities
- `stemmy formats list` - List all podcasts
- `stemmy items list --format-id ID` - List episodes

### Analysis (47 modes)
- `stemmy analyze words --mode MODE --pattern PATTERN`
  Modes: starts_with, ends_with, contains, alliteration, palindrome, long_words, etc.
- `stemmy analyze text --mode MODE`
  Modes: questions, exclamations, conclusions, filler_words, formal, informal, etc.

### Audio Operations
- `stemmy audio extract --url URL --start S --end E --output FILE`
- `stemmy audio batch-extract --input JSON --output-dir DIR`
- `stemmy audio concat --input-dir DIR --output FILE`

### Compilations
- `stemmy compilations create --type TYPE --query QUERY --output FILE`
  Types: word, entity, phrase, analysis

## Conversation Memory

Remember our conversation context. When I say "ja", "doe maar", "yes", or refer to previous results, use that context.

## Language

Match my language. Respond in Dutch when I speak Dutch, English when I speak English.

## Response Style

Be helpful but concise. For search results:
1. Report count found
2. Show representative samples (3-5)
3. Suggest next steps if applicable

For compilations:
1. Confirm what you'll create
2. Show progress
3. Report output file location"""

    async def execute_with_sdk(
        self,
        user_request: str,
    ) -> Dict[str, Any]:
        """
        Execute a request using Claude Agent SDK.
        
        This uses the official SDK with skills and proper tool handling.
        """
        try:
            from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition
        except ImportError:
            console.print("[yellow]Claude Agent SDK not available, falling back to direct API[/yellow]")
            return await self._execute_fallback(user_request)
        
        # Build agent definitions from our config
        agents = {}
        for name, config in STEMMY_AGENTS.items():
            agents[name] = AgentDefinition(
                description=config["description"],
                prompt=config["prompt"],
                tools=config["tools"],
            )
        
        options = ClaudeAgentOptions(
            cwd=str(self.project_root),
            setting_sources=["project"],  # Load skills from .claude/skills/
            allowed_tools=["Skill", "Read", "Bash", "Glob", "Grep", "Task"],
            agents=agents,
            system_prompt=self._get_system_prompt(),
        )
        
        # Add conversation context
        if self.conversation_history:
            context = "\n\nConversation context:\n"
            for msg in self.conversation_history[-6:]:
                context += f"{msg['role'].upper()}: {msg['content'][:200]}\n"
            user_request = context + "\n\nCurrent request: " + user_request
        
        results = []
        response_text = ""
        
        try:
            if self.verbose:
                console.print("[dim]Connecting to Claude...[/dim]")
            
            async for message in query(
                prompt=user_request,
                options=options,
            ):
                # Handle different message types
                if hasattr(message, "content") and message.content:
                    for block in message.content:
                        if hasattr(block, "text"):
                            response_text += block.text
                            if self.verbose:
                                console.print(block.text, end="")
                        elif hasattr(block, "name"):
                            if self.verbose:
                                console.print(f"[cyan]→ {block.name}[/cyan]")
                            results.append({"tool": block.name})
                
                if hasattr(message, "result"):
                    response_text = message.result
                
                if hasattr(message, "session_id"):
                    self.session_id = message.session_id
            
            # Update conversation history
            self.conversation_history.append({"role": "user", "content": user_request})
            if response_text:
                self.conversation_history.append({"role": "assistant", "content": response_text})
            
            # Trim history
            if len(self.conversation_history) > 20:
                self.conversation_history = self.conversation_history[-20:]
            
            return {
                "response": response_text,
                "results": results,
                "session_id": self.session_id,
            }
            
        except Exception as e:
            console.print(f"[red]SDK Error: {e}[/red]")
            # Fall back to direct API
            return await self._execute_fallback(user_request)
    
    async def _execute_fallback(
        self,
        user_request: str,
    ) -> Dict[str, Any]:
        """Fallback to direct Anthropic API when SDK not available."""
        from stemmy_cli.agent.orchestrator import Orchestrator
        
        orch = Orchestrator(provider="anthropic")
        orch.conversation_history = self.conversation_history
        
        result = orch.execute(user_request, verbose=self.verbose)
        
        self.conversation_history = orch.conversation_history
        
        return result
    
    def execute(
        self,
        user_request: str,
        verbose: bool = True,
    ) -> Dict[str, Any]:
        """Synchronous wrapper for execute_with_sdk."""
        self.verbose = verbose
        return asyncio.run(self.execute_with_sdk(user_request))
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        self.session_id = None
    
    def print_result(self, result: Dict[str, Any]):
        """Pretty-print an execution result."""
        if result.get("results"):
            console.print()
            console.print("[bold]Tools used:[/bold]")
            for r in result["results"]:
                console.print(f"  [green]✓[/green] {r.get('tool', 'unknown')}")
        
        if result.get("response"):
            console.print()
            panel = Panel(
                result["response"],
                title="[bold cyan]Stemmy AI[/bold cyan]",
                border_style="cyan",
            )
            console.print(panel)


def create_orchestrator(use_sdk: bool = True, **kwargs) -> Any:
    """
    Factory function to create the appropriate orchestrator.
    
    Args:
        use_sdk: Whether to use Claude Agent SDK (default True)
        **kwargs: Additional arguments passed to orchestrator
    
    Returns:
        SDKOrchestrator or Orchestrator instance
    """
    if use_sdk:
        try:
            return SDKOrchestrator(**kwargs)
        except ImportError:
            pass
    
    from stemmy_cli.agent.orchestrator import Orchestrator
    return Orchestrator(**kwargs)
