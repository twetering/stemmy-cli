"""
Text Analyzer - Complexity, coherence, and variation analysis.

Mirrors: CreateStemmy/WordAnalyzers/TextAnalyzer.js
"""

import json
from typing import List, Dict, Any, Optional
from collections import Counter

TEXT_MODES = {
    "complexity": {
        "label": "Complexity",
        "help": "Analyze text complexity and readability",
        "metrics": {
            "long_sentences": "Find long sentences (many words)",
            "short_sentences": "Find short, punchy sentences",
            "long_words": "Find sentences with complex/long words",
            "short_words": "Find sentences with simple/short words",
        },
    },
    "coherence": {
        "label": "Coherence",
        "help": "Analyze text coherence and flow",
        "metrics": {
            "transitions": "Find sentences with transition words",
            "topic_shifts": "Find apparent topic shifts",
        },
    },
    "variation": {
        "label": "Variation",
        "help": "Analyze language variation",
        "metrics": {
            "unique_vocabulary": "Find segments with diverse vocabulary",
            "repetitive": "Find segments with repetitive words",
        },
    },
    "questions": {
        "label": "Questions",
        "help": "Find questions in the text",
        "metrics": {
            "all_questions": "Find all questions",
            "wh_questions": "Find who/what/where/when/why/how questions",
            "yes_no": "Find yes/no questions",
        },
    },
    "exclamations": {
        "label": "Exclamations",
        "help": "Find exclamatory statements",
        "metrics": {
            "all_exclamations": "Find all exclamations",
        },
    },
}

# Transition words for coherence analysis
TRANSITION_WORDS = [
    "daarom", "dus", "omdat", "want", "echter", "maar", "bovendien",
    "daarnaast", "vervolgens", "allereerst", "ten slotte", "kortom",
    "namelijk", "immers", "hoewel", "ondanks", "toch", "zelfs",
    "bijvoorbeeld", "zoals", "met andere woorden", "dat wil zeggen",
]

# Question starters
WH_WORDS = ["wie", "wat", "waar", "wanneer", "waarom", "hoe", "welke", "hoeveel"]


def _estimate_syllables(word: str) -> int:
    """Estimate syllable count for a word."""
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
    
    if word.endswith('e') and count > 1:
        count -= 1
    
    return max(1, count)


def _find_sentence_boundaries(words: List[Dict], start_index: int) -> tuple:
    """Find sentence boundaries."""
    start_idx = start_index
    end_idx = start_index
    
    while start_idx > 0:
        prev_word = words[start_idx - 1]
        prev_text = prev_word.get("text", "")
        if any(p in prev_text for p in [".", "!", "?"]):
            break
        current_start = words[start_idx].get("start", 0)
        prev_end = prev_word.get("end", 0)
        if (current_start - prev_end) > 750:
            break
        start_idx -= 1
    
    while end_idx < len(words) - 1:
        current_word = words[end_idx]
        current_text = current_word.get("text", "")
        if any(p in current_text for p in [".", "!", "?"]):
            break
        current_end = current_word.get("end", 0)
        next_start = words[end_idx + 1].get("start", 0)
        if (next_start - current_end) > 750:
            break
        end_idx += 1
    
    return start_idx, end_idx


def _extract_sentences(words_data: List[Dict]) -> List[Dict]:
    """Extract individual sentences from word data."""
    sentences = []
    current_sentence_start = 0
    
    for i, word in enumerate(words_data):
        text = word.get("text", "")
        
        if any(p in text for p in [".", "!", "?"]):
            sentence_words = words_data[current_sentence_start:i + 1]
            if sentence_words:
                sentences.append({
                    "words": sentence_words,
                    "text": " ".join(w.get("text", "") for w in sentence_words),
                    "start_idx": current_sentence_start,
                    "end_idx": i,
                    "start_time": sentence_words[0].get("start", 0),
                    "end_time": sentence_words[-1].get("end", 0),
                    "word_count": len(sentence_words),
                })
            current_sentence_start = i + 1
    
    # Handle final sentence without punctuation
    if current_sentence_start < len(words_data):
        sentence_words = words_data[current_sentence_start:]
        if sentence_words:
            sentences.append({
                "words": sentence_words,
                "text": " ".join(w.get("text", "") for w in sentence_words),
                "start_idx": current_sentence_start,
                "end_idx": len(words_data) - 1,
                "start_time": sentence_words[0].get("start", 0),
                "end_time": sentence_words[-1].get("end", 0),
                "word_count": len(sentence_words),
            })
    
    return sentences


def search_text_pattern(
    words_data: List[Dict],
    mode: str,
    metric: str = "",
    threshold: float = 0.5,
    min_words: int = 0,
    max_words: int = 0,
    audio_url: str = "",
    fragment_id: str = "",
    item_id: str = "",
    item_title: str = "",
) -> List[Dict[str, Any]]:
    """
    Search for text patterns based on complexity, coherence, or variation.
    
    Args:
        words_data: List of word objects with text, start, end keys
        mode: Text analysis mode from TEXT_MODES
        metric: Specific metric within the mode
        threshold: Sensitivity threshold (0-1)
        min_words: Minimum words filter
        max_words: Maximum words filter
        audio_url: Audio URL for results
        fragment_id: Fragment ID for results
        item_id: Item ID for results
        item_title: Item title for results
    
    Returns:
        List of matching entries with timing information
    """
    matches = []
    sentences = _extract_sentences(words_data)
    
    if mode == "complexity":
        for sentence in sentences:
            is_match = False
            match_reason = ""
            
            if metric == "long_sentences":
                # Long sentences: > 15 words adjusted by threshold
                min_length = int(10 + (threshold * 20))  # 10-30 words
                is_match = sentence["word_count"] >= min_length
                match_reason = f"{sentence['word_count']} words"
                
            elif metric == "short_sentences":
                # Short sentences: < 5 words adjusted by threshold
                max_length = int(3 + ((1 - threshold) * 7))  # 3-10 words
                is_match = sentence["word_count"] <= max_length
                match_reason = f"{sentence['word_count']} words"
                
            elif metric == "long_words":
                # Check average word length
                words = [w.get("text", "").strip(".,!?:;\"'-") for w in sentence["words"]]
                avg_length = sum(len(w) for w in words) / len(words) if words else 0
                is_match = avg_length >= (5 + threshold * 5)  # 5-10 avg char length
                match_reason = f"avg {avg_length:.1f} chars/word"
                
            elif metric == "short_words":
                words = [w.get("text", "").strip(".,!?:;\"'-") for w in sentence["words"]]
                avg_length = sum(len(w) for w in words) / len(words) if words else 0
                is_match = avg_length <= (6 - threshold * 3)  # 3-6 avg char length
                match_reason = f"avg {avg_length:.1f} chars/word"
            
            if is_match:
                matches.append({
                    "id": f"{fragment_id}_{sentence['start_time']}_{sentence['end_time']}",
                    "text": sentence["text"],
                    "mode": mode,
                    "metric": metric,
                    "reason": match_reason,
                    "word_count": sentence["word_count"],
                    "start_time": sentence["start_time"] / 1000,
                    "end_time": sentence["end_time"] / 1000,
                    "duration": (sentence["end_time"] - sentence["start_time"]) / 1000,
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                })
    
    elif mode == "coherence":
        if metric == "transitions":
            for sentence in sentences:
                sentence_text = sentence["text"].lower()
                found_transitions = [t for t in TRANSITION_WORDS if t in sentence_text]
                
                if found_transitions:
                    matches.append({
                        "id": f"{fragment_id}_{sentence['start_time']}_{sentence['end_time']}",
                        "text": sentence["text"],
                        "mode": mode,
                        "metric": metric,
                        "transitions_found": found_transitions,
                        "start_time": sentence["start_time"] / 1000,
                        "end_time": sentence["end_time"] / 1000,
                        "duration": (sentence["end_time"] - sentence["start_time"]) / 1000,
                        "audio_url": audio_url,
                        "fragment_id": fragment_id,
                        "item_id": item_id,
                        "item_title": item_title,
                    })
    
    elif mode == "variation":
        if metric == "repetitive":
            for sentence in sentences:
                words = [w.get("text", "").strip(".,!?:;\"'-").lower() 
                        for w in sentence["words"] if len(w.get("text", "")) > 3]
                word_counts = Counter(words)
                repeated = {w: c for w, c in word_counts.items() if c >= 2}
                
                if repeated:
                    matches.append({
                        "id": f"{fragment_id}_{sentence['start_time']}_{sentence['end_time']}",
                        "text": sentence["text"],
                        "mode": mode,
                        "metric": metric,
                        "repeated_words": dict(repeated),
                        "start_time": sentence["start_time"] / 1000,
                        "end_time": sentence["end_time"] / 1000,
                        "duration": (sentence["end_time"] - sentence["start_time"]) / 1000,
                        "audio_url": audio_url,
                        "fragment_id": fragment_id,
                        "item_id": item_id,
                        "item_title": item_title,
                    })
                    
        elif metric == "unique_vocabulary":
            for sentence in sentences:
                if sentence["word_count"] < 5:
                    continue
                words = [w.get("text", "").strip(".,!?:;\"'-").lower() 
                        for w in sentence["words"] if len(w.get("text", "")) > 2]
                unique_ratio = len(set(words)) / len(words) if words else 0
                
                if unique_ratio >= (0.7 + threshold * 0.3):  # 70-100% unique
                    matches.append({
                        "id": f"{fragment_id}_{sentence['start_time']}_{sentence['end_time']}",
                        "text": sentence["text"],
                        "mode": mode,
                        "metric": metric,
                        "unique_ratio": round(unique_ratio, 2),
                        "start_time": sentence["start_time"] / 1000,
                        "end_time": sentence["end_time"] / 1000,
                        "duration": (sentence["end_time"] - sentence["start_time"]) / 1000,
                        "audio_url": audio_url,
                        "fragment_id": fragment_id,
                        "item_id": item_id,
                        "item_title": item_title,
                    })
    
    elif mode == "questions":
        for sentence in sentences:
            text = sentence["text"]
            text_lower = text.lower()
            
            is_question = "?" in text
            is_wh = any(text_lower.startswith(wh) or f" {wh} " in text_lower for wh in WH_WORDS)
            
            should_match = False
            # Default to all_questions if no metric specified
            if metric in ("all_questions", "") and is_question:
                should_match = True
            elif metric == "wh_questions" and is_question and is_wh:
                should_match = True
            elif metric == "yes_no" and is_question and not is_wh:
                should_match = True
            
            if should_match:
                matches.append({
                    "id": f"{fragment_id}_{sentence['start_time']}_{sentence['end_time']}",
                    "text": text,
                    "mode": mode,
                    "metric": metric,
                    "question_type": "wh" if is_wh else "yes_no",
                    "start_time": sentence["start_time"] / 1000,
                    "end_time": sentence["end_time"] / 1000,
                    "duration": (sentence["end_time"] - sentence["start_time"]) / 1000,
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                })
    
    elif mode == "exclamations":
        for sentence in sentences:
            if "!" in sentence["text"]:
                matches.append({
                    "id": f"{fragment_id}_{sentence['start_time']}_{sentence['end_time']}",
                    "text": sentence["text"],
                    "mode": mode,
                    "metric": metric or "all_exclamations",
                    "start_time": sentence["start_time"] / 1000,
                    "end_time": sentence["end_time"] / 1000,
                    "duration": (sentence["end_time"] - sentence["start_time"]) / 1000,
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                })
    
    return matches
