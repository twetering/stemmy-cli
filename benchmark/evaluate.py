"""
benchmark/evaluate.py — VASTE evaluation engine.

Meet de extraction_score van de huidige extractor_config.py.
De agent wijzigt dit bestand NOOIT.

Gebruik:
    python benchmark/evaluate.py                   # Draai experiment
    python benchmark/evaluate.py --save-baseline   # Sla resultaat op als baseline
    python benchmark/evaluate.py --fixture holdout # Gebruik holdout set
    python benchmark/evaluate.py --budget 120      # Tijdbudget in seconden
"""

import argparse
import ast
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Zorg dat de stemmy_cli src beschikbaar is
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from prepare import DATASET_PATH, HOLDOUT_PATH, load_fixture

RESULTS_DIR = Path(__file__).parent / "results"
EXPERIMENTS_DIR = RESULTS_DIR / "experiments"
BASELINE_PATH = Path(__file__).parent / "BASELINE_SCORE"
BASELINE_JSON = RESULTS_DIR / "baseline.json"

DEFAULT_BUDGET_SEC = 90


# ---------------------------------------------------------------------------
# Safety constraints — niet omzeilbaar via extractor_config.py
# ---------------------------------------------------------------------------
CONSTRAINTS = {
    "max_workers": (1, 16),
    "buffer_before": (0.5, 5.0),
    "buffer_after": (0.0, 3.0),
    "http_timeout_read": (10.0, 300.0),
    "max_group_size": (1, 20),
    "max_time_gap": (1.0, 60.0),
    "max_group_span": (5.0, 120.0),
    "ffmpeg_vbr_quality": (0, 9),
}


class ConfigViolation(Exception):
    pass


# ---------------------------------------------------------------------------
# Resultaat dataclass
# ---------------------------------------------------------------------------
@dataclass
class ExperimentResult:
    experiment_id: str
    config_hash: str
    git_commit: str
    timestamp: str

    # PRIMAIRE METRIC
    extraction_score: float

    # Throughput
    total_time_sec: float
    fragments_per_second: float
    baseline_fps: float
    median_group_time_sec: float
    p95_group_time_sec: float

    # Kwaliteit
    mean_duration_error_sec: float
    silence_ratio: float
    clipping_ratio: float

    # Betrouwbaarheid
    success_count: int
    failure_count: int
    success_rate: float

    # Netwerk
    mean_http_time_sec: float
    total_bytes_downloaded: int
    network_penalty_applied: bool

    # Resources
    peak_memory_mb: float

    # Meta
    fixture_path: str = ""
    config_summary: str = ""
    error: Optional[str] = None


def _git_short_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _make_experiment_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"e-{ts}-{_git_short_hash()}"


def _import_config():
    """Importeer (of herlaad) extractor_config module."""
    config_path = Path(__file__).parent / "extractor_config.py"
    spec = importlib.util.spec_from_file_location("extractor_config", config_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _config_hash(config_path: Path) -> str:
    import hashlib
    return hashlib.sha256(config_path.read_bytes()).hexdigest()[:12]


def validate_config(cfg) -> None:
    """Controleer veiligheidsgrenzen. Gooit ConfigViolation als iets fout is."""
    for field, (lo, hi) in CONSTRAINTS.items():
        val = getattr(cfg, field, None)
        if val is None:
            continue
        if not (lo <= val <= hi):
            raise ConfigViolation(
                f"{field}={val} valt buiten toegestaan bereik [{lo}, {hi}]"
            )
    if cfg.ffmpeg_codec not in ("libmp3lame", "copy"):
        raise ConfigViolation(f"Onbekende ffmpeg_codec: {cfg.ffmpeg_codec}")
    if cfg.executor_strategy not in ("thread", "process"):
        raise ConfigViolation(f"Onbekende executor_strategy: {cfg.executor_strategy}")


def _config_summary(cfg) -> str:
    return (
        f"workers={cfg.max_workers} "
        f"gap={cfg.max_time_gap}s "
        f"span={cfg.max_group_span}s "
        f"size={cfg.max_group_size} "
        f"buf={cfg.buffer_before}/{cfg.buffer_after}s "
        f"fade={cfg.ffmpeg_fade_duration}s "
        f"codec={cfg.ffmpeg_codec}"
    )


# ---------------------------------------------------------------------------
# Kwaliteitsmetrics via FFmpeg
# ---------------------------------------------------------------------------

def measure_silence_ratio(output_path: str, boundary_ms: int = 100) -> float:
    """
    Meet het percentage stilte in de eerste en laatste 100ms van een segment.
    Hoog = buffer_before/after te klein of timing fout.
    """
    try:
        duration_cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", output_path
        ]
        total = float(subprocess.check_output(duration_cmd, timeout=10).decode().strip())
        if total < 0.3:
            return 0.0

        check_dur = min(boundary_ms / 1000.0, total / 4)
        cmd = [
            "ffmpeg", "-hide_banner", "-i", output_path,
            "-af", f"silencedetect=noise=-55dB:d=0.01",
            "-vn", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        stderr = result.stderr.decode()

        silence_dur = 0.0
        lines = stderr.split("\n")
        for line in lines:
            if "silence_duration" in line:
                try:
                    silence_dur += float(line.split("silence_duration:")[-1].strip())
                except ValueError:
                    pass

        return min(1.0, silence_dur / max(total, 0.001))
    except Exception:
        return 0.0


def measure_clipping_ratio(output_path: str) -> float:
    """Meet het aandeel samples dichtbij 0dBFS (clipping-indicatie)."""
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-i", output_path,
            "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.Max_level",
            "-vn", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        stderr = result.stderr.decode()
        max_level = -999.0
        for line in stderr.split("\n"):
            if "Max_level" in line and "=" in line:
                try:
                    val = float(line.split("=")[-1].strip())
                    max_level = max(max_level, val)
                except ValueError:
                    pass
        # Max_level >= -1 dBFS = potentieel clipping
        return 1.0 if max_level >= -1.0 else 0.0
    except Exception:
        return 0.0


def measure_actual_duration(output_path: str) -> float:
    """Gebruik ffprobe om werkelijke duur te meten."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", output_path
        ]
        return float(subprocess.check_output(cmd, timeout=10).decode().strip())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Geconfigureerde extractor aanmaken
# ---------------------------------------------------------------------------

def build_configured_extractor(cfg, output_dir: Path):
    """
    Maak een ParallelExtractor subklasse met de opgegeven config-waarden.
    Wijzigt parallel_extractor.py NIET — gebruikt dynamische subklasse.
    """
    from stemmy_cli.audio.parallel_extractor import ParallelExtractor

    class ConfiguredExtractor(ParallelExtractor):
        MAX_TIME_GAP = cfg.max_time_gap
        MAX_GROUP_SPAN = cfg.max_group_span
        MAX_GROUP_SIZE = cfg.max_group_size
        BUFFER_BEFORE = cfg.buffer_before
        BUFFER_AFTER = cfg.buffer_after

    return ConfiguredExtractor(max_workers=cfg.max_workers, output_dir=output_dir)


# ---------------------------------------------------------------------------
# Tijdgebudgetteerde evaluator
# ---------------------------------------------------------------------------

class TimeBudgetedEvaluator:
    def __init__(self, budget_sec: int = DEFAULT_BUDGET_SEC):
        self.budget_sec = budget_sec
        self._timed_out = False

    def run(
        self,
        fixture_data: List[Dict],
        fixture_path: str = "",
        experiment_id: Optional[str] = None,
    ) -> ExperimentResult:
        config_mod = _import_config()
        cfg = config_mod.CONFIG
        exp_id = experiment_id or _make_experiment_id()
        cfg_hash = _config_hash(Path(__file__).parent / "extractor_config.py")
        git_commit = _git_short_hash()
        timestamp = datetime.now(timezone.utc).isoformat()

        print(f"\n{'='*60}")
        print(f"Experiment: {exp_id}")
        print(f"Config:     {_config_summary(cfg)}")
        print(f"Budget:     {self.budget_sec}s | Segmenten: {len(fixture_data)}")
        print(f"{'='*60}")

        # Safety check
        try:
            validate_config(cfg)
        except ConfigViolation as e:
            print(f"✗ CONFIG VIOLATION: {e}")
            return self._error_result(exp_id, cfg_hash, git_commit, timestamp,
                                      fixture_path, cfg, str(e))

        # Tijdelijk output-dir
        with tempfile.TemporaryDirectory(prefix="stemmy_bench_") as tmpdir:
            output_dir = Path(tmpdir)

            try:
                extractor = build_configured_extractor(cfg, output_dir)
            except Exception as e:
                return self._error_result(exp_id, cfg_hash, git_commit, timestamp,
                                          fixture_path, cfg, f"Extractor init fout: {e}")

            # Netwerk baseline meten
            unique_urls = list({s["audio_url"] for s in fixture_data})[:3]
            http_times = self._measure_network_baseline(unique_urls)
            mean_http = sum(http_times.values()) / max(len(http_times), 1)
            network_penalty = any(t > mean_http * 2.5 for t in http_times.values())

            # Extractie draaien met tijdlimiet
            self._timed_out = False
            successes = []
            failures = []
            group_times = []
            bytes_downloaded = 0
            peak_memory_mb = 0.0

            extraction_start = time.time()
            done_event = threading.Event()
            result_holder = [None]

            def run_extraction():
                try:
                    s, f = extractor.extract_segments(
                        fixture_data,
                        progress_callback=self._progress_callback
                    )
                    result_holder[0] = (s, f)
                except Exception as e:
                    result_holder[0] = ([], [])
                    print(f"  Extractie-fout: {e}")
                finally:
                    done_event.set()

            thread = threading.Thread(target=run_extraction, daemon=True)
            thread.start()
            finished = done_event.wait(timeout=self.budget_sec)

            if not finished:
                self._timed_out = True
                print(f"  ⏱ Tijdslimiet ({self.budget_sec}s) bereikt — partieel resultaat")

            total_time = time.time() - extraction_start

            if result_holder[0]:
                successes, failures = result_holder[0]
            else:
                successes, failures = [], []

            # Geheugen schatten (via /proc/self/status op Linux, best-effort)
            try:
                import resource
                peak_memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            except Exception:
                pass

            # Kwaliteitsmetrics meten
            silence_vals = []
            clipping_vals = []
            duration_errors = []

            for res in successes[:20]:  # Sample eerste 20 voor snelheid
                if res.output_path and Path(res.output_path).exists():
                    expected = next(
                        (s["end_time"] - s["start_time"] for s in fixture_data
                         if s["id"] == res.segment_id),
                        None
                    )
                    actual = measure_actual_duration(res.output_path)
                    if expected and expected > 0:
                        duration_errors.append(abs(actual - expected))

                    silence_vals.append(measure_silence_ratio(res.output_path))
                    clipping_vals.append(measure_clipping_ratio(res.output_path))

            # Metrics berekenen
            n_success = len(successes)
            n_failure = len(failures)
            n_total = n_success + n_failure
            success_rate = n_success / max(n_total, 1)

            fps = n_success / max(total_time, 0.001)
            silence_ratio = sum(silence_vals) / max(len(silence_vals), 1)
            clipping_ratio = sum(clipping_vals) / max(len(clipping_vals), 1)
            mean_dur_error = sum(duration_errors) / max(len(duration_errors), 1)

            # Baseline fps laden voor normalisatie
            baseline_fps = self._load_baseline_fps()

            # PRIMAIRE METRIC
            score = self._compute_score(fps, baseline_fps, silence_ratio, clipping_ratio, success_rate)

            result = ExperimentResult(
                experiment_id=exp_id,
                config_hash=cfg_hash,
                git_commit=git_commit,
                timestamp=timestamp,
                extraction_score=round(score, 6),
                total_time_sec=round(total_time, 3),
                fragments_per_second=round(fps, 4),
                baseline_fps=round(baseline_fps, 4),
                median_group_time_sec=0.0,
                p95_group_time_sec=0.0,
                mean_duration_error_sec=round(mean_dur_error, 4),
                silence_ratio=round(silence_ratio, 4),
                clipping_ratio=round(clipping_ratio, 4),
                success_count=n_success,
                failure_count=n_failure,
                success_rate=round(success_rate, 4),
                mean_http_time_sec=round(mean_http, 3),
                total_bytes_downloaded=0,
                network_penalty_applied=network_penalty,
                peak_memory_mb=round(peak_memory_mb, 1),
                fixture_path=fixture_path,
                config_summary=_config_summary(cfg),
            )

            self._print_result(result)
            return result

    def _compute_score(
        self,
        fps: float,
        baseline_fps: float,
        silence_ratio: float,
        clipping_ratio: float,
        success_rate: float,
    ) -> float:
        """
        extraction_score = (fps / baseline_fps) × quality_multiplier × reliability_multiplier

        quality_multiplier   = clamp(1 - silence_ratio×2 - clipping_ratio×3, 0, 1)
        reliability_multiplier = success_rate²
        """
        if baseline_fps <= 0:
            throughput_score = fps  # Absolute waarde als geen baseline
        else:
            throughput_score = fps / baseline_fps

        quality_mult = max(0.0, min(1.0, 1.0 - silence_ratio * 2.0 - clipping_ratio * 3.0))
        reliability_mult = success_rate ** 2

        return throughput_score * quality_mult * reliability_mult

    def _measure_network_baseline(self, urls: List[str]) -> Dict[str, float]:
        """Meet HEAD-request latency per URL voor netwerk-normalisatie."""
        times = {}
        for url in urls:
            try:
                start = time.time()
                import requests as req
                req.head(url, timeout=5)
                times[url] = time.time() - start
            except Exception:
                times[url] = 0.0
        return times

    def _load_baseline_fps(self) -> float:
        if BASELINE_JSON.exists():
            try:
                with open(BASELINE_JSON) as f:
                    data = json.load(f)
                return float(data.get("fragments_per_second", 0))
            except Exception:
                pass
        return 0.0

    def _progress_callback(self, current: int, total: int, msg: str) -> None:
        print(f"  [{current}/{total}] {msg}", end="\r")

    def _print_result(self, r: ExperimentResult) -> None:
        status = "✓" if r.extraction_score > 1.0 else "○"
        print(f"\n{status} Score:     {r.extraction_score:.4f}")
        print(f"  fps:       {r.fragments_per_second:.3f} (baseline: {r.baseline_fps:.3f})")
        print(f"  success:   {r.success_count}/{r.success_count + r.failure_count} ({r.success_rate*100:.1f}%)")
        print(f"  silence:   {r.silence_ratio:.3f}  clipping: {r.clipping_ratio:.3f}")
        print(f"  duur:      {r.total_time_sec:.1f}s  netwerk-penalty: {r.network_penalty_applied}")

    def _error_result(self, exp_id, cfg_hash, git_commit, timestamp,
                      fixture_path, cfg, error_msg) -> ExperimentResult:
        return ExperimentResult(
            experiment_id=exp_id,
            config_hash=cfg_hash,
            git_commit=git_commit,
            timestamp=timestamp,
            extraction_score=0.0,
            total_time_sec=0.0,
            fragments_per_second=0.0,
            baseline_fps=0.0,
            median_group_time_sec=0.0,
            p95_group_time_sec=0.0,
            mean_duration_error_sec=0.0,
            silence_ratio=0.0,
            clipping_ratio=0.0,
            success_count=0,
            failure_count=0,
            success_rate=0.0,
            mean_http_time_sec=0.0,
            total_bytes_downloaded=0,
            network_penalty_applied=False,
            peak_memory_mb=0.0,
            fixture_path=fixture_path,
            config_summary=_config_summary(cfg) if cfg else "",
            error=error_msg,
        )


def save_result(result: ExperimentResult) -> Path:
    """Sla experiment-resultaat op als JSON."""
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPERIMENTS_DIR / f"{result.experiment_id}.json"
    with open(path, "w") as f:
        json.dump(asdict(result), f, indent=2)
    return path


def save_as_baseline(result: ExperimentResult) -> None:
    """Sla resultaat op als baseline."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_JSON, "w") as f:
        json.dump(asdict(result), f, indent=2)
    with open(BASELINE_PATH, "w") as f:
        f.write(f"{result.extraction_score:.6f}\n")
    print(f"\n✓ Baseline opgeslagen: score={result.extraction_score:.4f} fps={result.fragments_per_second:.3f}")


def load_baseline_score() -> float:
    if BASELINE_PATH.exists():
        try:
            return float(BASELINE_PATH.read_text().strip())
        except Exception:
            pass
    return 0.0


def load_all_results() -> List[ExperimentResult]:
    """Laad alle experiment-resultaten gesorteerd op timestamp."""
    results = []
    if EXPERIMENTS_DIR.exists():
        for path in sorted(EXPERIMENTS_DIR.glob("*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
                results.append(ExperimentResult(**data))
            except Exception:
                pass
    return results


def main():
    parser = argparse.ArgumentParser(description="Draai één benchmark-experiment")
    parser.add_argument("--save-baseline", action="store_true",
                        help="Sla resultaat op als nieuwe baseline")
    parser.add_argument("--fixture", choices=["primary", "holdout"], default="primary",
                        help="Welke fixture-set te gebruiken (default: primary)")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET_SEC,
                        help=f"Tijdbudget in seconden (default: {DEFAULT_BUDGET_SEC})")
    parser.add_argument("--experiment-id", type=str, default=None,
                        help="Overschrijf automatisch gegenereerde experiment-ID")
    args = parser.parse_args()

    fixture_path = DATASET_PATH if args.fixture == "primary" else HOLDOUT_PATH
    fixture_data = load_fixture(fixture_path)
    print(f"Fixture: {fixture_path} ({len(fixture_data)} segmenten)")

    evaluator = TimeBudgetedEvaluator(budget_sec=args.budget)
    result = evaluator.run(
        fixture_data,
        fixture_path=str(fixture_path),
        experiment_id=args.experiment_id,
    )

    result_path = save_result(result)
    print(f"\nResultaat: {result_path}")

    if args.save_baseline:
        save_as_baseline(result)

    return result


if __name__ == "__main__":
    main()
