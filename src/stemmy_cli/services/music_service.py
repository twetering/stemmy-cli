"""
Music service for royalty-free background tracks.

NOTE: Pixabay does not have a public Music API (only Images/Videos).
This service provides a framework for music search that can be extended
with alternative sources like Free Music Archive, or local music files.

For now, it provides placeholder functionality that explains how to
manually source royalty-free music from Pixabay.
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


PIXABAY_MUSIC_PAGE = "https://pixabay.com/music/"


def get_api_key() -> Optional[str]:
    """Get Pixabay API key from environment."""
    return os.environ.get("PIXABAY_API_KEY")


def search_music(
    query: str = "",
    genre: Optional[str] = None,
    mood: Optional[str] = None,
    min_duration: int = 0,
    max_duration: int = 0,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Search for royalty-free music suggestions.
    
    NOTE: Pixabay does not have a public Music API. This returns
    curated suggestions based on common royalty-free music sources.
    
    Args:
        query: Search keywords (e.g., "upbeat electronic")
        genre: Filter by genre (e.g., "electronic", "jazz", "rock")
        mood: Filter by mood (e.g., "happy", "sad", "energetic")
        min_duration: Minimum duration in seconds
        max_duration: Maximum duration in seconds (0 = no limit)
        limit: Maximum results to return
    
    Returns:
        List of music suggestions with source URLs
    """
    # Since there's no public Pixabay Music API, return helpful suggestions
    suggestions = [
        {
            "id": 1,
            "title": "Pixabay Music Library",
            "description": f"Search for '{query or 'background music'}' on Pixabay",
            "source_url": f"https://pixabay.com/music/search/{query.replace(' ', '%20') if query else 'background'}/" ,
            "license": "Pixabay License (free for commercial use)",
            "how_to_use": "Download MP3 manually from Pixabay, then use layer_audio_tracks tool",
        },
        {
            "id": 2,
            "title": "Free Music Archive",
            "description": f"Search for '{genre or 'ambient'}' music",
            "source_url": f"https://freemusicarchive.org/search?quicksearch={query or genre or 'ambient'}",
            "license": "Various Creative Commons licenses",
            "how_to_use": "Check license, download, then use layer_audio_tracks tool",
        },
        {
            "id": 3,
            "title": "YouTube Audio Library",
            "description": "Free music for creators",
            "source_url": "https://studio.youtube.com/channel/UC/music",
            "license": "Free for YouTube videos, check terms for other uses",
            "how_to_use": "Download from YouTube Studio, then use layer_audio_tracks tool",
        },
    ]
    
    return suggestions[:limit]


def download_track(
    audio_url: str,
    output_path: Optional[str] = None,
    track_id: Optional[int] = None,
) -> str:
    """
    Download a music track from a URL.
    
    Args:
        audio_url: URL to the audio file (must be a direct MP3 link)
        output_path: Where to save the file (optional)
        track_id: Track ID for default filename
    
    Returns:
        Path to the downloaded file
    """
    if not output_path:
        filename = f"music_{track_id or 'track'}.mp3"
        output_path = str(Path(tempfile.gettempdir()) / "stemmy_music" / filename)
    
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get(audio_url)
            response.raise_for_status()
            
            with open(output_path, "wb") as f:
                f.write(response.content)
        
        return output_path
    
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to download track: {e}")


def search_and_download(
    query: str = "background music",
    genre: Optional[str] = None,
    mood: Optional[str] = None,
    duration_range: Optional[tuple] = None,
) -> Dict[str, Any]:
    """
    Get music suggestions (manual download required).
    
    NOTE: Since Pixabay doesn't have a Music API, this returns
    suggestions for where to find royalty-free music.
    
    Args:
        query: Search keywords
        genre: Music genre filter
        mood: Mood filter
        duration_range: (min_seconds, max_seconds) tuple
    
    Returns:
        Dict with music suggestions
    """
    suggestions = search_music(
        query=query,
        genre=genre,
        mood=mood,
        limit=3,
    )
    
    return {
        "success": True,
        "message": "Pixabay doesn't have a public Music API. Here are suggestions:",
        "suggestions": suggestions,
        "instructions": [
            "1. Visit one of the source URLs above",
            "2. Search for and download a royalty-free track",
            "3. Save the MP3 locally (e.g., /tmp/music.mp3)",
            "4. Use layer_audio_tracks tool to mix with your compilation",
        ],
    }
