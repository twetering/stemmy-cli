"""
LLM Orchestrator for natural language CLI interactions.

Translates natural language requests into tool calls and executes them.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from stemmy_cli.agent.tools import (
    TOOL_REGISTRY,
    execute_tool,
    get_openai_tools_schema,
)

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

console = Console()


class Orchestrator:
    """
    Orchestrates natural language command execution.
    
    Supports multiple LLM backends: OpenAI, Anthropic Claude, local Ollama.
    """
    
    def __init__(
        self,
        provider: str = "auto",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        # Auto-detect provider based on available API keys
        if provider == "auto":
            if os.environ.get("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif os.environ.get("OPENAI_API_KEY"):
                provider = "openai"
            else:
                provider = "openai"  # Default fallback
        
        self.provider = provider
        self.api_key = api_key or self._get_api_key(provider)
        
        if not self.api_key:
            raise ValueError(f"No API key found for provider '{provider}'. Set OPENAI_API_KEY or ANTHROPIC_API_KEY.")
        
        # Check for MODEL env var first, then use defaults
        env_model = os.environ.get("MODEL", "").strip().strip("'\"")
        
        # Default models per provider (only used if no MODEL env var)
        default_models = {
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-6",
            "ollama": "llama3.2",
        }
        
        # Priority: explicit model arg > MODEL env var > provider default
        # Use model names exactly as provided - no mapping
        if model:
            self.model = model
        elif env_model:
            self.model = env_model
        else:
            self.model = default_models.get(provider, "gpt-4o")
        
        self.tools_schema = get_openai_tools_schema()
        self.conversation_history: List[Dict[str, Any]] = []
    
    def _get_api_key(self, provider: str) -> Optional[str]:
        """Get API key from environment."""
        key_vars = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        var = key_vars.get(provider)
        return os.environ.get(var) if var else None
    
    def _get_system_prompt(self) -> str:
        """Build comprehensive system prompt with full Stemmy knowledge."""
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

### SEARCH TOOLS
- **search_fragments**: Keyword search (exact text match)
- **search_semantic**: Find by MEANING - use for conceptual queries like "passionate debates", "skeptical reactions", "discussions about AI". Returns conceptually similar fragments even without exact words.
- **search_entities**: Named entities (people, companies, locations)

### COMPILATION TOOLS (Create MP3 files!)
- **create_compilation**: Search + extract → MP3
  - search_mode: "text" (keyword) or "semantic" (meaning-based) - use semantic for conceptual queries!
  - compilation_type: "word" (extract exact words) or "entity" (named entities) or "fragment" (whole sentences)
  - format_id: Filter to specific podcast (get from list_formats first!)
  - use_word_timing: true = extract only the word, false = extract whole fragment
  
- **create_shuffled_compilation**: Multiple queries shuffled together
  - search_mode: "semantic" for conceptual queries (e.g. ["passionate debates", "skeptical reactions"])
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

    async def _call_openai(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        """Call OpenAI API with function calling."""
        import httpx
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": self.tools_schema,
                    "tool_choice": "auto",
                },
                timeout=60.0,
            )
            if response.status_code != 200:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", str(error_data))
                except Exception:
                    error_msg = response.text[:500]
                raise Exception(f"OpenAI API error ({response.status_code}): {error_msg}")
            data = response.json()
        
        choice = data["choices"][0]
        message = choice["message"]
        
        tool_calls = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                tool_calls.append({
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": json.loads(tc["function"]["arguments"]),
                })
        
        # Extract usage info
        usage = data.get("usage", {})
        usage_info = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "model": self.model,
        }
        
        return message.get("content", ""), tool_calls, usage_info
    
    async def _call_anthropic(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        """Call Anthropic Claude API with tool use."""
        import httpx
        
        # Convert OpenAI tool schema to Anthropic format
        anthropic_tools = []
        for tool in self.tools_schema:
            func = tool["function"]
            anthropic_tools.append({
                "name": func["name"],
                "description": func["description"],
                "input_schema": func["parameters"],
            })
        
        # Convert messages to Anthropic format
        anthropic_messages = []
        pending_tool_results = []
        
        for msg in messages:
            if msg["role"] == "system":
                continue  # System is handled separately
            
            if msg["role"] == "tool":
                # Anthropic expects tool results in a different format
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id"),
                    "content": msg.get("content", ""),
                })
                continue
            
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                # Convert OpenAI tool_calls format to Anthropic content blocks
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    })
                anthropic_messages.append({
                    "role": "assistant",
                    "content": content_blocks,
                })
                continue
            
            # If we have pending tool results, add them as user message
            if pending_tool_results:
                anthropic_messages.append({
                    "role": "user",
                    "content": pending_tool_results,
                })
                pending_tool_results = []
            
            # Regular message
            anthropic_messages.append({
                "role": msg["role"],
                "content": msg["content"] if msg["content"] else "",
            })
        
        # Add any remaining tool results
        if pending_tool_results:
            anthropic_messages.append({
                "role": "user", 
                "content": pending_tool_results,
            })
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 4096,
                    "system": self._get_system_prompt(),
                    "messages": anthropic_messages,
                    "tools": anthropic_tools,
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
        
        # Parse response
        content_text = ""
        tool_calls = []
        
        for block in data.get("content", []):
            if block["type"] == "text":
                content_text += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": block["input"],
                })
        
        # Extract usage info
        usage = data.get("usage", {})
        usage_info = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "model": self.model,
        }
        
        return content_text, tool_calls, usage_info
    
    def _call_llm_sync(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        """Synchronous wrapper for LLM calls."""
        import asyncio
        
        if self.provider == "openai":
            coro = self._call_openai(messages)
        elif self.provider == "anthropic":
            coro = self._call_anthropic(messages)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")
        
        return asyncio.run(coro)
    
    def plan(self, user_request: str) -> List[Dict[str, Any]]:
        """
        Generate a plan of tool calls for a user request.
        
        Returns list of planned tool calls without executing them.
        """
        messages = [
            {"role": "system", "content": self._get_system_prompt()},
            {"role": "user", "content": user_request},
        ]
        
        _, tool_calls, _ = self._call_llm_sync(messages)
        return tool_calls
    
    def execute(
        self,
        user_request: str,
        verbose: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a natural language request with conversation memory.
        
        Conversation history is kept as simple user/assistant text pairs.
        Tool calls happen within each request but are summarized in history.
        
        Returns:
            Dict with 'response' (LLM text) and 'results' (tool outputs)
        """
        import time
        start_time = time.time()
        
        # Build initial messages: system + conversation history + new user message
        base_messages = [
            {"role": "system", "content": self._get_system_prompt()},
        ]
        
        # Add conversation history (simple messages only)
        for msg in self.conversation_history:
            base_messages.append(msg)
        
        # Add current user request
        base_messages.append({
            "role": "user",
            "content": user_request,
        })
        
        # Working messages array for this request (includes tool calls)
        messages = list(base_messages)
        
        all_results = []
        iterations = 0
        max_iterations = 5
        total_input_tokens = 0
        total_output_tokens = 0
        
        while iterations < max_iterations:
            iterations += 1
            
            if verbose:
                console.print(f"[dim]Thinking... (iteration {iterations})[/dim]")
            
            try:
                response_text, tool_calls, usage_info = self._call_llm_sync(messages)
                total_input_tokens += usage_info.get("input_tokens", 0)
                total_output_tokens += usage_info.get("output_tokens", 0)
            except Exception as e:
                # On error, try without tool history (fresh start)
                if iterations > 1:
                    messages = list(base_messages)
                    response_text, tool_calls, usage_info = self._call_llm_sync(messages)
                    total_input_tokens += usage_info.get("input_tokens", 0)
                    total_output_tokens += usage_info.get("output_tokens", 0)
                else:
                    raise
            
            if not tool_calls:
                # No more tools to call - save to history and return
                self.conversation_history.append({
                    "role": "user", 
                    "content": user_request,
                })
                
                # Build assistant response that includes context
                assistant_content = response_text or ""
                if all_results and assistant_content:
                    # Prepend tool summary if we used tools
                    tools_used = ", ".join([r["tool"] for r in all_results[:3]])
                    assistant_content = f"[Used {tools_used}] {assistant_content}"
                elif all_results and not assistant_content:
                    results_summary = []
                    for r in all_results:
                        count = len(r["result"]) if isinstance(r["result"], list) else 1
                        results_summary.append(f"{r['tool']}: {count} results")
                    assistant_content = f"[{', '.join(results_summary)}]"
                
                if assistant_content:
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": assistant_content,
                    })
                
                # Trim history (keep last 10 exchanges = 20 messages)
                if len(self.conversation_history) > 20:
                    self.conversation_history = self.conversation_history[-20:]
                
                # Calculate cost estimate (Claude Sonnet 4 pricing: $3/$15 per million tokens)
                cost_estimate = (total_input_tokens * 3 / 1_000_000) + (total_output_tokens * 15 / 1_000_000)
                duration_ms = int((time.time() - start_time) * 1000)
                
                return {
                    "response": response_text,
                    "results": all_results,
                    "tool_results": [r["result"] for r in all_results],
                    "tools_called": [{"name": r["tool"]} for r in all_results],
                    "usage": {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "total_cost_usd": cost_estimate,
                        "duration_ms": duration_ms,
                        "model": self.model,
                    },
                }
            
            # Execute all tool calls in this iteration
            tool_call_message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            tool_results_messages = []
            
            for tc in tool_calls:
                if verbose:
                    console.print(f"  [cyan]→ {tc['name']}[/cyan]({tc['arguments']})")
                
                # Add to tool_calls array
                tool_call_message["tool_calls"].append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                })
                
                try:
                    result = execute_tool(tc["name"], **tc["arguments"])
                    all_results.append({
                        "tool": tc["name"],
                        "arguments": tc["arguments"],
                        "result": result,
                    })
                    
                    tool_results_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, default=str)[:10000],  # Limit size
                    })
                    
                except Exception as e:
                    if verbose:
                        console.print(f"  [red]✗ Error: {e}[/red]")
                    tool_results_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"Error: {str(e)}",
                    })
            
            # Add assistant message with tool_calls, then tool results
            messages.append(tool_call_message)
            messages.extend(tool_results_messages)
        
        return {
            "response": "Max iterations reached",
            "results": all_results,
            "tool_results": [r["result"] for r in all_results],
            "tools_called": [{"name": r["tool"]} for r in all_results],
        }
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
    
    def print_result(self, result: Dict[str, Any]):
        """Pretty-print an execution result."""
        # Print tool results summary
        if result["results"]:
            console.print()
            console.print("[bold]Results:[/bold]")
            for r in result["results"]:
                count = len(r["result"]) if isinstance(r["result"], list) else 1
                console.print(f"  [green]✓[/green] {r['tool']}: {count} results")
        
        # Print response
        if result["response"]:
            console.print()
            panel = Panel(
                result["response"],
                title="[bold cyan]Stemmy AI[/bold cyan]",
                border_style="cyan",
            )
            console.print(panel)
        
        # Print usage/cost info
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
            model = usage.get("model")
            if model:
                parts.append(f"Model: {model}")
            if parts:
                console.print(f"[dim]{' | '.join(parts)}[/dim]")
