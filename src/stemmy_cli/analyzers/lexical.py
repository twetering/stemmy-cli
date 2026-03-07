"""
Lexical Pattern Analyzer - Word-level pattern matching.

Mirrors: CreateStemmy/WordAnalyzers/LexicalPatternAnalyzer.js
"""

import json
import re
from typing import List, Dict, Any, Optional

LEXICAL_MODES = {
    "starts_with": {
        "label": "Starts With",
        "help": "Find words starting with a pattern (e.g., 'voor' → vooruit, voorbij)",
    },
    "ends_with": {
        "label": "Ends With", 
        "help": "Find words ending with a pattern (e.g., 'ing' → spanning, training)",
    },
    "alliteration": {
        "label": "Alliteration",
        "help": "Find consecutive words with same starting letter",
    },
    "word_length": {
        "label": "Word Length",
        "help": "Find words with specific length (exact, min, max, or range)",
    },
    "repeating_letters": {
        "label": "Repeating Letters",
        "help": "Find words with repeated letters (e.g., 'a' → aanname, maatschap)",
    },
    "palindrome": {
        "label": "Palindrome",
        "help": "Find palindromic words (same forwards and backwards)",
    },
    "rhyme": {
        "label": "Rhyme",
        "help": "Find words that rhyme with query (same ending sounds)",
    },
    "syllable_count": {
        "label": "Syllable Count",
        "help": "Find words with specific syllable count",
    },
    "compound": {
        "label": "Compound Words",
        "help": "Find compound words containing a base word",
    },
    "contains": {
        "label": "Contains",
        "help": "Find words containing a substring",
    },
    "regex": {
        "label": "Regex Pattern",
        "help": "Find words matching a regex pattern",
    },
}


def _estimate_syllables(word: str) -> int:
    """Estimate syllable count for a word (Dutch/English heuristic)."""
    word = word.lower().strip()
    if not word:
        return 0
    
    vowels = "aeiouàèéëïöüy"
    count = 0
    prev_vowel = False
    
    for char in word:
        is_vowel = char in vowels
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    
    # Handle silent e at end
    if word.endswith('e') and count > 1:
        count -= 1
    
    return max(1, count)


def _is_palindrome(word: str) -> bool:
    """Check if word is a palindrome."""
    clean = re.sub(r'[^a-zA-Z]', '', word.lower())
    return len(clean) > 2 and clean == clean[::-1]


def _get_rhyme_ending(word: str, chars: int = 3) -> str:
    """Get the rhyming ending of a word."""
    clean = re.sub(r'[^a-zA-Z]', '', word.lower())
    return clean[-chars:] if len(clean) >= chars else clean


def search_lexical_pattern(
    words_data: List[Dict],
    mode: str,
    pattern: str = "",
    min_length: int = 0,
    max_length: int = 0,
    syllables: int = 0,
    case_sensitive: bool = False,
    audio_url: str = "",
    fragment_id: str = "",
    item_id: str = "",
    item_title: str = "",
) -> List[Dict[str, Any]]:
    """
    Search for lexical patterns in word-level data.
    
    Args:
        words_data: List of word objects with text, start, end keys
        mode: Lexical search mode from LEXICAL_MODES
        pattern: Search pattern (varies by mode)
        min_length: Minimum word length (for word_length mode)
        max_length: Maximum word length (for word_length mode)
        syllables: Target syllable count (for syllable_count mode)
        case_sensitive: Whether to match case-sensitively
        audio_url: Audio URL for results
        fragment_id: Fragment ID for results
        item_id: Item ID for results
        item_title: Item title for results
    
    Returns:
        List of matching word entries with timing information
    """
    matches = []
    compare_pattern = pattern if case_sensitive else pattern.lower()
    
    for i, word in enumerate(words_data):
        word_text = word.get("text", "")
        word_clean = word_text.strip(".,!?:;\"'-")
        compare_word = word_clean if case_sensitive else word_clean.lower()
        
        is_match = False
        
        if mode == "starts_with":
            is_match = compare_word.startswith(compare_pattern)
            
        elif mode == "ends_with":
            is_match = compare_word.endswith(compare_pattern)
            
        elif mode == "contains":
            is_match = compare_pattern in compare_word
            
        elif mode == "word_length":
            word_len = len(word_clean)
            if min_length and max_length:
                is_match = min_length <= word_len <= max_length
            elif min_length:
                is_match = word_len >= min_length
            elif max_length:
                is_match = word_len <= max_length
            else:
                is_match = word_len == int(pattern) if pattern.isdigit() else False
                
        elif mode == "repeating_letters":
            if pattern:
                char_count = compare_word.count(compare_pattern)
                is_match = char_count >= 2
            else:
                # Find any repeated letters
                is_match = any(compare_word.count(c) >= 2 for c in set(compare_word))
                
        elif mode == "palindrome":
            is_match = _is_palindrome(word_clean)
            
        elif mode == "rhyme":
            if pattern:
                is_match = _get_rhyme_ending(compare_word) == _get_rhyme_ending(compare_pattern)
                
        elif mode == "syllable_count":
            target = syllables if syllables else (int(pattern) if pattern.isdigit() else 0)
            if target:
                is_match = _estimate_syllables(word_clean) == target
                
        elif mode == "alliteration":
            # Check if this word and next word start with same letter
            if i < len(words_data) - 1:
                next_word = words_data[i + 1].get("text", "").strip(".,!?:;\"'-")
                if compare_word and next_word:
                    next_compare = next_word if case_sensitive else next_word.lower()
                    is_match = compare_word[0] == next_compare[0]
                    
        elif mode == "compound":
            # Check if word contains the pattern as a component
            if pattern and len(compare_word) > len(compare_pattern):
                is_match = compare_pattern in compare_word
                
        elif mode == "regex":
            try:
                flags = 0 if case_sensitive else re.IGNORECASE
                is_match = bool(re.search(pattern, word_clean, flags))
            except re.error:
                is_match = False
                
        elif mode == "long_words":
            # Words with 10+ characters
            min_len = int(pattern) if pattern and pattern.isdigit() else 10
            is_match = len(word_clean) >= min_len
            
        elif mode == "short_words":
            # Words with 1-3 characters
            max_len = int(pattern) if pattern and pattern.isdigit() else 3
            is_match = 1 <= len(word_clean) <= max_len
        
        if is_match:
            # DB/JSON may return strings; ensure numeric for arithmetic
            start_ms = float(word.get("start", word.get("start_time", 0)) or 0)
            end_ms = float(word.get("end", word.get("end_time", 0)) or 0)
            
            matches.append({
                "id": f"{fragment_id}_{int(start_ms)}_{int(end_ms)}",
                "word": word_text,
                "text": word_text,
                "start_time": start_ms / 1000,
                "end_time": end_ms / 1000,
                "duration": (end_ms - start_ms) / 1000,
                "confidence": word.get("confidence", 0),
                "speaker": word.get("speaker", ""),
                "audio_url": audio_url,
                "fragment_id": fragment_id,
                "item_id": item_id,
                "item_title": item_title,
                "mode": mode,
                "pattern": pattern,
                "word_index": i,
            })
    
    return matches
