"""
Speaker Analyzer - Speaker dynamics and characteristics.

Mirrors: CreateStemmy/WordAnalyzers/SpeakerAnalyzer.js
"""

import json
from typing import List, Dict, Any, Optional
from collections import Counter

SPEAKER_MODES = {
    "turn_taking": {
        "label": "Turn Taking",
        "help": "Detect natural turn-taking moments between speakers",
    },
    "interruptions": {
        "label": "Interruptions",
        "help": "Find moments where speakers interrupt each other",
    },
    "speaker_balance": {
        "label": "Speaker Balance",
        "help": "Analyze speaking time distribution",
    },
    "filler_words": {
        "label": "Filler Words",
        "help": "Find filler words (eh, uhm, etc.)",
        "patterns": ["eh", "uhm", "uh", "euh", "hm", "nou", "ja", "zeg maar",
                     "eigenlijk", "gewoon", "weet je", "snap je"],
    },
    "speaking_pace": {
        "label": "Speaking Pace",
        "help": "Find fast or slow speaking segments",
    },
    "long_pauses": {
        "label": "Long Pauses",
        "help": "Find segments with long pauses between words",
    },
    "speaker_change": {
        "label": "Speaker Change",
        "help": "Find moments of speaker change",
    },
    "monologue": {
        "label": "Monologue Segments",
        "help": "Find long uninterrupted speaker segments",
    },
}

# Common filler words in Dutch and English
FILLER_PATTERNS = [
    "eh", "uhm", "uh", "euh", "hm", "hmm", "mmm",
    "nou", "ja", "zeg maar", "eigenlijk", "gewoon",
    "weet je", "snap je", "dus", "toch", "hè",
    "um", "like", "you know", "basically", "actually",
]


def _calculate_words_per_minute(words: List[Dict]) -> float:
    """Calculate speaking pace in words per minute."""
    if len(words) < 2:
        return 0.0
    
    start_ms = words[0].get("start", 0)
    end_ms = words[-1].get("end", 0)
    duration_minutes = (end_ms - start_ms) / 60000
    
    if duration_minutes <= 0:
        return 0.0
    
    return len(words) / duration_minutes


def search_speaker_pattern(
    words_data: List[Dict],
    mode: str,
    threshold: float = 0.5,
    min_pause_ms: int = 500,
    min_segment_words: int = 20,
    audio_url: str = "",
    fragment_id: str = "",
    item_id: str = "",
    item_title: str = "",
) -> List[Dict[str, Any]]:
    """
    Search for speaker-related patterns in word-level data.
    
    Args:
        words_data: List of word objects with text, start, end, speaker keys
        mode: Speaker analysis mode from SPEAKER_MODES
        threshold: Sensitivity threshold (0-1)
        min_pause_ms: Minimum pause duration to consider
        min_segment_words: Minimum words for monologue detection
        audio_url: Audio URL for results
        fragment_id: Fragment ID for results
        item_id: Item ID for results
        item_title: Item title for results
    
    Returns:
        List of matching entries with timing information
    """
    matches = []
    
    if mode == "filler_words":
        for i, word in enumerate(words_data):
            word_text = word.get("text", "").strip(".,!?:;\"'-").lower()
            
            if word_text in FILLER_PATTERNS:
                start_ms = word.get("start", 0)
                end_ms = word.get("end", 0)
                
                matches.append({
                    "id": f"{fragment_id}_{start_ms}_{end_ms}",
                    "word": word.get("text", ""),
                    "text": word.get("text", ""),
                    "filler_type": word_text,
                    "start_time": start_ms / 1000,
                    "end_time": end_ms / 1000,
                    "duration": (end_ms - start_ms) / 1000,
                    "speaker": word.get("speaker", ""),
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                    "mode": mode,
                })
    
    elif mode == "speaker_change":
        prev_speaker = None
        for i, word in enumerate(words_data):
            current_speaker = word.get("speaker", "")
            
            if prev_speaker is not None and current_speaker != prev_speaker and current_speaker:
                # Speaker changed - capture transition
                start_idx = max(0, i - 2)
                end_idx = min(len(words_data) - 1, i + 2)
                
                context_words = words_data[start_idx:end_idx + 1]
                context_text = " ".join(w.get("text", "") for w in context_words)
                
                start_ms = context_words[0].get("start", 0)
                end_ms = context_words[-1].get("end", 0)
                
                matches.append({
                    "id": f"{fragment_id}_{start_ms}_{end_ms}",
                    "text": context_text,
                    "from_speaker": prev_speaker,
                    "to_speaker": current_speaker,
                    "start_time": start_ms / 1000,
                    "end_time": end_ms / 1000,
                    "duration": (end_ms - start_ms) / 1000,
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                    "mode": mode,
                })
            
            prev_speaker = current_speaker
    
    elif mode == "long_pauses":
        # Scale pause threshold by sensitivity
        pause_threshold = int(min_pause_ms * (2 - threshold))  # Higher threshold = shorter pauses
        
        for i in range(1, len(words_data)):
            prev_word = words_data[i - 1]
            curr_word = words_data[i]
            
            prev_end = prev_word.get("end", 0)
            curr_start = curr_word.get("start", 0)
            pause_duration = curr_start - prev_end
            
            if pause_duration >= pause_threshold:
                # Include words around the pause
                start_idx = max(0, i - 2)
                end_idx = min(len(words_data) - 1, i + 1)
                
                context_words = words_data[start_idx:end_idx + 1]
                context_text = " ".join(w.get("text", "") for w in context_words)
                
                start_ms = context_words[0].get("start", 0)
                end_ms = context_words[-1].get("end", 0)
                
                matches.append({
                    "id": f"{fragment_id}_{start_ms}_{end_ms}",
                    "text": context_text,
                    "pause_duration_ms": pause_duration,
                    "pause_position": i,
                    "start_time": start_ms / 1000,
                    "end_time": end_ms / 1000,
                    "duration": (end_ms - start_ms) / 1000,
                    "speaker": curr_word.get("speaker", ""),
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                    "mode": mode,
                })
    
    elif mode == "speaking_pace":
        # Analyze in sliding windows
        window_size = 10
        
        for i in range(0, len(words_data) - window_size + 1, window_size // 2):
            window = words_data[i:i + window_size]
            wpm = _calculate_words_per_minute(window)
            
            # Fast > 180 WPM, Slow < 100 WPM
            is_fast = wpm > 180 and threshold >= 0.5
            is_slow = wpm < 100 and threshold < 0.5
            
            if is_fast or is_slow:
                context_text = " ".join(w.get("text", "") for w in window)
                start_ms = window[0].get("start", 0)
                end_ms = window[-1].get("end", 0)
                
                matches.append({
                    "id": f"{fragment_id}_{start_ms}_{end_ms}",
                    "text": context_text,
                    "words_per_minute": round(wpm, 1),
                    "pace": "fast" if is_fast else "slow",
                    "start_time": start_ms / 1000,
                    "end_time": end_ms / 1000,
                    "duration": (end_ms - start_ms) / 1000,
                    "speaker": window[0].get("speaker", ""),
                    "audio_url": audio_url,
                    "fragment_id": fragment_id,
                    "item_id": item_id,
                    "item_title": item_title,
                    "mode": mode,
                })
    
    elif mode == "monologue":
        # Find long segments by same speaker
        current_speaker = None
        segment_start = 0
        
        for i, word in enumerate(words_data):
            speaker = word.get("speaker", "")
            
            if speaker != current_speaker:
                # Check if previous segment was long enough
                if current_speaker and (i - segment_start) >= min_segment_words:
                    segment_words = words_data[segment_start:i]
                    context_text = " ".join(w.get("text", "") for w in segment_words)
                    
                    start_ms = segment_words[0].get("start", 0)
                    end_ms = segment_words[-1].get("end", 0)
                    
                    matches.append({
                        "id": f"{fragment_id}_{start_ms}_{end_ms}",
                        "text": context_text[:200] + "..." if len(context_text) > 200 else context_text,
                        "speaker": current_speaker,
                        "word_count": len(segment_words),
                        "start_time": start_ms / 1000,
                        "end_time": end_ms / 1000,
                        "duration": (end_ms - start_ms) / 1000,
                        "audio_url": audio_url,
                        "fragment_id": fragment_id,
                        "item_id": item_id,
                        "item_title": item_title,
                        "mode": mode,
                    })
                
                current_speaker = speaker
                segment_start = i
        
        # Check final segment
        if current_speaker and (len(words_data) - segment_start) >= min_segment_words:
            segment_words = words_data[segment_start:]
            context_text = " ".join(w.get("text", "") for w in segment_words)
            
            start_ms = segment_words[0].get("start", 0)
            end_ms = segment_words[-1].get("end", 0)
            
            matches.append({
                "id": f"{fragment_id}_{start_ms}_{end_ms}",
                "text": context_text[:200] + "..." if len(context_text) > 200 else context_text,
                "speaker": current_speaker,
                "word_count": len(segment_words),
                "start_time": start_ms / 1000,
                "end_time": end_ms / 1000,
                "duration": (end_ms - start_ms) / 1000,
                "audio_url": audio_url,
                "fragment_id": fragment_id,
                "item_id": item_id,
                "item_title": item_title,
                "mode": mode,
            })
    
    return matches
