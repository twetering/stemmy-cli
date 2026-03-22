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
from pathlib import Path as _Path

# Laad .env vanuit projectroot (zelfde patroon als orchestrator.py)
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_file = _Path(__file__).resolve().parent.parent / ".env"
    if _env_file.exists():
        _load_dotenv(_env_file, override=True)
except ImportError:
    pass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Zorg dat de stemmy_cli src beschikbaar is
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from prepare import DATASET_PATH, HOLDOUT_PATH, load_fixture


def _find_binary(name: str) -> str:
    """Zoek een binary in PATH + veelgebruikte extra locaties (homebrew, conda)."""
    import shutil, os
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

RESULTS_DIR = Path(__file__).parent / "results"
EXPERIMENTS_DIR = RESULTS_DIR / "experiments"
BASELINE_PATH = Path(__file__).parent / "BASELINE_SCORE"
BASELINE_JSON = RESULTS_DIR / "baseline.json"

DEFAULT_BUDGET_SEC = 150


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


IMPL_PATH = Path(__file__).parent / "extractor_impl.py"


def _import_impl():
    """
    Laad extractor_impl.py als verse module.
    Gooit ImportError als het bestand ontbreekt of niet importeerbaar is.
    """
    spec = importlib.util.spec_from_file_location("extractor_impl", IMPL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _impl_hash() -> str:
    import hashlib
    return hashlib.sha256(IMPL_PATH.read_bytes()).hexdigest()[:12]


def _validate_impl(mod) -> None:
    """
    Minimale contractcheck: module moet extract_segments hebben met juiste signatuur.
    Gooit ConfigViolation bij schending.
    """
    if not hasattr(mod, "extract_segments"):
        raise ConfigViolation("extractor_impl.py mist de functie `extract_segments`")
    import inspect
    sig = inspect.signature(mod.extract_segments)
    params = list(sig.parameters)
    if len(params) < 2 or params[0] != "segments" or params[1] != "output_dir":
        raise ConfigViolation(
            f"extract_segments heeft verkeerde parameters: {params}. "
            f"Verwacht: (segments, output_dir, progress_callback=None)"
        )


def _impl_summary() -> str:
    """Extraheer de HYPOTHESE-regel uit extractor_impl.py voor logging."""
    try:
        for line in IMPL_PATH.read_text().split("\n"):
            if "HYPOTHESE" in line or "hypothese" in line.lower():
                return line.strip().lstrip("#").strip()[:80]
    except Exception:
        pass
    return IMPL_PATH.read_text()[:80].replace("\n", " ")


# ---------------------------------------------------------------------------
# Kwaliteitsmetrics via FFmpeg
# ---------------------------------------------------------------------------

def measure_silence_ratio(output_path: str, boundary_ms: int = 100) -> float:
    """
    Controleer of het begin van het fragment stil is — proxy voor 'te kleine buffer_before'.
    Analyseer alleen de eerste 200ms: als die volledig stil is, is de buffer te klein.
    Geeft 0.0 = geen probleem, 1.0 = volledig stil begin.
    """
    try:
        # Analyseer alleen eerste 200ms
        cmd = [
            _FFMPEG, "-hide_banner",
            "-ss", "0", "-t", "0.2",
            "-i", output_path,
            "-af", "silencedetect=noise=-45dB:d=0.05",
            "-vn", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        stderr = result.stderr.decode()
        # Als het hele begin stil is, zien we geen audio_start maar wel silence_start=0
        has_silence_at_zero = any(
            "silence_start: 0" in line or "silence_start:0" in line
            for line in stderr.split("\n")
        )
        return 0.5 if has_silence_at_zero else 0.0
    except Exception:
        return 0.0


def measure_clipping_ratio(output_path: str) -> float:
    """
    Onbetrouwbaar voor MP3 → altijd 0.0.
    Duration-accuracy is een betere kwaliteitsindicator.
    """
    return 0.0


def measure_actual_duration(output_path: str) -> float:
    """Gebruik ffprobe om werkelijke duur te meten."""
    try:
        cmd = [
            _FFPROBE, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", output_path
        ]
        return float(subprocess.check_output(cmd, timeout=10).decode().strip())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# (build_configured_extractor verwijderd — evaluator spreekt nu rechtstreeks
#  met extractor_impl.extract_segments, zodat de agent volledige vrijheid heeft)
# ---------------------------------------------------------------------------


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
        exp_id     = experiment_id or _make_experiment_id()
        impl_hash  = _impl_hash()
        git_commit = _git_short_hash()
        timestamp  = datetime.now(timezone.utc).isoformat()
        summary    = _impl_summary()

        print(f"\n{'='*60}")
        print(f"Experiment: {exp_id}")
        print(f"Impl:       {summary[:70]}")
        print(f"Budget:     {self.budget_sec}s | Segmenten: {len(fixture_data)}")
        print(f"{'='*60}")

        # Laad en valideer extractor_impl.py
        try:
            impl_mod = _import_impl()
            _validate_impl(impl_mod)
        except (ConfigViolation, Exception) as e:
            print(f"✗ IMPL FOUT: {e}")
            return self._error_result(exp_id, impl_hash, git_commit, timestamp,
                                      fixture_path, summary, str(e))

        # Tijdelijk output-dir
        with tempfile.TemporaryDirectory(prefix="stemmy_bench_") as tmpdir:
            output_dir = Path(tmpdir)

            # Netwerk baseline meten
            unique_urls = list({s["audio_url"] for s in fixture_data})[:3]
            http_times = self._measure_network_baseline(unique_urls)
            mean_http = sum(http_times.values()) / max(len(http_times), 1)
            network_penalty = any(t > mean_http * 2.5 for t in http_times.values())

            # Extractie draaien met tijdlimiet
            self._timed_out = False
            peak_memory_mb = 0.0
            extraction_start = time.time()
            done_event = threading.Event()
            result_holder = [None]

            error_holder: list = [None]

            def run_extraction():
                try:
                    # DIRECTE AANROEP van extractor_impl.extract_segments
                    # Geen subklasse, geen config-dataclass — pure functie-aanroep
                    s, f = impl_mod.extract_segments(
                        fixture_data,
                        output_dir,
                        self._progress_callback,
                    )
                    result_holder[0] = (s, f)
                except Exception as e:
                    traceback.print_exc()
                    result_holder[0] = ([], [])
                    error_holder[0] = f"{type(e).__name__}: {e}"
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
            score = self._compute_score(fps, baseline_fps, mean_dur_error, silence_ratio, success_rate)

            result = ExperimentResult(
                experiment_id=exp_id,
                config_hash=impl_hash,
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
                config_summary=summary,
                error=error_holder[0],
            )

            self._print_result(result)
            return result

    def _compute_score(
        self,
        fps: float,
        baseline_fps: float,
        mean_duration_error: float,
        silence_ratio: float,
        success_rate: float,
    ) -> float:
        """
        extraction_score = throughput_score × quality_multiplier × reliability_multiplier

        throughput_score     = fps / baseline_fps  (1.0 bij baseline-run)
        quality_multiplier   = f(mean_duration_error) — alleen straffen bij grote afwijking
        reliability_multiplier = success_rate²
        """
        # Throughput: 1.0 bij eerste run (zelf de baseline), anders relatief
        if baseline_fps <= 0:
            throughput_score = 1.0
        else:
            throughput_score = fps / baseline_fps

        # Kwaliteit: gebaseerd op duration-accuracy + silence
        # < 0.3s fout = prima, > 1.5s fout = slechte extractie
        if mean_duration_error < 0.3:
            duration_penalty = 0.0
        elif mean_duration_error < 1.5:
            duration_penalty = (mean_duration_error - 0.3) / 1.2 * 0.5
        else:
            duration_penalty = 0.5

        # Silence penalty alleen als begin echt stil is
        silence_penalty = silence_ratio * 0.3

        quality_mult = max(0.0, min(1.0, 1.0 - duration_penalty - silence_penalty))
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
            config_summary=str(cfg) if cfg else "",
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
    """
    Sla resultaat op als baseline.
    De baseline-score is per definitie 1.0 — alle volgende experimenten zijn relatief hieraan.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # Forceer score=1.0 voor de baseline-run (definitie van "normaal")
    baseline_result = ExperimentResult(**{**asdict(result), "extraction_score": 1.0})
    with open(BASELINE_JSON, "w") as f:
        json.dump(asdict(baseline_result), f, indent=2)
    with open(BASELINE_PATH, "w") as f:
        f.write("1.000000\n")
    print(f"\n✓ Baseline opgeslagen: score=1.0000 (definitie) | fps={result.fragments_per_second:.3f} | success={result.success_rate*100:.1f}%")


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
