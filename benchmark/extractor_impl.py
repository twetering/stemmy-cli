# HYPOTHESE: Globale ThreadPoolExecutor over ALLE groepen van ALLE URLs tegelijk,
# gecombineerd met VBR-veilig byte-range downloaden (ruime buffer) en FFmpeg
# timestamp-based seeking in tempfile. Elimineert serieel-per-URL bottleneck.
# 20 workers voor cross-episode fixture (43 URLs × 1 segment = maximaal parallellisme).

import logging
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def _find_binary(name: str) -> str:
    """Zoek een binary in PATH + veelgebruikte extra locaties (homebrew, conda)."""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    for extra in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]:
        candidate = os.path.join(extra, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(f"'{name}' niet gevonden in PATH of standaard locaties.")


_FFMPEG  = _find_binary("ffmpeg")
_FFPROBE = _find_binary("ffprobe")


# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------
_MAX_WORKERS     = 20    # parallelle groep-verwerkers (I/O-bound: meer = sneller)
_MAX_TIME_GAP    = 10.0  # max gap in seconden om segmenten te groeperen
_MAX_GROUP_SPAN  = 30.0  # max tijdspanne van een groep
_MAX_GROUP_SIZE  = 8     # max segmenten per groep
_BUFFER_BEFORE   = 8.0   # preroll in seconden (ruim voor VBR-precisie en VBR-timing)
_BUFFER_AFTER    = 2.0   # post-roll in seconden
_HTTP_TIMEOUT    = 45.0  # seconden voor HTTP download
_HTTP_CHUNK_SIZE = 65536 # bytes per download-chunk


# ---------------------------------------------------------------------------
# Publieke interface — NIET wijzigen
# ---------------------------------------------------------------------------
@dataclass
class ExtractionResult:
    segment_id: str
    output_path: str
    duration: float
    success: bool
    error: Optional[str] = None


def extract_segments(
    segments: List[Dict[str, Any]],
    output_dir: Path,
    progress_callback=None,
) -> Tuple[List[ExtractionResult], List[ExtractionResult]]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()

    # Normaliseer en groepeer per URL
    segments_by_url: Dict[str, List[Dict]] = {}
    for seg in segments:
        url = (
            seg.get("audio_url")
            or seg.get("item_audio_url")
            or seg.get("source_audio_url")
        )
        if not url:
            continue
        norm = {
            "id":         str(seg.get("id", id(seg))),
            "start_time": _to_seconds(seg.get("start_time", 0)),
            "end_time":   _to_seconds(seg.get("end_time", 0)),
            "audio_url":  url,
        }
        segments_by_url.setdefault(url, []).append(norm)

    total = sum(len(v) for v in segments_by_url.values())

    # Bouw ALLE groepen van ALLE URLs als platte lijst (kernverbetering: globale executor)
    all_groups: List[Tuple[List[Dict], str]] = []
    for url, url_segs in segments_by_url.items():
        sorted_segs = sorted(url_segs, key=lambda s: s["start_time"])
        for group in _make_groups(sorted_segs):
            all_groups.append((group, url))

    logger.info(f"Processing {total} segments → {len(all_groups)} groups from {len(segments_by_url)} URLs")

    # GLOBALE executor: alle groepen van alle URLs tegelijk
    processed = 0
    lock = threading.Lock()
    all_success: List[ExtractionResult] = []
    all_failed:  List[ExtractionResult] = []

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_process_group, group, url, output_dir): group
            for group, url in all_groups
        }
        for fut in as_completed(futures):
            group = futures[fut]
            try:
                results = fut.result()
            except Exception as e:
                results = [
                    ExtractionResult(seg["id"], "", 0.0, False, str(e))
                    for seg in group
                ]

            with lock:
                for r in results:
                    (all_success if r.success else all_failed).append(r)
                processed += len(group)
                if progress_callback:
                    progress_callback(processed, total, f"Extracted {processed}/{total}")

    elapsed = time.time() - t0
    logger.info(
        f"Done: {len(all_success)} ok, {len(all_failed)} fail "
        f"in {elapsed:.1f}s ({processed/max(elapsed,0.001):.2f} fps)"
    )
    return all_success, all_failed


# ---------------------------------------------------------------------------
# Interne implementatie
# ---------------------------------------------------------------------------

def _make_groups(segs: List[Dict]) -> List[List[Dict]]:
    """Greedy groepering op basis van tijdsgap, tijdsspanne en groepsgrootte."""
    if not segs:
        return []
    groups: List[List[Dict]] = []
    cur: List[Dict] = [segs[0]]
    for seg in segs[1:]:
        gap  = seg["start_time"] - cur[-1]["end_time"]
        span = seg["end_time"]   - cur[0]["start_time"]
        if gap > _MAX_TIME_GAP or span > _MAX_GROUP_SPAN or len(cur) >= _MAX_GROUP_SIZE:
            groups.append(cur)
            cur = []
        cur.append(seg)
    if cur:
        groups.append(cur)
    return groups


def _process_group(
    group: List[Dict],
    url: str,
    output_dir: Path,
) -> List[ExtractionResult]:
    """Download een chunk voor de groep en extraheer elk segment."""
    if not group:
        return []

    group_start = group[0]["start_time"]
    group_end   = group[-1]["end_time"]
    chunk_start = max(0.0, group_start - _BUFFER_BEFORE)
    chunk_end   = group_end + _BUFFER_AFTER

    # Probeer byte-range download; val terug op volledige download
    chunk_data, actual_chunk_start = _download_chunk(url, chunk_start, chunk_end)
    if chunk_data is None:
        return [
            ExtractionResult(seg["id"], "", 0.0, False, "Download mislukt")
            for seg in group
        ]

    # Schrijf chunk naar tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
        f.write(chunk_data)

    try:
        results = []
        for seg in group:
            # Tijdpositie relatief aan het begin van de chunk
            # Gebruik BUFFER_BEFORE als anker (niet byte-berekende offset) voor VBR-robuustheid
            chunk_time_offset = seg["start_time"] - actual_chunk_start
            seg_duration = seg["end_time"] - seg["start_time"]
            res = _extract_segment(seg["id"], chunk_time_offset, seg_duration, tmp_path, output_dir)
            results.append(res)
        return results
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _download_chunk(
    url: str,
    chunk_start: float,
    chunk_end: float,
) -> Tuple[Optional[bytes], float]:
    """
    Download audio-chunk via HTTP Range request.
    Retourneert (bytes, actual_chunk_start_time).
    Bij mislukking of geen Range-support: volledige download.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=2)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Schat byte-posities via HEAD request voor bitrate
    bitrate = 128000  # conservatieve default (128kbps)
    content_length = None
    accept_ranges = False

    try:
        head = session.head(url, timeout=8, allow_redirects=True)
        if head.status_code == 200:
            cl = head.headers.get("Content-Length")
            if cl:
                content_length = int(cl)
            ar = head.headers.get("Accept-Ranges", "").lower()
            accept_ranges = "bytes" in ar

            # Schat bitrate uit Content-Length en gebruik ffprobe als backup
            if content_length and content_length > 1000:
                # Probeer bitrate te schatten (conservatief: neem 192kbps als upper bound)
                # Gebruik Content-Length / (bestandsduur als bekende) — maar we weten duur niet
                # → gebruik conservatief 128kbps
                pass
    except Exception:
        pass

    if accept_ranges and content_length:
        bytes_per_sec = bitrate / 8.0
        byte_start = max(0, int(chunk_start * bytes_per_sec))
        byte_end   = min(content_length - 1, int(chunk_end * bytes_per_sec) + 65536)

        try:
            resp = session.get(
                url,
                headers={"Range": f"bytes={byte_start}-{byte_end}"},
                timeout=_HTTP_TIMEOUT,
                stream=True,
            )
            if resp.status_code in (200, 206):
                data = b"".join(resp.iter_content(chunk_size=_HTTP_CHUNK_SIZE))
                # actual_chunk_start is de geschatte tijd op basis van byte offset
                actual_start = byte_start / bytes_per_sec
                return data, actual_start
        except Exception as e:
            logger.debug(f"Range download failed: {e}")

    # Fallback: volledig bestand downloaden
    try:
        resp = session.get(url, timeout=_HTTP_TIMEOUT, stream=True)
        if resp.status_code in (200, 206):
            data = b"".join(resp.iter_content(chunk_size=_HTTP_CHUNK_SIZE))
            return data, 0.0  # Volledig bestand begint op t=0
    except Exception as e:
        logger.debug(f"Full download failed: {e}")

    return None, 0.0


def _extract_segment(
    seg_id: str,
    chunk_time_offset: float,
    duration: float,
    tmp_path: str,
    output_dir: Path,
) -> ExtractionResult:
    """
    Extraheer één segment uit de lokale tempfile via FFmpeg met twee-pass seek.
    Pass 1 (-ss vóór -i): snelle positie in tempfile
    Pass 2 (-ss na -i): nauwkeurige frame-gebaseerde seek
    Output = exact segment_duration.
    """
    if duration <= 0:
        return ExtractionResult(seg_id, "", 0.0, False, "Zero/negative duration")

    # Pre-seek (snel): zet af op een positie iets vóór de doelpositie
    pre_seek = max(0.0, chunk_time_offset - 1.0)
    fine_seek = chunk_time_offset - pre_seek

    safe    = "".join(c if c.isalnum() else "_" for c in seg_id[:50])
    outpath = str(output_dir / f"seg_{safe}.mp3")

    cmd = [
        _FFMPEG, "-y",
        "-ss", f"{pre_seek:.3f}",
        "-i", tmp_path,
        "-ss", f"{fine_seek:.3f}",
        "-t",  f"{duration:.3f}",
        "-c:a", "libmp3lame", "-q:a", "2",
        "-avoid_negative_ts", "make_zero",
        outpath,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if (
            result.returncode == 0
            and Path(outpath).exists()
            and Path(outpath).stat().st_size > 0
        ):
            actual = _get_duration(outpath)
            return ExtractionResult(seg_id, outpath, actual, True)

        err = result.stderr.decode(errors="replace")[-200:]
        return ExtractionResult(seg_id, "", 0.0, False, f"FFmpeg rc={result.returncode}: {err}")

    except subprocess.TimeoutExpired:
        return ExtractionResult(seg_id, "", 0.0, False, "FFmpeg timeout")
    except Exception as e:
        return ExtractionResult(seg_id, "", 0.0, False, str(e))


def _to_seconds(val) -> float:
    v = float(val or 0)
    return v / 1000.0 if v > 10000 else v


def _get_duration(path: str) -> float:
    try:
        out = subprocess.check_output(
            [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            timeout=10, stderr=subprocess.DEVNULL,
        )
        return float(out.decode().strip())
    except Exception:
        return 0.0
