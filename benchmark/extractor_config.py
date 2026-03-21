"""
extractor_config.py — AGENT-EDITEERBAAR configuratiebestand voor ParallelExtractor.

Dit is het enige bestand dat de autoresearch-agent mag wijzigen.
Pas ALLEEN de waarden aan in het ExtractorConfig dataclass.
Wijzig NIET de imports, de klasse-definitie of de CONFIG = ExtractorConfig() regel.

Safety constraints worden afgedwongen door evaluate.py en kunnen hier NIET omzeild worden.
"""

from dataclasses import dataclass, field
from typing import List, Literal


@dataclass(frozen=True)
class ExtractorConfig:
    # -------------------------------------------------------------------------
    # Concurrency
    # -------------------------------------------------------------------------
    # Aantal parallelle download/extract threads (1–16)
    max_workers: int = 4

    # Executor strategie: "thread" = ThreadPoolExecutor (standaard, laag overhead)
    # "process" = ProcessPoolExecutor (nuttig bij CPU-gebonden FFmpeg, hogere overhead)
    executor_strategy: Literal["thread", "process"] = "thread"

    # -------------------------------------------------------------------------
    # HTTP Download
    # -------------------------------------------------------------------------
    # Chunk-grootte voor iter_content in bytes
    http_chunk_size: int = 8192

    # Connect/read timeouts voor S3 requests (seconden)
    http_timeout_connect: float = 10.0
    http_timeout_read: float = 60.0

    # Aantal retries bij tijdelijke HTTP-fouten (429, 503, 5xx)
    http_max_retries: int = 2

    # Prefetch de volgende groep terwijl FFmpeg de huidige verwerkt
    # WAARSCHUWING: verhoogt S3 request-concurrentie
    enable_prefetch: bool = False

    # -------------------------------------------------------------------------
    # Groeperingsalgoritme
    # -------------------------------------------------------------------------
    # Max tijdskloof (seconden) tussen twee segmenten in dezelfde groep
    max_time_gap: float = 10.0

    # Max totale tijdspanne (seconden) die een groep mag beslaan
    max_group_span: float = 30.0

    # Max aantal segmenten per downloadgroep
    max_group_size: int = 5

    # Sorteer segmenten op start_time binnen een URL vóór groepering
    sort_before_grouping: bool = True

    # -------------------------------------------------------------------------
    # Audio buffers
    # -------------------------------------------------------------------------
    # Seconden buffer vóór het eerste segment in een groep (min 0.5)
    buffer_before: float = 3.0

    # Seconden buffer ná het laatste segment in een groep (max 3.0)
    buffer_after: float = 1.0

    # -------------------------------------------------------------------------
    # FFmpeg parameters
    # -------------------------------------------------------------------------
    # Audio codec: "libmp3lame" (re-encode) of "copy" (geen transcode, geen fade)
    ffmpeg_codec: str = "libmp3lame"

    # VBR kwaliteit: 0 (best) – 9 (slechtst). Alleen voor libmp3lame.
    ffmpeg_vbr_quality: int = 2

    # Fade-in/fade-out duur in seconden (0 = geen fade, ~15% sneller)
    ffmpeg_fade_duration: float = 0.015

    # Voer FFmpeg-aanroepen binnen een groep parallel uit
    # (elk segment in eigen subprocess tegelijk)
    ffmpeg_parallel_within_group: bool = False

    # Extra FFmpeg-flags als lijst (bijv. ["-threads", "1"])
    ffmpeg_extra_flags: List[str] = field(default_factory=list)


# Singleton — evaluate.py importeert dit object
CONFIG = ExtractorConfig()
