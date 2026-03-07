"""
Parallel Audio Segment Extractor for CLI.

Based on services/audioextracter_service.py but optimized for local CLI use:
- Groups segments by audio URL for efficient HTTP range requests
- Parallel extraction using ThreadPoolExecutor
- Smart segment grouping to minimize downloads
- Local file output (no S3)
"""

import subprocess
import tempfile
import time
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
    """
    
    # Grouping parameters
    MAX_TIME_GAP = 10      # Max seconds between segments in a group
    MAX_GROUP_SPAN = 30    # Max total duration a group can span
    MAX_GROUP_SIZE = 5     # Max segments per group
    
    # Audio processing
    BUFFER_BEFORE = 3      # Seconds of buffer before segment
    BUFFER_AFTER = 1       # Seconds of buffer after segment
    DURATION_TOLERANCE = 0.1
    
    # Default audio metadata
    DEFAULT_METADATA = {
        'bit_rate': 128000,
        'sample_rate': 44100,
        'bytes_per_second': 16000
    }
    
    def __init__(self, max_workers: int = 4, output_dir: Optional[Path] = None):
        """
        Initialize extractor.
        
        Args:
            max_workers: Number of parallel extraction threads
            output_dir: Directory for extracted segments (uses temp if not specified)
        """
        self.max_workers = max_workers
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
        Extract multiple audio segments efficiently.
        
        Args:
            segments: List of segment dicts with keys:
                - id: Unique identifier
                - start_time: Start in seconds
                - end_time: End in seconds  
                - audio_url: Source audio URL
            progress_callback: Optional callback(current, total, message)
            
        Returns:
            Tuple of (successful_results, failed_results)
        """
        start_time = time.time()
        
        # Group segments by URL
        segments_by_url: Dict[str, List[Dict]] = {}
        for seg in segments:
            url = seg.get('audio_url') or seg.get('item_audio_url') or seg.get('source_audio_url')
            if not url:
                logger.warning(f"Segment {seg.get('id', 'unknown')} missing audio_url, skipping")
                continue
            
            # Normalize segment format
            normalized = {
                'id': seg.get('id', f"seg_{id(seg)}"),
                'start_time': float(seg.get('start_time', seg.get('start', 0)) or 0),
                'end_time': float(seg.get('end_time', seg.get('end', 0)) or 0),
                'audio_url': url,
                'original': seg  # Keep original for reference
            }
            
            # Handle milliseconds
            if normalized['start_time'] > 10000:
                normalized['start_time'] /= 1000
                normalized['end_time'] /= 1000
            
            if url not in segments_by_url:
                segments_by_url[url] = []
            segments_by_url[url].append(normalized)
        
        all_results: List[ExtractionResult] = []
        all_failed: List[ExtractionResult] = []
        
        total_segments = sum(len(segs) for segs in segments_by_url.values())
        processed = 0
        
        for url, url_segments in segments_by_url.items():
            # Sort by start time
            sorted_segments = sorted(url_segments, key=lambda x: x['start_time'])
            
            # Get audio metadata
            metadata = self._get_audio_metadata(url)
            
            # Create groups
            groups = self._create_groups(sorted_segments)
            
            logger.info(f"Processing {len(url_segments)} segments from {url[:50]}... in {len(groups)} groups")
            
            # Process groups in parallel
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._process_group, group, url, metadata): group
                    for group in groups
                }
                
                for future in as_completed(futures):
                    try:
                        results = future.result()
                        for result in results:
                            if result.success:
                                all_results.append(result)
                            else:
                                all_failed.append(result)
                            
                            processed += 1
                            if progress_callback:
                                progress_callback(processed, total_segments, f"Extracted {processed}/{total_segments}")
                    
                    except Exception as e:
                        group = futures[future]
                        logger.error(f"Group processing failed: {e}")
                        for seg in group:
                            all_failed.append(ExtractionResult(
                                segment_id=seg['id'],
                                output_path="",
                                duration=0,
                                success=False,
                                error=str(e)
                            ))
                            processed += 1
        
        duration = time.time() - start_time
        logger.info(f"Extraction complete: {len(all_results)} success, {len(all_failed)} failed in {duration:.1f}s")
        
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
        
        # Calculate byte range with buffers
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
            output = subprocess.check_output(cmd, timeout=10)
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
