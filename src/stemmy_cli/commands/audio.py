"""Audio utility commands."""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console

from stemmy_cli.adapters.http import HTTPAdapter
from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.output import (
    output_result, 
    print_error, 
    print_success, 
    print_info,
    status_spinner,
)
from stemmy_cli.viz import print_audio_preview

app = typer.Typer(help="Audio utilities")
console = Console()

# Create effects subcommand group
effects_app = typer.Typer(help="Audio effects and processing")
app.add_typer(effects_app, name="effects")


def _download_audio(url: str, output_path: str) -> None:
    """Download audio from URL to local file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with httpx.Client(timeout=120.0) as client:
        response = client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)


@app.command()
def clean(
    audio_url: str = typer.Argument(..., help="URL to audio file"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for cleaning to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Clean audio (remove noise, normalize)."""
    try:
        http = HTTPAdapter()

        payload = {"audio_url": audio_url}

        if wait:
            print_info("Cleaning audio...")
            result = http.start_and_wait(
                "/api/clean-audio",
                payload,
                "/api/task-status/{task_id}",
                verbose=True,
            )
        else:
            result = http.post("/api/clean-audio", json=payload)
            print_success(f"Cleaning started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Clean audio result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def separate(
    audio_url: str = typer.Argument(..., help="URL to audio file"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for separation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Separate audio into stems (vocals, accompaniment, etc.)."""
    try:
        http = HTTPAdapter()

        payload = {"audio_url": audio_url}

        if wait:
            print_info("Separating audio (this may take a while)...")
            result = http.start_and_wait(
                "/api/separate-audio",
                payload,
                "/api/task-status/{task_id}",
                poll_interval=5.0,
                max_wait=600.0,
                verbose=True,
            )
        else:
            result = http.post("/api/separate-audio", json=payload)
            print_success(f"Separation started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Audio separation result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("extract-segment")
def extract_segment(
    audio_url: str = typer.Argument(..., help="URL to audio file"),
    start_time: float = typer.Argument(..., help="Start time in seconds"),
    end_time: float = typer.Argument(..., help="End time in seconds"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Extract a single segment from an audio file."""
    try:
        http = HTTPAdapter(timeout=120.0)

        payload = {
            "audioUrl": audio_url,
            "startTime": start_time,
            "endTime": end_time,
        }

        print_info(f"Extracting segment ({start_time}s - {end_time}s)...")
        result = http.post("/api/extract-audio-segment", json=payload)

        extracted_url = result.get("extracted_url") or result.get("audio_url")
        if extracted_url and output:
            print_info(f"Downloading audio...")
            _download_audio(extracted_url, output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="Extraction result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("extract-segments")
def extract_segments(
    item_id: str = typer.Argument(..., help="Item ID"),
    fragment_ids: str = typer.Argument(..., help="Comma-separated fragment IDs"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for extraction to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Extract audio for multiple fragments."""
    try:
        http = HTTPAdapter()
        adapter = SQLiteAdapter()

        ids = [fid.strip() for fid in fragment_ids.split(",")]

        segments = []
        for fid in ids:
            frag = adapter.get_by_id("fragments", fid)
            if frag:
                segments.append({
                    "id": fid,
                    "item_id": frag.get("item_id"),
                    "start_time": frag.get("start_time"),
                    "end_time": frag.get("end_time"),
                    "text": frag.get("text"),
                })

        if not segments:
            print_error("No valid fragments found")
            raise typer.Exit(1)

        payload = {
            "item_id": item_id,
            "segments": segments,
        }

        if wait:
            print_info(f"Extracting audio for {len(segments)} segments...")
            result = http.start_and_wait(
                "/api/extract-audio-segments",
                payload,
                "/api/extract-audio-segments-status/{task_id}",
                poll_interval=2.0,
                verbose=True,
            )
        else:
            result = http.post("/api/extract-audio-segments", json=payload)
            print_success(f"Extraction started: task_id={result.get('task_id')}")

        output_result(result, json_output=json_output, title="Extraction result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("speech-to-speech")
def speech_to_speech(
    audio_url: str = typer.Argument(..., help="URL to audio file"),
    voice_id: str = typer.Option(..., "--voice", "-v", help="Target voice ID"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for conversion to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Convert audio to a different voice using speech-to-speech."""
    try:
        http = HTTPAdapter()

        payload = {
            "audioPath": audio_url,
            "voiceId": voice_id,
        }

        if wait:
            print_info("Converting audio to new voice...")
            result = http.start_and_wait(
                "/api/generate-speech-to-speech",
                payload,
                "/api/speech-to-speech-status/{task_id}",
                poll_interval=3.0,
                max_wait=300.0,
                verbose=True,
            )
        else:
            result = http.post("/api/generate-speech-to-speech", json=payload)
            print_success(f"Conversion started: task_id={result.get('task_id')}")

        if result.get("audio_url") and output:
            _download_audio(result["audio_url"], output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="Speech-to-speech result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command()
def tts(
    text: str = typer.Argument(..., help="Text to convert to speech"),
    voice: str = typer.Option(..., "--voice", "-v", help="Voice ID (use 'stemmy voices list' to see options)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    provider: str = typer.Option("elevenlabs", "--provider", "-p", help="TTS provider: elevenlabs, gemini"),
    stability: float = typer.Option(0.3, "--stability", help="Voice stability (0-1)"),
    similarity: float = typer.Option(0.98, "--similarity", help="Voice similarity boost (0-1)"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for generation to complete"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate speech from text using ElevenLabs TTS."""
    import json
    import uuid
    
    try:
        http = HTTPAdapter(timeout=120.0)
        idempotency_key = str(uuid.uuid4())

        fragment = {
            "id": str(uuid.uuid4()),
            "type": "sentence",
            "text": text,
            "voiceId": voice,
            "provider": provider,
            "ttsProvider": provider,
            "isGenerated": False,
        }

        voicesettings = json.dumps({
            "stability": stability,
            "similarity_boost": similarity,
            "style": 0.5,
            "use_speaker_boost": True,
        })

        payload = {
            "fragments": [fragment],
            "showformat": "cli",
            "voicesettings": voicesettings,
            "intro": {},
            "outro": {},
            "bgaudio": {},
        }

        if wait:
            print_info(f"Generating speech for: '{text[:50]}...' " if len(text) > 50 else f"Generating speech for: '{text}'")
            result = http.start_and_wait(
                "/api/generate-multiple-voices",
                payload,
                "/api/audio-status/{task_id}",
                poll_interval=2.0,
                max_wait=180.0,
                verbose=True,
                headers={"Idempotency-Key": idempotency_key}
            )
        else:
            result = http.post(
                "/api/generate-multiple-voices", 
                json=payload,
                headers={"Idempotency-Key": idempotency_key}
            )
            print_success(f"Generation started: task_id={result.get('task_id')}")
            output_result(result, json_output=json_output, title="TTS result")
            return

        inner_result = result.get("result", {})
        if isinstance(inner_result, str):
            import json as json_mod
            inner_result = json_mod.loads(inner_result)
        
        audio_url = (
            result.get("audio_url") or 
            result.get("audioUrl") or 
            inner_result.get("audio_file") or
            inner_result.get("audio_url")
        )
        
        if audio_url and output:
            print_info("Downloading audio...")
            _download_audio(audio_url, output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="TTS result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("generate-effect")
def generate_effect(
    prompt: str = typer.Argument(..., help="Description of the sound effect"),
    duration: float = typer.Option(5.0, "--duration", "-d", help="Duration in seconds"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save audio to local file"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Generate a sound effect using AI."""
    try:
        http = HTTPAdapter()

        payload = {
            "text": prompt,
            "duration_seconds": duration,
        }

        print_info(f"Generating sound effect: '{prompt}'...")
        result = http.post("/api/generate-sound-effect", json=payload)

        audio_url = result.get("audio_url") or result.get("url")
        if audio_url and output:
            _download_audio(audio_url, output)
            print_success(f"Audio saved to: {output}")

        output_result(result, json_output=json_output, title="Sound effect result")

    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


# =====================
# Audio Effects Commands
# =====================

@effects_app.command("normalize")
def normalize_audio(
    input_file: str = typer.Argument(..., help="Input audio file"),
    output: str = typer.Option(None, "--output", "-o", help="Output file (default: input_normalized.mp3)"),
    target_db: float = typer.Option(-16.0, "--target", "-t", help="Target loudness in dB LUFS"),
):
    """
    Normalize audio to consistent loudness level.
    
    Uses ITU-R BS.1770 loudness normalization for professional results.
    
    Example: stemmy audio effects normalize podcast.mp3 --target -14
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_normalized{p.suffix}")
    
    console.print(f"[cyan]Normalizing to {target_db} dB LUFS...[/cyan]")
    
    with status_spinner("Processing audio...", "Normalized"):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_file,
                "-af", f"loudnorm=I={target_db}:TP=-1.5:LRA=11",
                "-ar", "44100", "-ac", "2",
                "-c:a", "libmp3lame", "-q:a", "2",
                output,
            ],
            capture_output=True,
            text=True,
        )
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Normalization failed: {result.stderr[:200]}")


@effects_app.command("fade")
def fade_audio(
    input_file: str = typer.Argument(..., help="Input audio file"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
    fade_in: int = typer.Option(0, "--in", "-i", help="Fade in duration in milliseconds"),
    fade_out: int = typer.Option(0, "--out", help="Fade out duration in milliseconds"),
):
    """
    Apply fade in/out to audio.
    
    Example: stemmy audio effects fade clip.mp3 --in 500 --out 1000
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if fade_in == 0 and fade_out == 0:
        print_error("Specify --in and/or --out duration")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_faded{p.suffix}")
    
    # Build filter
    filters = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in/1000}")
    if fade_out > 0:
        # Need to get duration first
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_file],
            capture_output=True,
            text=True,
        )
        try:
            duration = float(probe.stdout.strip())
            start = duration - (fade_out / 1000)
            filters.append(f"afade=t=out:st={start}:d={fade_out/1000}")
        except ValueError:
            print_error("Could not determine audio duration")
            raise typer.Exit(1)
    
    filter_str = ",".join(filters)
    
    console.print(f"[cyan]Applying fades: in={fade_in}ms, out={fade_out}ms[/cyan]")
    
    with status_spinner("Processing...", "Complete"):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_file,
                "-af", filter_str,
                "-c:a", "libmp3lame", "-q:a", "2",
                output,
            ],
            capture_output=True,
            text=True,
        )
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Failed: {result.stderr[:200]}")


@effects_app.command("trim-silence")
def trim_silence(
    input_file: str = typer.Argument(..., help="Input audio file"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
    threshold: int = typer.Option(-40, "--threshold", "-t", help="Silence threshold in dB"),
    duration: float = typer.Option(0.5, "--duration", "-d", help="Minimum silence duration to trim (seconds)"),
):
    """
    Remove silence from beginning and end of audio.
    
    Example: stemmy audio effects trim-silence recording.mp3 --threshold -35
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_trimmed{p.suffix}")
    
    console.print(f"[cyan]Trimming silence (threshold: {threshold}dB)...[/cyan]")
    
    # Use silenceremove filter
    filter_str = f"silenceremove=start_periods=1:start_threshold={threshold}dB:start_duration={duration}:stop_periods=-1:stop_threshold={threshold}dB:stop_duration={duration}"
    
    with status_spinner("Processing...", "Complete"):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_file,
                "-af", filter_str,
                "-c:a", "libmp3lame", "-q:a", "2",
                output,
            ],
            capture_output=True,
            text=True,
        )
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Failed: {result.stderr[:200]}")


@effects_app.command("speed")
def change_speed(
    input_file: str = typer.Argument(..., help="Input audio file"),
    factor: float = typer.Argument(..., help="Speed factor (0.5 = half speed, 2.0 = double speed)"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
    preserve_pitch: bool = typer.Option(True, "--pitch/--no-pitch", "-p", help="Preserve pitch"),
):
    """
    Change audio playback speed.
    
    Example: stemmy audio effects speed podcast.mp3 1.25 (25% faster)
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if factor <= 0:
        print_error("Speed factor must be positive")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_{factor}x{p.suffix}")
    
    console.print(f"[cyan]Changing speed to {factor}x...[/cyan]")
    
    if preserve_pitch:
        # Use atempo (supports 0.5 to 2.0, chain for wider range)
        if factor < 0.5:
            filter_str = f"atempo={factor * 2},atempo=0.5"
        elif factor > 2.0:
            filter_str = f"atempo=2.0,atempo={factor / 2}"
        else:
            filter_str = f"atempo={factor}"
    else:
        # Use asetrate for speed without pitch preservation
        filter_str = f"asetrate=44100*{factor},aresample=44100"
    
    with status_spinner("Processing...", "Complete"):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_file,
                "-af", filter_str,
                "-c:a", "libmp3lame", "-q:a", "2",
                output,
            ],
            capture_output=True,
            text=True,
        )
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Failed: {result.stderr[:200]}")


@effects_app.command("trim")
def trim_audio(
    input_file: str = typer.Argument(..., help="Input audio file"),
    start: str = typer.Argument(..., help="Start time (e.g., '00:30' or '30')"),
    end: str = typer.Argument(None, help="End time (optional, default: end of file)"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
):
    """
    Trim audio to specified time range.
    
    Times can be in seconds or mm:ss format.
    
    Example: stemmy audio effects trim podcast.mp3 00:30 02:00 --output clip.mp3
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_trimmed{p.suffix}")
    
    console.print(f"[cyan]Trimming: {start} to {end or 'end'}...[/cyan]")
    
    cmd = ["ffmpeg", "-y", "-i", input_file, "-ss", start]
    if end:
        cmd.extend(["-to", end])
    cmd.extend(["-c:a", "libmp3lame", "-q:a", "2", output])
    
    with status_spinner("Processing...", "Complete"):
        result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Failed: {result.stderr[:200]}")


@effects_app.command("compress")
def compress_audio(
    input_file: str = typer.Argument(..., help="Input audio file"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
    ratio: float = typer.Option(4.0, "--ratio", "-r", help="Compression ratio"),
    threshold: int = typer.Option(-20, "--threshold", "-t", help="Threshold in dB"),
):
    """
    Apply dynamic range compression.
    
    Reduces the difference between loud and quiet parts.
    
    Example: stemmy audio effects compress voice.mp3 --ratio 3 --threshold -18
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_compressed{p.suffix}")
    
    console.print(f"[cyan]Applying compression (ratio: {ratio}, threshold: {threshold}dB)...[/cyan]")
    
    filter_str = f"acompressor=threshold={threshold}dB:ratio={ratio}:attack=5:release=50"
    
    with status_spinner("Processing...", "Complete"):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_file,
                "-af", filter_str,
                "-c:a", "libmp3lame", "-q:a", "2",
                output,
            ],
            capture_output=True,
            text=True,
        )
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Failed: {result.stderr[:200]}")


@effects_app.command("eq")
def equalize_audio(
    input_file: str = typer.Argument(..., help="Input audio file"),
    output: str = typer.Option(None, "--output", "-o", help="Output file"),
    bass: int = typer.Option(0, "--bass", "-b", help="Bass boost/cut in dB (-20 to +20)"),
    mid: int = typer.Option(0, "--mid", "-m", help="Mid boost/cut in dB (-20 to +20)"),
    treble: int = typer.Option(0, "--treble", "-t", help="Treble boost/cut in dB (-20 to +20)"),
):
    """
    Apply 3-band equalizer.
    
    Example: stemmy audio effects eq voice.mp3 --bass -3 --treble +5
    """
    if not Path(input_file).exists():
        print_error(f"File not found: {input_file}")
        raise typer.Exit(1)
    
    if bass == 0 and mid == 0 and treble == 0:
        print_error("Specify at least one EQ band adjustment")
        raise typer.Exit(1)
    
    if output is None:
        p = Path(input_file)
        output = str(p.parent / f"{p.stem}_eq{p.suffix}")
    
    console.print(f"[cyan]Applying EQ: bass={bass}dB, mid={mid}dB, treble={treble}dB[/cyan]")
    
    # Build equalizer filter
    filters = []
    if bass != 0:
        filters.append(f"equalizer=f=100:width_type=o:width=2:g={bass}")
    if mid != 0:
        filters.append(f"equalizer=f=1000:width_type=o:width=2:g={mid}")
    if treble != 0:
        filters.append(f"equalizer=f=8000:width_type=o:width=2:g={treble}")
    
    filter_str = ",".join(filters)
    
    with status_spinner("Processing...", "Complete"):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_file,
                "-af", filter_str,
                "-c:a", "libmp3lame", "-q:a", "2",
                output,
            ],
            capture_output=True,
            text=True,
        )
    
    if result.returncode == 0:
        print_success(f"Created: {output}")
        print_audio_preview(output)
    else:
        print_error(f"Failed: {result.stderr[:200]}")


@effects_app.command("list")
def list_effects():
    """List all available audio effects."""
    effects = [
        ("normalize", "Loudness normalization (LUFS)"),
        ("fade", "Fade in/out effects"),
        ("trim-silence", "Remove silence from start/end"),
        ("speed", "Change playback speed"),
        ("trim", "Trim to time range"),
        ("compress", "Dynamic range compression"),
        ("eq", "3-band equalizer"),
    ]
    
    console.print()
    console.print("[bold]Available Audio Effects[/bold]")
    console.print()
    for name, desc in effects:
        console.print(f"  [cyan]{name:15}[/cyan] {desc}")
    console.print()
    console.print("[dim]Use: stemmy audio effects <effect> --help for details[/dim]")
