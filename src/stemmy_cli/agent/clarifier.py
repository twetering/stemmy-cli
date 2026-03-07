"""
Question clarification system for Stemmy CLI.

Presents multiple-choice questions to clarify ambiguous requests
before executing them.
"""

from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, IntPrompt

console = Console()


# Patterns that suggest clarification might be needed
AMBIGUOUS_PATTERNS = {
    "compilatie": {
        "question": "Wat voor soort compilatie wil je maken?",
        "options": [
            ("word", "Woord compilatie - alle keren dat een specifiek woord voorkomt"),
            ("entity", "Entity compilatie - alle mentions van bijv. een persoon of bedrijf"),
            ("sentence", "Zin compilatie - zinnen die beginnen/eindigen met..."),
            ("mixed", "Gemixte compilatie - combinatie van bronnen"),
        ],
        "followup": {
            "word": "Welk woord wil je compileren?",
            "entity": "Welke entity (persoon, bedrijf, etc) wil je compileren?",
            "sentence": "Welk patroon? (bijv. 'begint met Ik denk')",
        }
    },
    "zoek": {
        "question": "Wat wil je zoeken?",
        "options": [
            ("fragments", "Fragmenten - zinnen/transcripties"),
            ("entities", "Entities - personen, bedrijven, locaties"),
            ("formats", "Formats - podcasts/shows"),
        ],
    },
    "analyse": {
        "question": "Wat voor analyse wil je uitvoeren?",
        "options": [
            ("lexical", "Lexicale analyse - woordpatronen, alliteratie, etc."),
            ("sentiment", "Sentiment analyse - positief/negatief"),
            ("structure", "Structuur analyse - vragen, conclusies, uitroepen"),
        ],
    },
}


class RequestClarifier:
    """
    Detects ambiguous requests and asks clarifying questions.
    
    Similar to Claude Code's question articulation feature.
    """
    
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.context: Dict[str, Any] = {}
    
    def needs_clarification(self, request: str) -> bool:
        """Check if a request might benefit from clarification."""
        if not self.enabled:
            return False
        
        request_lower = request.lower()
        
        # Very short requests are often ambiguous
        if len(request.split()) <= 2:
            return True
        
        # Check for pattern matches
        for pattern in AMBIGUOUS_PATTERNS:
            if pattern in request_lower:
                # But not if they already have specifics
                if self._has_specifics(request_lower, pattern):
                    return False
                return True
        
        return False
    
    def _has_specifics(self, request: str, pattern: str) -> bool:
        """Check if request already has enough specifics."""
        # If there are quoted strings, probably specific
        if '"' in request or "'" in request:
            return True
        
        # If there are entity type keywords
        entity_types = ["person", "organization", "location", "persoon", "bedrijf"]
        if pattern == "compilatie" and any(et in request for et in entity_types):
            return True
        
        # Long requests are usually specific
        if len(request.split()) > 8:
            return True
        
        return False
    
    def clarify(self, request: str) -> Tuple[str, Dict[str, Any]]:
        """
        Present clarification questions and return enhanced request.
        
        Returns:
            Tuple of (enhanced_request, context_dict)
        """
        if not self.enabled:
            return request, {}
        
        request_lower = request.lower()
        context = {}
        enhanced_parts = []
        
        # Find matching patterns
        for pattern, config in AMBIGUOUS_PATTERNS.items():
            if pattern in request_lower:
                choice, value = self._ask_choice(
                    config["question"],
                    config["options"]
                )
                
                if choice:
                    context[pattern] = choice
                    
                    # Check for followup question
                    if "followup" in config and choice in config["followup"]:
                        followup_answer = Prompt.ask(
                            f"[cyan]?[/cyan] {config['followup'][choice]}"
                        )
                        context[f"{pattern}_value"] = followup_answer
                        enhanced_parts.append(followup_answer)
                
                # Only handle one pattern per request
                break
        
        # Build enhanced request
        if enhanced_parts:
            enhanced_request = request + " " + " ".join(enhanced_parts)
        else:
            enhanced_request = request
        
        self.context = context
        return enhanced_request, context
    
    def _ask_choice(
        self,
        question: str,
        options: List[Tuple[str, str]]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Present a multiple choice question."""
        console.print()
        console.print(f"[bold cyan]?[/bold cyan] {question}")
        console.print()
        
        for i, (key, desc) in enumerate(options, 1):
            console.print(f"  [cyan]{i}[/cyan]. {desc}")
        
        console.print()
        console.print("  [dim]0. Skip (let AI decide)[/dim]")
        console.print()
        
        try:
            choice = IntPrompt.ask(
                "[cyan]Choose[/cyan]",
                choices=[str(i) for i in range(len(options) + 1)],
                default="0"
            )
            
            if choice == 0:
                return None, None
            
            key, desc = options[choice - 1]
            return key, desc
            
        except (KeyboardInterrupt, EOFError):
            return None, None
    
    def quick_clarify(
        self,
        header: str,
        options: List[str],
        allow_skip: bool = True
    ) -> Optional[str]:
        """
        Quick single-choice clarification.
        
        Args:
            header: Short question header
            options: List of option strings
            allow_skip: Whether to allow skipping
            
        Returns:
            Selected option or None if skipped
        """
        console.print()
        console.print(f"[bold cyan]?[/bold cyan] {header}")
        console.print()
        
        for i, opt in enumerate(options, 1):
            console.print(f"  [cyan]{i}[/cyan]. {opt}")
        
        if allow_skip:
            console.print()
            console.print("  [dim]0. Skip[/dim]")
        
        console.print()
        
        valid_choices = [str(i) for i in range(1, len(options) + 1)]
        if allow_skip:
            valid_choices.append("0")
        
        try:
            choice = IntPrompt.ask(
                "[cyan]Choose[/cyan]",
                choices=valid_choices,
                default="0" if allow_skip else "1"
            )
            
            if choice == 0:
                return None
            
            return options[choice - 1]
            
        except (KeyboardInterrupt, EOFError):
            return None
    
    def confirm(self, message: str, default: bool = True) -> bool:
        """Quick yes/no confirmation."""
        default_str = "y" if default else "n"
        try:
            result = Prompt.ask(
                f"[cyan]?[/cyan] {message}",
                choices=["y", "n", "ja", "nee"],
                default=default_str
            )
            return result.lower() in ("y", "ja")
        except (KeyboardInterrupt, EOFError):
            return default


# Global clarifier instance
_clarifier: Optional[RequestClarifier] = None


def get_clarifier() -> RequestClarifier:
    """Get the global clarifier instance."""
    global _clarifier
    if _clarifier is None:
        _clarifier = RequestClarifier(enabled=True)
    return _clarifier


def clarify_request(request: str) -> Tuple[str, Dict[str, Any]]:
    """Clarify a request if needed."""
    clarifier = get_clarifier()
    if clarifier.needs_clarification(request):
        return clarifier.clarify(request)
    return request, {}
