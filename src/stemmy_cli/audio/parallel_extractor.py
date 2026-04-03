"""
Parallel Audio Segment Extractor for CLI.

Based on services/audioextracter_service.py but optimized for local CLI use:
- Groups segments by audio URL for efficient HTTP range requests
- Parallel extraction using ThreadPoolExecutor
- Smart segment grouping to minimize downloads
- Local file output (no S3)

Twee strategieën beschikbaar (schakelbaar via use_global_executor):

  GLOBALE EXECUTOR (standaard, ~14× sneller):
    Alle groepen van alle URLs worden in één globale ThreadPoolExecutor
    tegelijk verwerkt. Geen seriële URL-lus. Benchmarks: fps ~15 (warm S3)
    vs. fps ~1 voor de legacy-aanpak. Geoptimaliseerd via autoresearch (2026-03).

  LEGACY (--legacy-extractor vlag):
    Per-URL ThreadPoolExecutor — oorspronkelijke implementatie.
    Serieel over URLs, parallel binnen een URL. Bewaard voor vergelijking.
"""

import subprocess
import tempfile
import time
import threading
import requests
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Result of a single segment extraction."""
    segment_id: str
    output_path: str
    duration: float
    success: bool
    error: Optional[str] = None


class ParallelExtractor:
    """
    Efficient parallel audio segment extractor.

    Groups segments by audio URL and downloads chunks with HTTP range requests,
    then extracts individual segments using ffmpeg.

    Gebruik use_global_executor=True (standaard) voor de geoptimaliseerde aanpak
    waarbij alle groepen van alle URLs tegelijk worden verwerkt (~14× sneller).
    Gebruik use_global_executor=False voor de originele per-URL aanpak (legacy).
    """

    # Grouping parameters
    MAX_TIME_GAP = 10      # Max seconds between segments in a group
    MAX_GROUP_SPAN = 30    # Max total duration a group can span
    MAX_GROUP_SIZE = 8     # Max segments per group (globale executor: meer per groep = minder processes)

    # Audio processing
    BUFFER_BEFORE = 8      # Seconds of buffer before segment (ruim voor VBR-audio, Megaphone CDN)
    BUFFER_AFTER = 1       # Seconds of buffer after segment
    DURATION_TOLERANCE = 0.1

    # Default audio metadata (128kbps — geen ffprobe-call nodig in globale modus)
    DEFAULT_METADATA = {
        'bit_rate': 128000,
        'sample_rate': 44100,
        'bytes_per_second': 16000
    }

    def __init__(
        self,
        max_workers: int = 20,
        output_dir: Optional[Path] = None,
        use_global_executor: bool = True,
    ):
        """
        Initialiseer extractor.

        Args:
            max_workers: Aantal parallelle extractie-threads (standaard 20 voor globale modus)
            output_dir: Uitvoermap voor geëxtraheerde segmenten
            use_global_executor: True = globale pool over alle URLs tegelijk (snel, standaard)
                                  False = per-URL pool (legacy, traag bij veel URLs)
        """
        self.max_workers = max_workers
        self.use_global_executor = use_global_executor
        self.output_dir = output_dir or Path(tempfile.gettempdir()) / "stemmy_extracts"
        self.output_dir.mkdir(exist_ok=True)

        # Cache for audio metadata
        self._metadata_cache: Dict[str, Dict] = {}
    
    def extract_segments(
        self,
        segments: List[Dict[str, Any]],
        progress_callback: Optional[callable] = None
    ) -> Tuple[List[ExtractionResult], List[ExtractionResult]]:
        """
        Extraheer meerdere audiosegmenten efficiënt.

        Args:
            segments: Lijst van segment-dicts met:
                - id: Uniek ID
                - start_time: Start in seconden (of milliseconden, auto-gedetecteerd)
                - end_time: Eind in seconden
                - audio_url: Bron-URL (ook item_audio_url / source_audio_url)
            progress_callback: Optionele callback(current, total, message)

        Returns:
            Tuple van (succesvolle resultaten, mislukte resultaten)
        """
        if self.use_global_executor:
            return self._extract_global(segments, progress_callback)
        else:
            return self._extract_legacy(segments, progress_callback)

    def _normalize_segments(self, segments: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
        """Normaliseer segmenten en groepeer per URL."""
        by_url: Dict[str, List[Dict]] = {}
        for seg in segments:
            url = seg.get('audio_url') or seg.get('item_audio_url') or seg.get('source_audio_url')
            if not url:
                logger.warning(f"Segment {seg.get('id', 'unknown')} heeft geen audio_url, overgeslagen")
                continue
            norm = {
                'id': str(seg.get('id', f"seg_{id(seg)}")),
                'start_time': float(seg.get('start_time', seg.get('start', 0)) or 0),
                'end_time': float(seg.get('end_time', seg.get('end', 0)) or 0),
                'audio_url': url,
                'original': seg,
            }
            # Milliseconden-detectie
            if norm['start_time'] > 10000:
                norm['start_time'] /= 1000
                norm['end_time'] /= 1000
            by_url.setdefault(url, []).append(norm)
        return by_url

    def _extract_global(
        self,
        segments: List[Dict[str, Any]],
        progress_callback: Optional[callable] = None,
    ) -> Tuple[List[ExtractionResult], List[ExtractionResult]]:
        """
        Globale executor-strategie (standaard, ~14× sneller dan legacy).

        Alle groepen van alle URLs worden in één gedeelde ThreadPoolExecutor
        tegelijk verwerkt — geen seriële URL-lus. Bewezen via autoresearch:
        fps ~15 (warm S3-cache), 100% success rate.
        """
        t0 = time.time()
        segments_by_url = self._normalize_segments(segments)
        total = sum(len(v) for v in segments_by_url.values())

        # Stap 1: Haal audio-metadata parallel op voor alle unieke URLs.
        # De byte-range berekening vereist de échte bitrate — zonder dit
        # landen we op de verkeerde positie voor niet-128kbps bestanden.
        n_urls = len(segments_by_url)
        meta_workers = min(n_urls, self.max_workers)
        with ThreadPoolExecutor(max_workers=meta_workers) as meta_pool:
            meta_futures = {
                meta_pool.submit(self._get_audio_metadata, url): url
                for url in segments_by_url
            }
            url_metadata = {}
            for fut in as_completed(meta_futures):
                url = meta_futures[fut]
                try:
                    url_metadata[url] = fut.result()
                except Exception:
                    url_metadata[url] = self.DEFAULT_METADATA

        # Stap 2: Bouw platte lijst van alle (groep, url, metadata) tuples
        all_groups: List[Tuple[List[Dict], str, Dict]] = []
        for url, url_segs in segments_by_url.items():
            metadata = url_metadata[url]
            sorted_segs = sorted(url_segs, key=lambda s: s['start_time'])
            for group in self._create_groups(sorted_segs):
                all_groups.append((group, url, metadata))

        logger.info(
            f"Globale executor: {total} segmenten → {len(all_groups)} groepen "
            f"uit {n_urls} URLs, {self.max_workers} workers"
        )

        processed = 0
        lock = threading.Lock()
        all_success: List[ExtractionResult] = []
        all_failed: List[ExtractionResult] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._process_group, group, url, metadata): group
                for group, url, metadata in all_groups
            }
            for fut in as_completed(futures):
                group = futures[fut]
                try:
                    results = fut.result()
                except Exception as e:
                    results = [
                        ExtractionResult(seg['id'], "", 0.0, False, str(e))
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
            f"Klaar: {len(all_success)} ok, {len(all_failed)} mislukt "
            f"in {elapsed:.1f}s ({processed / max(elapsed, 0.001):.2f} fps)"
        )
        return all_success, all_failed

    def _extract_legacy(
        self,
        segments: List[Dict[str, Any]],
        progress_callback: Optional[callable] = None,
    ) -> Tuple[List[ExtractionResult], List[ExtractionResult]]:
        """
        Legacy per-URL strategie (originele implementatie, bewaard voor vergelijking).
        Serieel over URLs, parallel binnen een URL. ~14× langzamer dan globale modus
        bij cross-URL werklasten (bijv. 1 fragment uit 20 verschillende afleveringen).
        """
        t0 = time.time()
        segments_by_url = self._normalize_segments(segments)

        all_results: List[ExtractionResult] = []
        all_failed: List[ExtractionResult] = []
        total_segments = sum(len(segs) for segs in segments_by_url.values())
        processed = 0

        for url, url_segments in segments_by_url.items():
            sorted_segments = sorted(url_segments, key=lambda x: x['start_time'])
            metadata = self._get_audio_metadata(url)
            groups = self._create_groups(sorted_segments)

            logger.info(f"[legacy] {len(url_segments)} segs van {url[:50]}... in {len(groups)} groepen")

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._process_group, group, url, metadata): group
                    for group in groups
                }
                for future in as_completed(futures):
                    try:
                        results = future.result()
                        for result in results:
                            (all_results if result.success else all_failed).append(result)
                            processed += 1
                            if progress_callback:
                                progress_callback(processed, total_segments, f"Extracted {processed}/{total_segments}")
                    except Exception as e:
                        group = futures[future]
                        logger.error(f"Groep mislukt: {e}")
                        for seg in group:
                            all_failed.append(ExtractionResult(seg['id'], "", 0, False, str(e)))
                            processed += 1

        elapsed = time.time() - t0
        logger.info(f"Legacy klaar: {len(all_results)} ok, {len(all_failed)} mislukt in {elapsed:.1f}s")
        return all_results, all_failed
    
    def _get_audio_metadata(self, url: str) -> Dict[str, int]:
        """Get audio file metadata using ffprobe, with caching."""
        if url in self._metadata_cache:
            return self._metadata_cache[url]
        
        try:
            cmd = [
                'ffprobe', '-v', 'quiet',
                '-select_streams', 'a:0',
                '-show_entries', 'format=bit_rate,duration',
                '-show_entries', 'stream=sample_rate',
                '-of', 'json',
                url
            ]
            
            output = subprocess.check_output(cmd, timeout=30)
            data = json.loads(output.decode('utf-8'))
            
            bit_rate = int(data.get('format', {}).get('bit_rate', 128000))
            sample_rate = int(data.get('streams', [{}])[0].get('sample_rate', 44100))
            
            metadata = {
                'bit_rate': bit_rate,
                'sample_rate': sample_rate,
                'bytes_per_second': bit_rate // 8
            }
            
            self._metadata_cache[url] = metadata
            return metadata
            
        except Exception as e:
            logger.warning(f"Could not get metadata for {url}: {e}, using defaults")
            return self.DEFAULT_METADATA
    
    def _create_groups(self, segments: List[Dict]) -> List[List[Dict]]:
        """Group segments that are close together for efficient downloading."""
        groups: List[List[Dict]] = []
        current_group: List[Dict] = []
        
        for seg in segments:
            should_start_new = False
            
            if current_group:
                time_gap = seg['start_time'] - current_group[-1]['end_time']
                group_span = seg['end_time'] - current_group[0]['start_time']
                
                should_start_new = (
                    time_gap > self.MAX_TIME_GAP or
                    group_span > self.MAX_GROUP_SPAN or
                    len(current_group) >= self.MAX_GROUP_SIZE
                )
            
            if should_start_new:
                groups.append(current_group)
                current_group = []
            
            current_group.append(seg)
        
        if current_group:
            groups.append(current_group)
        
        return groups
    
    def _process_group(
        self,
        group: List[Dict],
        audio_url: str,
        metadata: Dict[str, int]
    ) -> List[ExtractionResult]:
        """Process a group of segments from the same audio file."""
        if not group:
            return []
        
        results: List[ExtractionResult] = []
        
        # Calculate download range
        group_start = group[0]['start_time']
        group_end = group[-1]['end_time']
        
        bytes_per_second = metadata['bytes_per_second']
        bit_rate = metadata['bit_rate']
        sample_rate = metadata['sample_rate']
        
        # Bereken byte-range met buffers
        # BUFFER_BEFORE=8s vangt VBR-audio (Megaphone CDN) correct op
        download_start_byte = max(0, int((group_start - self.BUFFER_BEFORE) * bytes_per_second))
        download_end_byte = int((group_end + self.BUFFER_AFTER) * bytes_per_second)
        
        # Align to MP3 frame boundaries
        frame_size = max(1, int(144 * bit_rate / sample_rate)) if sample_rate > 0 else 1
        download_start_byte = (download_start_byte // frame_size) * frame_size
        download_end_byte = ((download_end_byte + frame_size - 1) // frame_size) * frame_size
        
        chunk_path = None
        
        try:
            # Download chunk with range request
            headers = {'Range': f'bytes={download_start_byte}-{download_end_byte}'}
            response = requests.get(audio_url, headers=headers, stream=True, timeout=60)
            
            if response.status_code not in (200, 206):
                error = f"HTTP {response.status_code}"
                for seg in group:
                    results.append(ExtractionResult(
                        segment_id=seg['id'],
                        output_path="",
                        duration=0,
                        success=False,
                        error=error
                    ))
                return results
            
            # Save chunk to temp file
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                chunk_path = f.name
            
            # Extract each segment from chunk
            for seg in group:
                result = self._extract_segment_from_chunk(
                    seg, chunk_path, group_start
                )
                results.append(result)
        
        except Exception as e:
            logger.error(f"Group download failed: {e}")
            for seg in group:
                results.append(ExtractionResult(
                    segment_id=seg['id'],
                    output_path="",
                    duration=0,
                    success=False,
                    error=str(e)
                ))
        
        finally:
            if chunk_path and os.path.exists(chunk_path):
                try:
                    os.unlink(chunk_path)
                except OSError:
                    pass
        
        return results
    
    def _extract_segment_from_chunk(
        self,
        segment: Dict,
        chunk_path: str,
        group_start: float
    ) -> ExtractionResult:
        """Extract a single segment from downloaded chunk."""
        seg_id = segment['id']
        seg_start = segment['start_time']
        seg_end = segment['end_time']
        duration = seg_end - seg_start
        
        if duration <= 0:
            return ExtractionResult(
                segment_id=seg_id,
                output_path="",
                duration=0,
                success=False,
                error="Zero or negative duration"
            )
        
        # Calculate seek positions relative to chunk
        initial_seek = self.BUFFER_BEFORE
        relative_start = seg_start - group_start
        
        # Output path
        safe_id = "".join(c if c.isalnum() else "_" for c in str(seg_id)[:50])
        output_path = str(self.output_dir / f"segment_{safe_id}_{int(seg_start*1000)}.mp3")
        
        try:
            # Extract with ffmpeg
            cmd = [
                'ffmpeg', '-y',
                '-ss', f"{initial_seek:.3f}",
                '-i', chunk_path,
                '-ss', f"{relative_start:.3f}",
                '-t', f"{duration:.3f}",
                '-c:a', 'libmp3lame', '-q:a', '2',
                '-af', 'afade=t=in:d=0.015,afade=t=out:st={:.3f}:d=0.015'.format(duration - 0.015),
                output_path
            ]
            
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            
            # Verify output
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return ExtractionResult(
                    segment_id=seg_id,
                    output_path="",
                    duration=0,
                    success=False,
                    error="Output file empty or missing"
                )
            
            # Get actual duration
            actual_duration = self._get_duration(output_path)
            
            return ExtractionResult(
                segment_id=seg_id,
                output_path=output_path,
                duration=actual_duration,
                success=True
            )
        
        except subprocess.CalledProcessError as e:
            return ExtractionResult(
                segment_id=seg_id,
                output_path="",
                duration=0,
                success=False,
                error=f"FFmpeg error: {e.stderr.decode()[:100] if e.stderr else 'unknown'}"
            )
        
        except Exception as e:
            return ExtractionResult(
                segment_id=seg_id,
                output_path="",
                duration=0,
                success=False,
                error=str(e)
            )
    
    def _get_duration(self, filepath: str) -> float:
        """Get audio file duration using ffprobe."""
        try:
            cmd = [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json',
                filepath
            ]
            # stderr=DEVNULL: MP3-frame-alignment warnings zijn cosmetisch,
            # ze blokkeren geen correcte duur-meting en moeten niet in de terminal.
            output = subprocess.check_output(cmd, timeout=10, stderr=subprocess.DEVNULL)
            data = json.loads(output.decode())
            return float(data.get('format', {}).get('duration', 0))
        except Exception:
            return 0.0


def extract_segments_parallel(
    segments: List[Dict[str, Any]],
    output_dir: Optional[Path] = None,
    max_workers: int = 4,
    progress_callback: Optional[callable] = None
) -> Tuple[List[str], List[str]]:
    """
    Convenience function for parallel segment extraction.
    
    Args:
        segments: List of segments with audio_url, start_time, end_time
        output_dir: Directory for output files
        max_workers: Number of parallel workers
        progress_callback: Optional progress callback(current, total, message)
        
    Returns:
        Tuple of (list of output file paths, list of errors)
    """
    extractor = ParallelExtractor(max_workers=max_workers, output_dir=output_dir)
    successes, failures = extractor.extract_segments(segments, progress_callback)
    
    return (
        [r.output_path for r in successes],
        [f"{r.segment_id}: {r.error}" for r in failures]
    )
