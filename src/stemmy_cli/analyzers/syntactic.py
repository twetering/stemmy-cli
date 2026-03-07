"""
Syntactic Pattern Analyzer - Sentence structure patterns.

Mirrors: CreateStemmy/WordAnalyzers/SyntacticAnalyzer.js
"""

import json
import re
from typing import List, Dict, Any, Optional

SYNTACTIC_PATTERNS = {
    "comparison": {
        "label": "Comparisons",
        "help": "Find comparisons (e.g., 'better than', 'more than')",
        "patterns": ["dan", "zoals", "net als", "in tegenstelling tot", "vergeleken met", 
                     "beter", "slechter", "meer", "minder", "groter", "kleiner"],
    },
    "enumeration": {
        "label": "Enumerations",
        "help": "Find lists and enumerations",
        "patterns": ["ten eerste", "ten tweede", "ten derde", "daarnaast", "bovendien", 
                     "tenslotte", "ook", "verder", "allereerst", "vervolgens"],
    },
    "causation": {
        "label": "Cause-Effect",
        "help": "Find cause-effect relationships",
        "patterns": ["omdat", "daardoor", "dus", "als gevolg van", "hierdoor", 
                     "vanwege", "want", "doordat", "waardoor", "resulterend in"],
    },
    "temporal": {
        "label": "Temporal Relations",
        "help": "Find time-related connections",
        "patterns": ["voordat", "nadat", "tijdens", "wanneer", "toen", "terwijl",
                     "daarna", "eerst", "later", "uiteindelijk", "inmiddels"],
    },
    "contrast": {
        "label": "Contrast/Opposition",
        "help": "Find contrasting statements",
        "patterns": ["maar", "echter", "toch", "daarentegen", "hoewel", "ondanks",
                     "niettemin", "aan de andere kant", "integendeel", "wel"],
    },
    "condition": {
        "label": "Conditions",
        "help": "Find conditional statements",
        "patterns": ["als", "indien", "mits", "tenzij", "wanneer", "zodra",
                     "in het geval dat", "op voorwaarde dat", "gesteld dat"],
    },
    "conclusion": {
        "label": "Conclusions",
        "help": "Find concluding statements",
        "patterns": ["dus", "daarom", "concluderend", "kortom", "samenvattend",
                     "al met al", "uiteindelijk", "per saldo", "concludeer"],
    },
    "example": {
        "label": "Examples",
        "help": "Find examples and illustrations",
        "patterns": ["bijvoorbeeld", "zoals", "ter illustratie", "neem nu",
                     "denk aan", "een voorbeeld hiervan", "dit blijkt uit"],
    },
}


def _find_sentence_boundaries(words: List[Dict], match_index: int, pause_threshold_ms: int = 750) -> tuple:
    """Find sentence boundaries around a match point."""
    start_idx = match_index
    end_idx = match_index
    
    # Search backwards
    while start_idx > 0:
        prev_word = words[start_idx - 1]
        prev_text = prev_word.get("text", "")
        current_start = words[start_idx].get("start", 0)
        prev_end = prev_word.get("end", 0)
        
        if any(p in prev_text for p in [".", "!", "?"]):
            break
        if (current_start - prev_end) > pause_threshold_ms:
            break
        start_idx -= 1
    
    # Search forwards
    while end_idx < len(words) - 1:
        current_word = words[end_idx]
        current_text = current_word.get("text", "")
        current_end = current_word.get("end", 0)
        next_start = words[end_idx + 1].get("start", 0)
        
        if any(p in current_text for p in [".", "!", "?"]):
            break
        if (next_start - current_end) > pause_threshold_ms:
            break
        end_idx += 1
    
    return start_idx, end_idx


def search_syntactic_pattern(
    words_data: List[Dict],
    pattern_type: str,
    custom_patterns: Optional[List[str]] = None,
    context_words: int = 5,
    audio_url: str = "",
    fragment_id: str = "",
    item_id: str = "",
    item_title: str = "",
    extract_mode: str = "sentence",  # sentence, context, word
) -> List[Dict[str, Any]]:
    """
    Search for syntactic patterns in word-level data.
    
    Args:
        words_data: List of word objects with text, start, end keys
        pattern_type: Pattern type from SYNTACTIC_PATTERNS
        custom_patterns: Additional patterns to search for
        context_words: Number of context words (for context mode)
        audio_url: Audio URL for results
        fragment_id: Fragment ID for results
        item_id: Item ID for results
        item_title: Item title for results
        extract_mode: What to extract (sentence, context, word)
    
    Returns:
        List of matching entries with timing information
    """
    matches = []
    patterns_to_search = []
    
    # Get predefined patterns
    if pattern_type in SYNTACTIC_PATTERNS:
        patterns_to_search.extend(SYNTACTIC_PATTERNS[pattern_type]["patterns"])
    
    # Add custom patterns
    if custom_patterns:
        patterns_to_search.extend(custom_patterns)
    
    if not patterns_to_search:
        return []
    
    # Build text from words for multi-word pattern matching
    full_text = " ".join(w.get("text", "") for w in words_data).lower()
    
    for pattern in patterns_to_search:
        pattern_lower = pattern.lower()
        pattern_words = pattern_lower.split()
        pattern_len = len(pattern_words)
        
        # Scan through words looking for pattern start
        for i in range(len(words_data) - pattern_len + 1):
            # Build sequence to compare
            sequence = " ".join(
                words_data[i + j].get("text", "").strip(".,!?:;\"'-").lower() 
                for j in range(pattern_len)
            )
            
            if sequence == pattern_lower or pattern_lower in sequence:
                match_start_idx = i
                match_end_idx = i + pattern_len - 1
                
                # Determine extraction range
                if extract_mode == "sentence":
                    start_idx, end_idx = _find_sentence_boundaries(words_data, match_start_idx)
                elif extract_mode == "context":
                    start_idx = max(0, match_start_idx - context_words)
                    end_idx = min(len(words_data) - 1, match_end_idx + context_words)
                else:  # word
                    start_idx = match_start_idx
                    end_idx = match_end_idx
                
                extract_words = words_data[start_idx:end_idx + 1]
                extracted_text = " ".join(w.get("text", "") for w in extract_words)
                
                start_ms = extract_words[0].get("start", 0)
                end_ms = extract_words[-1].get("end", 0)
                
                matches.append({
                    "id": f"{fragment_id}_{start_ms}_{end_ms}",
                    "text": extracted_text,
                    "match_pattern": pattern,
                    "pattern_type": pattern_type,
                    "start_time": start_ms / 1000,
                    "end_time": end_ms / 1000,
                    "duration": (end_ms - start_ms) / 1000,
                    "confidence": min(w.get("confidence", 1.0) for w in extract_words),
                    "speaker": extract_words[0].get("speaker", ""),
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                    "extract_mode": extract_mode,
                })
    
    return matches
