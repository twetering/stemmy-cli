"""
Style Analyzer - Formality, rhetoric, tone, and argumentation.

Mirrors: CreateStemmy/WordAnalyzers/StyleAnalyzer.js
"""

import json
from typing import List, Dict, Any, Optional

STYLE_MODES = {
    "formality": {
        "label": "Formality",
        "help": "Detect formal vs informal language",
        "subtypes": {
            "formal": {
                "label": "Formal",
                "patterns": ["welkom bij", "in deze aflevering", "ter introductie",
                           "concluderend", "samenvattend", "tot zover",
                           "daarnaast", "bovendien", "vervolgens", "ten eerste",
                           "desbetreffende", "betreffende", "aangaande", "derhalve"],
            },
            "informal": {
                "label": "Informal", 
                "patterns": ["nou", "ja dus", "zeg maar", "weet je", "toch",
                           "eigenlijk", "gewoon", "zeg", "joh", "goh",
                           "eh", "uhm", "snap je", "zal ik je vertellen",
                           "nou ja", "trouwens", "by the way", "anyway"],
            },
        },
    },
    "rhetoric": {
        "label": "Rhetoric",
        "help": "Find rhetorical devices and patterns",
        "subtypes": {
            "repetition": {
                "label": "Repetition",
                "patterns": ["weer en weer", "keer op keer", "steeds weer",
                           "telkens opnieuw", "zoals gezegd", "zoals ik al zei",
                           "nogmaals", "wat ik bedoel is", "laat ik het zo zeggen"],
            },
            "contrast": {
                "label": "Contrast",
                "patterns": ["enerzijds", "anderzijds", "daarentegen",
                           "aan de ene kant", "aan de andere kant",
                           "maar interessant is dat", "het gekke is dat",
                           "wat opvalt is dat", "in tegenstelling tot"],
            },
            "emphasis": {
                "label": "Emphasis",
                "patterns": ["vooral", "met name", "in het bijzonder", "juist",
                           "wat echt belangrijk is", "waar het om gaat",
                           "let wel", "nota bene", "sterker nog",
                           "wat ik wil benadrukken", "het punt is"],
            },
            "rhetorical_questions": {
                "label": "Rhetorical Questions",
                "patterns": ["is het niet zo dat", "waarom zou", "wie denkt",
                           "vraag je je niet af", "hoe kan het dat",
                           "wat denk jij", "wat vind jij daarvan"],
            },
        },
    },
    "tone": {
        "label": "Tone",
        "help": "Analyze conversation tone",
        "subtypes": {
            "authoritative": {
                "label": "Authoritative",
                "patterns": ["experts zeggen", "onderzoek toont aan", "blijkt uit",
                           "volgens onderzoek", "studies laten zien",
                           "wat we zien is", "wat belangrijk is om te weten"],
            },
            "casual": {
                "label": "Casual",
                "patterns": ["weet je wat", "snap je", "zal ik je vertellen",
                           "grappig genoeg", "toevallig", "best wel",
                           "eigenlijk best", "wat ik zo leuk vind"],
            },
            "humorous": {
                "label": "Humorous",
                "patterns": ["grappig", "hilarisch", "lachen",
                           "het gekke is", "wat zo leuk is",
                           "moeten we even om lachen", "dat is toch fantastisch"],
            },
            "serious": {
                "label": "Serious",
                "patterns": ["serieus", "belangrijk", "cruciaal",
                           "we moeten niet vergeten", "het probleem is",
                           "waar we ons zorgen over maken", "wat kritisch is"],
            },
        },
    },
    "dialog": {
        "label": "Dialog",
        "help": "Find dialog patterns",
        "subtypes": {
            "turn_taking": {
                "label": "Turn Taking",
                "patterns": ["zit", "zegt", "vertelt", "vraagt", "antwoordt",
                           "tegenover mij", "ik ben", "deze week", "gaan we"],
            },
            "agreement": {
                "label": "Agreement",
                "patterns": ["inderdaad", "precies", "klopt", "zeker",
                           "natuurlijk", "absoluut", "helemaal", "exact",
                           "mee eens", "dat denk ik ook"],
            },
            "disagreement": {
                "label": "Disagreement",
                "patterns": ["nee", "niet waar", "oneens", "integendeel",
                           "daarentegen", "echter", "maar", "daar ben ik het niet mee eens"],
            },
        },
    },
    "argumentation": {
        "label": "Argumentation",
        "help": "Find argument structures",
        "subtypes": {
            "premise": {
                "label": "Premise",
                "patterns": ["want", "omdat", "aangezien", "immers",
                           "gezien", "vanwege", "doordat",
                           "als je kijkt naar", "wat we zien is dat"],
            },
            "conclusion": {
                "label": "Conclusion",
                "patterns": ["dus", "daarom", "concludeer", "kortom",
                           "samenvattend", "concluderend", "betekent dat",
                           "wat we hieruit kunnen leren", "waar het op neerkomt"],
            },
            "evidence": {
                "label": "Evidence",
                "patterns": ["blijkt uit", "onderzoek toont", "volgens",
                           "laat zien", "toont aan", "bewijst",
                           "in de praktijk zien we", "uit de cijfers blijkt"],
            },
            "counter": {
                "label": "Counter Argument",
                "patterns": ["echter", "maar", "daarentegen", "toch",
                           "niettemin", "desondanks", "hoewel",
                           "aan de andere kant", "daar staat tegenover"],
            },
        },
    },
}


def _find_sentence_boundaries(words: List[Dict], match_index: int) -> tuple:
    """Find sentence boundaries around a match point."""
    start_idx = match_index
    end_idx = match_index
    
    while start_idx > 0:
        prev_word = words[start_idx - 1]
        prev_text = prev_word.get("text", "")
        current_start = words[start_idx].get("start", 0)
        prev_end = prev_word.get("end", 0)
        
        if any(p in prev_text for p in [".", "!", "?"]):
            break
        if (current_start - prev_end) > 750:
            break
        start_idx -= 1
    
    while end_idx < len(words) - 1:
        current_word = words[end_idx]
        current_text = current_word.get("text", "")
        current_end = current_word.get("end", 0)
        next_start = words[end_idx + 1].get("start", 0)
        
        if any(p in current_text for p in [".", "!", "?"]):
            break
        if (next_start - current_end) > 750:
            break
        end_idx += 1
    
    return start_idx, end_idx


def search_style_pattern(
    words_data: List[Dict],
    mode: str,
    subtype: Optional[str] = None,
    custom_patterns: Optional[List[str]] = None,
    extract_mode: str = "sentence",
    context_words: int = 5,
    audio_url: str = "",
    fragment_id: str = "",
    item_id: str = "",
    item_title: str = "",
) -> List[Dict[str, Any]]:
    """
    Search for style patterns in word-level data.
    
    Args:
        words_data: List of word objects with text, start, end keys
        mode: Style mode from STYLE_MODES
        subtype: Specific subtype within the mode
        custom_patterns: Additional patterns to search
        extract_mode: What to extract (sentence, context, word)
        context_words: Number of context words
        audio_url: Audio URL for results
        fragment_id: Fragment ID for results
        item_id: Item ID for results
        item_title: Item title for results
    
    Returns:
        List of matching entries with timing information
    """
    matches = []
    patterns_to_search = []
    
    # Get patterns from mode/subtype
    if mode in STYLE_MODES:
        mode_config = STYLE_MODES[mode]
        if subtype and "subtypes" in mode_config:
            if subtype in mode_config["subtypes"]:
                patterns_to_search.extend(mode_config["subtypes"][subtype]["patterns"])
        elif "subtypes" in mode_config:
            # Get all patterns from all subtypes
            for sub_config in mode_config["subtypes"].values():
                patterns_to_search.extend(sub_config["patterns"])
    
    # Add custom patterns
    if custom_patterns:
        patterns_to_search.extend(custom_patterns)
    
    if not patterns_to_search:
        return []
    
    for pattern in patterns_to_search:
        pattern_lower = pattern.lower()
        pattern_words = pattern_lower.split()
        pattern_len = len(pattern_words)
        
        for i in range(len(words_data) - pattern_len + 1):
            sequence = " ".join(
                words_data[i + j].get("text", "").strip(".,!?:;\"'-").lower()
                for j in range(pattern_len)
            )
            
            if sequence == pattern_lower or pattern_lower in sequence:
                match_start_idx = i
                match_end_idx = i + pattern_len - 1
                
                if extract_mode == "sentence":
                    start_idx, end_idx = _find_sentence_boundaries(words_data, match_start_idx)
                elif extract_mode == "context":
                    start_idx = max(0, match_start_idx - context_words)
                    end_idx = min(len(words_data) - 1, match_end_idx + context_words)
                else:
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
                    "mode": mode,
                    "subtype": subtype,
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
