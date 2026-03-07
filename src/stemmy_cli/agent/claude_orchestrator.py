"""
Full Claude Agent SDK Orchestrator for Stemmy CLI.

Uses the official Claude Agent SDK with:
- AskUserQuestion for clarification
- Streaming with partial messages for progress
- Hooks for monitoring
- Skills and subagents
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.text import Text
from rich.table import Table

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

console = Console()


# Stemmy-specific agent definitions
STEMMY_AGENTS = {
    "fragment-searcher": {
        "description": "Searches audio fragments by text content. Use for finding words, phrases, or topics in transcripts.",
        "prompt": """You are a fragment search specialist for Stemmy audio platform.
Search the database for audio fragments matching user queries.
Report count and sample results. Note audio URLs for extraction.""",
        "tools": ["Read", "Bash", "Glob"],
    },
    "entity-finder": {
        "description": "Finds named entities (people, companies, locations) in transcripts.",
        "prompt": """You are an entity search specialist for Stemmy.
Find named entities: person_name, organization, location, money_amount, date, time.
Report confidence scores and timing for extraction.""",
        "tools": ["Read", "Bash", "Glob"],
    },
    "compilation-creator": {
        "description": "Creates audio compilations from fragments. Use for supercuts, word compilations, entity montages.",
        "prompt": """You are an audio compilation specialist.
Workflow: Search matching content → Extract audio → Concatenate → Report output path.
Use stemmy CLI commands and report the final MP3 file location.""",
        "tools": ["Read", "Bash", "Glob", "Write"],
    },
    "lexical-analyzer": {
        "description": "Analyzes linguistic patterns: alliteration, word patterns, questions, conclusions.",
        "prompt": """You are a lexical analysis specialist.
Available modes: starts_with, ends_with, contains, alliteration, palindrome, questions, exclamations, conclusions, filler_words.
Use: stemmy analyze words/text --mode MODE""",
        "tools": ["Read", "Bash", "Glob"],
    },
}


class ClaudeOrchestrator:
    """
    Full Claude Agent SDK orchestrator with rich features.
    
    Features:
    - AskUserQuestion for clarification
    - Streaming progress with thinking trace
    - Hooks for monitoring
    - Skills from .claude/skills/
    - Subagents for specialized tasks
    """
    
    def __init__(
        self,
        verbose: bool = True,
        show_thinking: bool = True,
        clarify_questions: bool = True,
    ):
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.clarify_questions = clarify_questions
        self.session_id: Optional[str] = None
        self.conversation_history: List[Dict[str, str]] = []
        from stemmy_cli.paths import get_project_root
        self.project_root = get_project_root()
        
        # Check API key - prefer Anthropic, but can work with OpenAI fallback
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        self.openai_key = os.environ.get("OPENAI_API_KEY")
        
        if not self.anthropic_key and not self.openai_key:
            raise ValueError("No API key set (ANTHROPIC_API_KEY or OPENAI_API_KEY)")
        
        self.use_sdk = bool(self.anthropic_key)  # SDK needs Anthropic key
        self._client = None
        self._thinking_lines: List[str] = []
    
    def _get_system_prompt(self) -> str:
        """Comprehensive system prompt for Stemmy."""
        return """You are Stemmy AI, an intelligent assistant for the Stemmy audio content platform CLI.

## CRITICAL RULES - READ CAREFULLY!

1. **EXECUTE, DON'T EXPLAIN** - When user asks for a compilation, USE the tools. NEVER give instructions.
2. **WORD EXTRACTION** - When user wants "only the words" (alleen de woorden), set compilation_type="word" and use_word_timing=True
3. **PODCAST FILTER** - When user mentions a specific podcast (e.g., "POM", "HKU en AI"), FIRST use list_formats to find the format_id, then filter with format_id parameter
4. **MUSIC MIXING** - ALWAYS use `mix_with_background_music` (NOT layer_audio_tracks) for adding background music. Music files are in static/music/

## Available Music Files
- static/music/cinematic1.mp3
- static/music/crime1.mp3  
- static/music/horror1.mp3, horror2.mp3
- static/music/interviewmusic1-5.mp3

## Database Overview
- ~19,800 fragments with WORD-LEVEL TIMING (can extract individual words!)
- ~55,000 entities (people, companies, locations)
- 24 formats (podcasts), 237+ episodes

## TOOLS - USE THESE!

### COMPILATION TOOLS (Create MP3 files!)
- **create_compilation**: Search + extract → MP3
  - compilation_type: "word" (extract exact words) or "entity" (named entities) or "fragment" (whole sentences)
  - format_id: Filter to specific podcast (get from list_formats first!)
  - use_word_timing: true = extract only the word, false = extract whole fragment
  
- **create_shuffled_compilation**: Multiple queries shuffled together
  - queries: ["Google", "Microsoft", "Tesla"]
  - format_id: Filter to specific podcast!
  
### MUSIC TOOLS
- **mix_with_background_music**: Add background music professionally
  - voice_track: path to compilation
  - music_track: path to music file (e.g., "static/music/interviewmusic1.mp3")
  - Auto-applies: 15% volume, fades, length matching, ducking
  
### SEARCH TOOLS  
- **search_fragments**: Search text, returns timing for extraction
- **search_entities**: Search named entities (person_name, organization, location, etc.)
- **list_formats**: List all podcasts (use to get format_id!)
- **get_format_items**: Get episodes for a format

### EFFECT TOOLS
- **apply_audio_effects**: Add fade, reverb, normalize, speed change
- **create_dj_compilation**: Preset effects (hype, smooth, dramatic, chaos)

## EXAMPLES

User: "compilatie van woorden 'AI' in podcast POM"
1. list_formats() → find POM format_id
2. create_compilation(query="AI", compilation_type="word", use_word_timing=true, format_id="xxx")
→ Returns MP3 with just the word "AI" extracted many times

User: "compilatie met muziek eronder"  
1. create_shuffled_compilation(...) → creates compilation.mp3
2. mix_with_background_music(voice_track="compilation.mp3", music_track="static/music/interviewmusic1.mp3")
→ Returns professional mix with faded background music

User: "zoek Google mentions alleen in HKU podcast"
1. list_formats() → find HKU format_id  
2. search_entities(query="Google", format_id="xxx")

## LANGUAGE
Match the user's language (Dutch/English). Be concise."""

    async def _ensure_sdk(self):
        """Lazy import and check SDK availability."""
        if not self.use_sdk:
            return False
        try:
            from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition
            return True
        except ImportError:
            return False
    
    async def execute_with_progress(
        self,
        user_request: str,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_tool_use: Optional[Callable[[str, dict], None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute request with real-time progress updates.
        
        Args:
            user_request: The user's natural language request
            on_thinking: Callback for thinking updates
            on_tool_use: Callback for tool use updates
        """
        if not await self._ensure_sdk():
            # Fallback to basic orchestrator - run in thread to avoid asyncio conflict
            import concurrent.futures
            from stemmy_cli.agent.orchestrator import Orchestrator
            
            def run_sync():
                orch = Orchestrator()
                orch.conversation_history = self.conversation_history.copy()
                result = orch.execute(user_request, verbose=self.verbose)
                return result, orch.conversation_history
            
            loop = asyncio.get_event_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result, history = await loop.run_in_executor(pool, run_sync)
            
            self.conversation_history = history
            return result
        
        from claude_agent_sdk import (
            ClaudeSDKClient, 
            ClaudeAgentOptions, 
            AgentDefinition,
            AssistantMessage,
            TextBlock,
            ThinkingBlock,
            ToolUseBlock,
            ToolResultBlock,
            ResultMessage,
        )
        
        try:
            from claude_agent_sdk.types import StreamEvent
        except ImportError:
            StreamEvent = None
        
        # Build agent definitions
        agents = {}
        for name, config in STEMMY_AGENTS.items():
            agents[name] = AgentDefinition(
                description=config["description"],
                prompt=config["prompt"],
                tools=config["tools"],
            )
        
        options = ClaudeAgentOptions(
            cwd=str(self.project_root),
            setting_sources=["project"],
            allowed_tools=[
                "Skill", "Read", "Bash", "Glob", "Grep", "Task",
                "Write", "Edit", "AskUserQuestion"
            ],
            agents=agents,
            system_prompt=self._get_system_prompt(),
            include_partial_messages=self.show_thinking,
            permission_mode="acceptEdits",
        )
        
        # Add conversation context
        context_prompt = user_request
        if self.conversation_history:
            context = "\n\nConversation context:\n"
            for msg in self.conversation_history[-6:]:
                context += f"{msg['role'].upper()}: {msg['content'][:200]}\n"
            context_prompt = context + "\n\nCurrent request: " + user_request
        
        results = []
        response_text = ""
        tools_used = []
        thinking_text = ""
        usage_info = {}
        
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(context_prompt)
                
                async for message in client.receive_response():
                    # Handle streaming events (partial messages)
                    if StreamEvent and isinstance(message, StreamEvent):
                        event = message.event
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "thinking_delta":
                                thinking_chunk = delta.get("thinking", "")
                                thinking_text += thinking_chunk
                                if on_thinking and thinking_chunk:
                                    on_thinking(thinking_chunk)
                            elif delta.get("type") == "text_delta":
                                text_chunk = delta.get("text", "")
                                response_text += text_chunk
                        continue
                    
                    # Handle complete messages
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                if not response_text:
                                    response_text = block.text
                                else:
                                    response_text += block.text
                            elif isinstance(block, ThinkingBlock):
                                thinking_text += block.thinking
                                if on_thinking:
                                    on_thinking(block.thinking)
                            elif isinstance(block, ToolUseBlock):
                                tools_used.append({
                                    "name": block.name,
                                    "input": block.input,
                                })
                                if on_tool_use:
                                    on_tool_use(block.name, block.input)
                            elif isinstance(block, ToolResultBlock):
                                results.append({
                                    "tool_use_id": block.tool_use_id,
                                    "content": block.content,
                                    "is_error": block.is_error,
                                })
                    
                    elif isinstance(message, ResultMessage):
                        self.session_id = message.session_id
                        if message.result:
                            response_text = message.result
                        # Extract usage and cost info
                        usage_info = {
                            "total_cost_usd": message.total_cost_usd,
                            "duration_ms": message.duration_ms,
                            "num_turns": message.num_turns,
                        }
                        if message.usage:
                            usage_info.update({
                                "input_tokens": message.usage.get("input_tokens", 0),
                                "output_tokens": message.usage.get("output_tokens", 0),
                                "cache_read_tokens": message.usage.get("cache_read_input_tokens", 0),
                            })
            
            # Update conversation history
            self.conversation_history.append({"role": "user", "content": user_request})
            if response_text:
                self.conversation_history.append({"role": "assistant", "content": response_text[:500]})
            
            # Trim history
            if len(self.conversation_history) > 20:
                self.conversation_history = self.conversation_history[-20:]
            
            return {
                "response": response_text,
                "results": results,
                "tools_called": tools_used,
                "tool_results": [r["content"] for r in results],
                "thinking": thinking_text,
                "session_id": self.session_id,
                "usage": usage_info,
            }
            
        except Exception as e:
            console.print(f"[red]SDK Error: {e}[/red]")
            # Fallback
            from stemmy_cli.agent.orchestrator import Orchestrator
            orch = Orchestrator()
            orch.conversation_history = self.conversation_history
            result = orch.execute(user_request, verbose=self.verbose)
            self.conversation_history = orch.conversation_history
            return result
    
    def execute(
        self,
        user_request: str,
        verbose: bool = True,
    ) -> Dict[str, Any]:
        """
        Synchronous execute with rich progress display.
        """
        self.verbose = verbose
        
        thinking_lines = []
        current_tool = None
        
        def on_thinking(text: str):
            nonlocal thinking_lines
            lines = text.strip().split('\n')
            for line in lines:
                if line.strip():
                    thinking_lines.append(line.strip()[:80])
                    if len(thinking_lines) > 5:
                        thinking_lines = thinking_lines[-5:]
        
        def on_tool_use(name: str, input_data: dict):
            nonlocal current_tool
            current_tool = name
        
        # Run with progress display
        async def run_with_display():
            if verbose and self.show_thinking:
                with Live(console=console, refresh_per_second=4) as live:
                    async def update_display():
                        while True:
                            table = Table(show_header=False, box=None, padding=(0, 1))
                            table.add_column(style="dim")
                            
                            if current_tool:
                                table.add_row(f"[cyan]→ {current_tool}[/cyan]")
                            
                            for line in thinking_lines[-3:]:
                                table.add_row(f"[dim]{line}[/dim]")
                            
                            if not thinking_lines and not current_tool:
                                table.add_row("[dim]Thinking...[/dim]")
                            
                            live.update(table)
                            await asyncio.sleep(0.25)
                    
                    # Run both concurrently
                    display_task = asyncio.create_task(update_display())
                    try:
                        result = await self.execute_with_progress(
                            user_request,
                            on_thinking=on_thinking,
                            on_tool_use=on_tool_use,
                        )
                    finally:
                        display_task.cancel()
                        try:
                            await display_task
                        except asyncio.CancelledError:
                            pass
                    
                    return result
            else:
                return await self.execute_with_progress(user_request)
        
        return asyncio.run(run_with_display())
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        self.session_id = None
    
    def print_result(self, result: Dict[str, Any]):
        """Pretty-print execution result."""
        # Show tools used
        if result.get("tools_called"):
            console.print()
            console.print("[bold]Tools used:[/bold]")
            for tool in result["tools_called"][:5]:
                name = tool.get("name", "unknown")
                console.print(f"  [green]✓[/green] {name}")
        
        # Show thinking summary if available
        if result.get("thinking") and self.show_thinking:
            thinking = result["thinking"]
            if len(thinking) > 200:
                thinking = thinking[:200] + "..."
            console.print()
            console.print(Panel(
                thinking,
                title="[dim]Thinking[/dim]",
                border_style="dim",
                padding=(0, 1),
            ))
        
        # Main response
        if result.get("response"):
            console.print()
            console.print(Panel(
                result["response"],
                title="[bold cyan]Stemmy AI[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            ))
        
        # Show usage/cost info
        usage = result.get("usage", {})
        if usage:
            parts = []
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_tokens = input_tokens + output_tokens
            if total_tokens > 0:
                parts.append(f"Tokens: {total_tokens:,}")
            cost = usage.get("total_cost_usd")
            if cost and cost > 0:
                parts.append(f"Cost: ${cost:.4f}")
            duration = usage.get("duration_ms")
            if duration:
                parts.append(f"Time: {duration/1000:.1f}s")
            if parts:
                console.print(f"[dim]{' | '.join(parts)}[/dim]")


def create_orchestrator(
    use_sdk: bool = True,
    show_thinking: bool = True,
    **kwargs
) -> Any:
    """Factory to create the best available orchestrator."""
    if use_sdk:
        try:
            return ClaudeOrchestrator(show_thinking=show_thinking, **kwargs)
        except Exception:
            pass
    
    from stemmy_cli.agent.orchestrator import Orchestrator
    return Orchestrator(**kwargs)
