"""
Interactive shell mode for Stemmy CLI.

Apple-inspired design: just type what you want.
The shell figures out whether you want AI help or a direct command.
"""

import shlex
from pathlib import Path
from typing import List, Optional, Dict, Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# Quick commands (shortcuts, not required)
SHORTCUTS = {
    "?": "help",
    "q": "quit",
    "/s": "search",
    "/e": "entities", 
    "/f": "formats",
    "/a": "analyze",
}


class SmartCompleter(Completer):
    """Intelligent completer that learns from context."""
    
    def __init__(self):
        self.recent_queries: List[str] = []
        self.shortcuts = list(SHORTCUTS.keys())
        self.common_queries = [
            "zoek fragmenten over",
            "hoeveel",
            "list formats",
            "search",
            "maak compilatie",
            "find questions",
            "analyze",
        ]
    
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lower()
        
        if not text:
            # Show recent + common queries
            for q in self.recent_queries[-3:]:
                yield Completion(q, display=f"↻ {q[:40]}")
            return
        
        # Shortcut completions
        for shortcut in self.shortcuts:
            if shortcut.startswith(text):
                yield Completion(shortcut, display=f"{shortcut} → {SHORTCUTS[shortcut]}")
        
        # Query suggestions
        for query in self.common_queries:
            if query.startswith(text):
                yield Completion(query, start_position=-len(text))
    
    def add_to_history(self, query: str):
        if query and query not in self.recent_queries:
            self.recent_queries.append(query)
            if len(self.recent_queries) > 10:
                self.recent_queries.pop(0)


def get_style():
    """Minimal, clean style."""
    return Style.from_dict({
        "prompt": "#00d4ff bold",
        "": "#ffffff",
    })


class InteractiveShell:
    """
    Intelligent interactive shell.
    
    Design principle: The user just types what they want.
    - Natural language → AI handles it
    - Shortcuts → Quick access to common functions
    - Everything else → Intelligent routing
    """
    
    def __init__(self, show_thinking: bool = True):
        history_path = Path.home() / ".stemmy_history"
        self.completer = SmartCompleter()
        self.session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=self.completer,
            style=get_style(),
            complete_while_typing=False,
        )
        self.last_results: List[Dict[str, Any]] = []
        self.running = True
        self.context: Dict[str, Any] = {}
        
        # Persistent AI orchestrator with conversation memory
        self._orchestrator: Optional[Any] = None
        
        # Show thinking trace (toggle with 'think' command)
        self._show_thinking = show_thinking
        
        # Clarification mode (toggle with 'clarify' command)
        self._clarify_mode = True
    
    def run(self):
        """Main loop with proper signal handling."""
        import signal
        
        # Track consecutive Ctrl+C presses
        self._interrupt_count = 0
        
        def handle_sigint(signum, frame):
            """Handle Ctrl+C gracefully."""
            self._interrupt_count += 1
            if self._interrupt_count >= 2:
                # Second Ctrl+C = immediate exit
                console.print("\n[dim]Interrupted[/dim]")
                raise SystemExit(0)
            else:
                # First Ctrl+C = friendly message
                console.print("\n[dim]Press Ctrl+C again to exit, or type 'q'[/dim]")
        
        # Set up signal handler
        original_handler = signal.signal(signal.SIGINT, handle_sigint)
        
        try:
            self._show_welcome()
            
            while self.running:
                try:
                    text = self.session.prompt(
                        HTML("<prompt>▸ </prompt>"),
                        placeholder=HTML("<style fg='#666666'>Type anything...</style>"),
                    )
                    
                    # Reset interrupt count only after successful input
                    self._interrupt_count = 0
                    
                    if text.strip():
                        self.completer.add_to_history(text.strip())
                        self._process(text.strip())
                        
                except KeyboardInterrupt:
                    # Handle Ctrl+C from prompt_toolkit
                    self._interrupt_count += 1
                    console.print()
                    if self._interrupt_count >= 2:
                        console.print("[dim]Bye![/dim]")
                        self.running = False
                        break
                    else:
                        console.print("[dim]Press Ctrl+C again to exit, or type 'q'[/dim]")
                    
                except EOFError:
                    # Ctrl+D
                    self.running = False
            
            console.print("\n[dim]👋[/dim]")
            
        finally:
            # Restore original signal handler
            signal.signal(signal.SIGINT, original_handler)
    
    def _show_welcome(self):
        """Clean, minimal welcome."""
        from stemmy_cli.branding import get_banner
        
        console.print()
        console.print(get_banner(include_tagline=False), style="cyan")
        console.print()
        
        # Quick stats
        try:
            from stemmy_cli.agent.tools import execute_tool
            stats = execute_tool("get_database_stats")
            stats_line = " · ".join([
                f"[cyan]{stats.get('fragments', 0):,}[/cyan] fragments",
                f"[cyan]{stats.get('entities', 0):,}[/cyan] entities",
                f"[cyan]{stats.get('formats', 0):,}[/cyan] podcasts",
            ])
            console.print(f"  {stats_line}")
        except Exception:
            pass
        
        console.print()
        console.print("  [dim]Just type what you want. Examples:[/dim]")
        console.print("  [dim]·[/dim] zoek fragmenten over AI")
        console.print("  [dim]·[/dim] maak compilatie van vragen")
        console.print("  [dim]·[/dim] hoeveel entities zijn er?")
        console.print()
        console.print("  [dim]Shortcuts: ? = help, q = quit, Tab = autocomplete[/dim]")
        console.print()
    
    def _process(self, text: str):
        """
        Smart routing: figure out what the user wants.
        """
        text_lower = text.lower().strip()
        
        # Handle shortcuts
        if text_lower in SHORTCUTS:
            text_lower = SHORTCUTS[text_lower]
        
        # Exit commands
        if text_lower in ("quit", "exit", "q", "bye"):
            self.running = False
            return
        
        # Help
        if text_lower in ("help", "?", "h"):
            self._show_help()
            return
        
        # Clear screen
        if text_lower in ("clear", "cls"):
            console.clear()
            self._show_welcome()
            return
        
        # New conversation (reset AI memory)
        if text_lower in ("new", "reset", "nieuw"):
            if self._orchestrator:
                self._orchestrator.clear_history()
            self.context = {}
            self.last_results = []
            console.print("[dim]New conversation started[/dim]")
            return
        
        # Toggle thinking trace visibility
        if text_lower in ("think", "thinking", "trace"):
            self._show_thinking = not self._show_thinking
            status = "[green]ON[/green]" if self._show_thinking else "[red]OFF[/red]"
            console.print(f"[dim]Thinking trace: {status}[/dim]")
            # Update orchestrator if it exists
            if self._orchestrator and hasattr(self._orchestrator, 'show_thinking'):
                self._orchestrator.show_thinking = self._show_thinking
            return
        
        # Verbose mode
        if text_lower in ("verbose", "debug"):
            if self._orchestrator:
                current = getattr(self._orchestrator, 'verbose', False)
                self._orchestrator.verbose = not current
                status = "[green]ON[/green]" if self._orchestrator.verbose else "[red]OFF[/red]"
                console.print(f"[dim]Verbose mode: {status}[/dim]")
            return
        
        # Toggle clarification mode
        if text_lower in ("clarify", "questions", "quiz"):
            self._clarify_mode = not self._clarify_mode
            status = "[green]ON[/green]" if self._clarify_mode else "[red]OFF[/red]"
            console.print(f"[dim]Clarification quiz: {status}[/dim]")
            return
        
        # Stats shortcut
        if text_lower in ("stats", "status", "info"):
            self._show_stats()
            return
        
        # Formats shortcut (expanded from /f)
        if text_lower == "formats":
            self._quick_formats()
            return
        
        # Quick search shortcuts - check both /x and expanded form
        if text_lower.startswith("/s ") or text_lower.startswith("search "):
            query = text[3:].strip() if text_lower.startswith("/s ") else text[7:].strip()
            self._quick_search(query)
            return
        
        if text_lower.startswith("/e ") or text_lower.startswith("entities "):
            query = text[3:].strip() if text_lower.startswith("/e ") else text[9:].strip()
            self._quick_entities(query)
            return
        
        if text_lower == "/f":
            self._quick_formats()
            return
        
        if text_lower.startswith("/a ") or text_lower.startswith("analyze "):
            query = text[3:].strip() if text_lower.startswith("/a ") else text[8:].strip()
            self._quick_analyze(query)
            return
        
        # Everything else → AI
        self._ask_ai(text)
    
    def _get_orchestrator(self):
        """Get or create the persistent orchestrator with SDK support."""
        if self._orchestrator is None:
            # Try new ClaudeOrchestrator first, then SDK, then basic
            try:
                from stemmy_cli.agent.claude_orchestrator import ClaudeOrchestrator
                self._orchestrator = ClaudeOrchestrator(
                    verbose=True,
                    show_thinking=self._show_thinking,
                    clarify_questions=True,
                )
                console.print("[dim]Using Claude Agent SDK with streaming[/dim]")
            except Exception as e:
                try:
                    from stemmy_cli.agent.sdk_orchestrator import SDKOrchestrator
                    self._orchestrator = SDKOrchestrator(verbose=False)
                    console.print("[dim]Using Claude Agent SDK with skills[/dim]")
                except Exception:
                    from stemmy_cli.agent import Orchestrator
                    self._orchestrator = Orchestrator()
        return self._orchestrator
    
    def _ask_ai(self, request: str):
        """Route to AI for natural language processing with thinking trace."""
        try:
            console.print()
            
            # Clarify ambiguous requests if enabled
            if self._clarify_mode:
                from stemmy_cli.agent.clarifier import clarify_request
                enhanced_request, context = clarify_request(request)
                if enhanced_request != request:
                    console.print(f"[dim]Enhanced: {enhanced_request}[/dim]")
                    request = enhanced_request
            
            orchestrator = self._get_orchestrator()
            
            # Check if orchestrator supports streaming
            if hasattr(orchestrator, 'execute_with_progress'):
                # Use the new rich progress display
                result = orchestrator.execute(request, verbose=True)
            else:
                # Fallback to spinner
                with console.status("[cyan]Thinking...[/cyan]", spinner="dots"):
                    result = orchestrator.execute(request, verbose=False)
            
            # Use orchestrator's print method if available
            if hasattr(orchestrator, 'print_result'):
                orchestrator.print_result(result)
            else:
                # Manual display
                tools_used = result.get("tools_called", [])
                if tools_used:
                    tools_str = ", ".join([t.get("name", "") for t in tools_used[:3]])
                    console.print(f"[dim]Used: {tools_str}[/dim]")
                
                if result.get("tool_results"):
                    total_results = sum(len(r) if isinstance(r, list) else 1 for r in result["tool_results"])
                    if total_results > 0:
                        console.print(f"[dim]Found {total_results} results[/dim]")
                
                response = result.get("response", "")
                if response:
                    console.print()
                    console.print(Panel(
                        response,
                        border_style="cyan",
                        padding=(1, 2),
                    ))
            
            # Store results for follow-up
            if result.get("tool_results"):
                self.last_results = result["tool_results"]
            
            # Store context for conversation
            self.context["last_request"] = request
            self.context["last_response"] = result.get("response", "")
            self.context["last_tool_results"] = result.get("tool_results", [])
            
            console.print()
            
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            console.print("[dim]Try rephrasing your request[/dim]")
    
    def _quick_search(self, query: str):
        """Fast fragment search."""
        if not query:
            console.print("[yellow]Usage: /s <query>[/yellow]")
            return
        
        from stemmy_cli.agent.tools import execute_tool
        
        with console.status(f"[dim]Searching '{query}'...[/dim]"):
            results = execute_tool("search_fragments", query=query, limit=10)
        
        self.last_results = results
        self._display_fragments(results)
    
    def _quick_entities(self, query: str):
        """Fast entity search."""
        if not query:
            console.print("[yellow]Usage: /e <query>[/yellow]")
            return
        
        from stemmy_cli.agent.tools import execute_tool
        
        with console.status(f"[dim]Searching entities '{query}'...[/dim]"):
            results = execute_tool("search_entities", query=query, limit=10)
        
        self.last_results = results
        self._display_entities(results)
    
    def _quick_formats(self):
        """List formats."""
        from stemmy_cli.agent.tools import execute_tool
        
        with console.status("[dim]Loading formats...[/dim]"):
            results = execute_tool("list_formats", limit=15)
        
        if not results:
            console.print("[yellow]No formats found[/yellow]")
            return
        
        console.print()
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("ID", style="dim", width=8)
        table.add_column("Title")
        table.add_column("Language", style="dim")
        
        for r in results:
            table.add_row(
                r.get("id", "")[:8],
                r.get("title", "")[:50],
                r.get("language", ""),
            )
        
        console.print(table)
        console.print()
    
    def _quick_analyze(self, query: str):
        """Quick analysis."""
        parts = query.split(maxsplit=1)
        if not parts:
            console.print("[yellow]Usage: /a <mode> [pattern][/yellow]")
            console.print("[dim]Modes: starts_with, ends_with, questions, filler_words...[/dim]")
            return
        
        mode = parts[0]
        pattern = parts[1] if len(parts) > 1 else ""
        
        from stemmy_cli.agent.tools import execute_tool
        
        with console.status(f"[dim]Analyzing with {mode}...[/dim]"):
            try:
                results = execute_tool("analyze_lexical", mode=mode, pattern=pattern, limit=10)
            except Exception:
                try:
                    results = execute_tool("analyze_text", mode=mode, limit=10)
                except Exception as e:
                    console.print(f"[red]Analysis failed: {e}[/red]")
                    return
        
        self.last_results = results
        
        if results:
            console.print(f"\n[green]Found {len(results)} matches[/green]")
            for i, r in enumerate(results[:8]):
                word = r.get("word", r.get("text", ""))[:60]
                console.print(f"  [cyan]{i+1}.[/cyan] {word}")
            if len(results) > 8:
                console.print(f"  [dim]... +{len(results) - 8} more[/dim]")
            console.print()
        else:
            console.print("[yellow]No matches found[/yellow]")
    
    def _display_fragments(self, results: List[Dict[str, Any]]):
        """Display fragment results nicely."""
        if not results:
            console.print("[yellow]No fragments found[/yellow]")
            return
        
        console.print(f"\n[green]Found {len(results)} fragments[/green]\n")
        
        for i, r in enumerate(results[:8]):
            text = r.get("text", "")[:70]
            if len(r.get("text", "")) > 70:
                text += "..."
            speaker = r.get("speaker_label", "")
            speaker_str = f" [dim]({speaker})[/dim]" if speaker else ""
            console.print(f"  [cyan]{i+1}.[/cyan] {text}{speaker_str}")
        
        if len(results) > 8:
            console.print(f"  [dim]... +{len(results) - 8} more[/dim]")
        console.print()
    
    def _display_entities(self, results: List[Dict[str, Any]]):
        """Display entity results."""
        if not results:
            console.print("[yellow]No entities found[/yellow]")
            return
        
        console.print(f"\n[green]Found {len(results)} entities[/green]\n")
        
        for i, r in enumerate(results[:8]):
            text = r.get("text", "")
            entity_type = r.get("entity_type", "")
            console.print(f"  [cyan]{i+1}.[/cyan] {text} [dim]({entity_type})[/dim]")
        
        if len(results) > 8:
            console.print(f"  [dim]... +{len(results) - 8} more[/dim]")
        console.print()
    
    def _show_stats(self):
        """Show database statistics."""
        from stemmy_cli.agent.tools import execute_tool
        
        stats = execute_tool("get_database_stats")
        
        console.print()
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Table", style="dim")
        table.add_column("Count", style="cyan bold", justify="right")
        
        for table_name, count in stats.items():
            table.add_row(table_name, f"{count:,}")
        
        console.print(table)
        console.print()
    
    def _show_help(self):
        """Show concise help."""
        console.print()
        console.print("[bold]How to use Stemmy[/bold]")
        console.print()
        console.print("  Just type what you want in natural language.")
        console.print("  Stemmy remembers our conversation - you can say \"ja\" to confirm,")
        console.print("  or refer to previous results.")
        console.print()
        console.print("  [dim]Examples:[/dim]")
        console.print("  [cyan]▸[/cyan] zoek fragmenten over Google, Amazon en Tesla")
        console.print("  [cyan]▸[/cyan] ja, maak daar een compilatie van")
        console.print("  [cyan]▸[/cyan] hoeveel entities van type person zijn er?")
        console.print()
        console.print("[bold]Shortcuts[/bold] [dim](optional)[/dim]")
        console.print()
        console.print("  [cyan]/s[/cyan] <query>     Quick search fragments")
        console.print("  [cyan]/e[/cyan] <query>     Quick search entities")
        console.print("  [cyan]/f[/cyan]             List formats/podcasts")
        console.print("  [cyan]/a[/cyan] <mode>      Run analyzer")
        console.print("  [cyan]stats[/cyan]          Database statistics")
        console.print("  [cyan]new[/cyan]            Start new conversation")
        console.print("  [cyan]clear[/cyan]          Clear screen")
        console.print("  [cyan]q[/cyan]              Quit")
        console.print()
        console.print("[bold]AI Options[/bold]")
        console.print()
        console.print("  [cyan]think[/cyan]          Toggle thinking trace visibility")
        console.print("  [cyan]clarify[/cyan]        Toggle clarification quiz (asks questions for ambiguous requests)")
        console.print("  [cyan]verbose[/cyan]        Toggle verbose debug output")
        console.print()


def start_interactive():
    """Start the interactive shell."""
    shell = InteractiveShell()
    shell.run()
