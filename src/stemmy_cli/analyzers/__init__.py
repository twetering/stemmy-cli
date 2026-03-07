"""
Word and Text Analyzers for Stemmy CLI.

This module provides various analyzers mirroring the frontend's CreateStemmy analyzers:
- lexical: Word patterns (starts_with, ends_with, alliteration, palindrome, etc.)
- syntactic: Syntactic patterns (comparison, enumeration, causation, temporal)
- speaker: Speaker analysis (turn_taking, interruptions, filler_words, etc.)
- style: Style analysis (formality, rhetoric, tone, dialog, argumentation)
- text: Text analysis (complexity, coherence, variation)
"""

from .lexical import LEXICAL_MODES, search_lexical_pattern
from .syntactic import SYNTACTIC_PATTERNS, search_syntactic_pattern
from .speaker import SPEAKER_MODES, search_speaker_pattern
from .style import STYLE_MODES, search_style_pattern
from .text import TEXT_MODES, search_text_pattern

__all__ = [
    # Lexical
    "LEXICAL_MODES",
    "search_lexical_pattern",
    # Syntactic
    "SYNTACTIC_PATTERNS",
    "search_syntactic_pattern",
    # Speaker
    "SPEAKER_MODES",
    "search_speaker_pattern",
    # Style
    "STYLE_MODES",
    "search_style_pattern",
    # Text
    "TEXT_MODES",
    "search_text_pattern",
]
