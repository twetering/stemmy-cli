"""
benchmark/autoresearch.py — VASTE agent orchestrator.

Implementeert de Karpathy-autoresearch-lus voor extractie-optimalisatie:
  1. Laad baseline + experiment history
  2. Vraag Claude om een nieuwe extractor_config.py voor te stellen
  3. Syntaxcheck → schrijf naar extractor_config.py
  4. Draai evaluate.py → ExperimentResult
  5. PeerValidator: 3 runs → commit bij PASS, anders reset
  6. Herhaal tot max_experiments bereikt

Gebruik:
    python benchmark/autoresearch.py                         # 50 experimenten
    python benchmark/autoresearch.py --max-experiments 100  # Nacht-run
    python benchmark/autoresearch.py --dry-run              # 1 experiment, geen commit
    python benchmark/autoresearch.py --parallel-agents 4   # Multi-agent modus
    python benchmark/autoresearch.py --fixture holdout      # Gebruik holdout set
"""

import argparse
import ast
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from evaluate import (
    DEFAULT_BUDGET_SEC,
    BASELINE_PATH,
    TimeBudgetedEvaluator,
    ExperimentResult,
    load_all_results,
    load_baseline_score,
    save_as_baseline,
    save_result,
)
from prepare import DATASET_PATH, HOLDOUT_PATH, load_fixture

CONFIG_PATH = Path(__file__).parent / "extractor_config.py"
PROGRAM_PATH = Path(__file__).parent / "program.md"

IMPROVEMENT_THRESHOLD = 0.01   # Minimaal 1% beter dan baseline om te committen
PEER_VALIDATION_RUNS = 3        # Aantal runs voor peer-validatie
HOLDOUT_THRESHOLD = 0.85        # Holdout-score moet >= 85% van primaire score zijn
HIGH_RISK_THRESHOLD = 0.03      # Bij hoog risico: 3% verbetering vereist


# ---------------------------------------------------------------------------
# LLM-aanroepen via Anthropic API
# ---------------------------------------------------------------------------

def _call_anthropic(system_prompt: str, user_prompt: str, model: str = "claude-opus-4-6") -> str:
    """Roep de Anthropic API aan. Geeft de content van het eerste tekst-blok terug."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY niet gevonden in omgeving.")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return message.content[0].text
    except ImportError:
        # Fallback: directe HTTP-aanroep
        import urllib.request
        import urllib.error
        payload = json.dumps({
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"]


# ---------------------------------------------------------------------------
# Peer Validator
# ---------------------------------------------------------------------------

class PeerValidator:
    def __init__(self, evaluator: TimeBudgetedEvaluator, n_runs: int = PEER_VALIDATION_RUNS):
        self.evaluator = evaluator
        self.n_runs = n_runs

    def verify(
        self,
        fixture_data: List[dict],
        baseline_score: float,
        candidate_score: float,
        threshold: float = IMPROVEMENT_THRESHOLD,
    ) -> Tuple[bool, float, float]:
        """
        Draai n_runs evaluaties met huidige config.
        Returns: (accepted, median_score, score_stdev)
        """
        print(f"\n  PeerValidator: {self.n_runs} verificatie-runs...")
        scores = []
        for i in range(self.n_runs):
            result = self.evaluator.run(fixture_data, experiment_id=f"peer-{i+1}")
            scores.append(result.extraction_score)
            print(f"    Run {i+1}: score={result.extraction_score:.4f}")

        median = statistics.median(scores)
        stdev = statistics.stdev(scores) if len(scores) > 1 else 0.0
        required = baseline_score * (1 + threshold)
        accepted = median >= required

        print(f"  Mediaan: {median:.4f} | Stdev: {stdev:.4f} | Vereist: {required:.4f}")
        print(f"  Uitslag: {'✓ PASS' if accepted else '✗ FAIL'}")
        return accepted, median, stdev

    def adversarial_review(self, config_content: str, result: ExperimentResult) -> Tuple[str, List[str]]:
        """
        Vraag een aparte Claude-instantie om edge cases en risico's te identificeren.
        Returns: (risk_level: "low"|"medium"|"high", concerns: List[str])
        """
        system = (
            "Je bent een kritische software-ingenieur die een audio-extractie-configuratie "
            "beoordeelt op correctheid, robuustheid en edge cases. "
            "Geef een JSON-object terug met: "
            '{"risk_level": "low"|"medium"|"high", "concerns": ["concern 1", ...]}'
        )
        user = (
            f"Beoordeel deze extractie-configuratie:\n\n```python\n{config_content}\n```\n\n"
            f"Resultaten: score={result.extraction_score:.4f}, "
            f"fps={result.fragments_per_second:.3f}, "
            f"silence={result.silence_ratio:.3f}, "
            f"clipping={result.clipping_ratio:.3f}, "
            f"success_rate={result.success_rate:.3f}\n\n"
            "Kijk specifiek naar:\n"
            "- buffer_before < 1.0: risico op clipping bij segment-begin\n"
            "- max_workers > 12: S3 rate-limit risico\n"
            "- ffmpeg_codec=copy zonder fade: audio-artefacten mogelijk\n"
            "- max_group_size > 10: geheugenrisico bij grote afleveringen\n"
            "Geef alleen JSON terug, geen uitleg."
        )
        try:
            response = _call_anthropic(system, user, model="claude-sonnet-4-6")
            # Extract JSON uit response
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get("risk_level", "low"), data.get("concerns", [])
        except Exception as e:
            print(f"  Peer review fout: {e}")
        return "low", []


# ---------------------------------------------------------------------------
# Cross-validatie op holdout
# ---------------------------------------------------------------------------

def validate_on_holdout(evaluator: TimeBudgetedEvaluator, primary_score: float) -> Optional[float]:
    """Draai één evaluatie op de holdout-set. Geeft None als geen holdout beschikbaar."""
    if not HOLDOUT_PATH.exists():
        return None
    try:
        holdout_data = load_fixture(HOLDOUT_PATH)
        result = evaluator.run(holdout_data, fixture_path=str(HOLDOUT_PATH),
                               experiment_id="holdout-check")
        return result.extraction_score
    except Exception as e:
        print(f"  Holdout-validatie fout: {e}")
        return None


# ---------------------------------------------------------------------------
# Prompt-bouw
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    return """Je bent een expert Python-performance-ingenieur die een podcast audio-extractie pipeline optimaliseert.

De pipeline downloadt audiochunks van S3 via HTTP Range requests, extraheert segmenten met FFmpeg,
en verwerkt ze parallel met ThreadPoolExecutor.

DOEL: Maximaliseer extraction_score = (fps / baseline_fps) × quality_multiplier × reliability_multiplier
waarbij quality_multiplier = clamp(1 - silence_ratio×2 - clipping_ratio×3, 0, 1)
en reliability_multiplier = success_rate²

HARDE GRENZEN (worden automatisch gecontroleerd):
- max_workers: 1–16
- buffer_before: 0.5–5.0 (onder 0.5 risico op clipping!)
- buffer_after: 0.0–3.0
- http_timeout_read: 10.0–300.0
- max_group_size: 1–20
- ffmpeg_codec: "libmp3lame" of "copy"
- executor_strategy: "thread" of "process"

REGELS:
- Wijzig ALLEEN de waarden in ExtractorConfig (na het = teken)
- Verander NIET de imports, dataclass-definitie of CONFIG = ExtractorConfig()
- Output ALLEEN geldige Python, geen markdown code-fences, geen uitleg
- Het bestand moet syntaxgeldig zijn (ast.parse controle)
"""


def _build_user_prompt(history: List[ExperimentResult], current_config: str) -> str:
    baseline = load_baseline_score()
    best = max((r.extraction_score for r in history), default=0.0)

    # Experiment history
    history_lines = []
    for r in history[-10:][::-1]:  # Laatste 10, nieuwste eerst
        committed = " | COMMITTED" if r.extraction_score > baseline else ""
        history_lines.append(
            f"  {r.experiment_id} | score={r.extraction_score:.4f} | "
            f"fps={r.fragments_per_second:.3f} | "
            f"silence={r.silence_ratio:.3f} | "
            f"{r.config_summary}{committed}"
        )

    history_text = "\n".join(history_lines) if history_lines else "  (geen experimenten nog)"

    # Program.md focus
    focus = _read_current_focus()

    # Parallel extractor kern (read-only context)
    extractor_context = _read_extractor_context()

    return f"""EXPERIMENT HISTORY (nieuwste eerst):
{history_text}

BASELINE: {baseline:.4f}
HUIDIG BESTE: {best:.4f}
EXPERIMENT #{len(history) + 1}

HUIDIGE extractor_config.py:
```python
{current_config}
```

PARALLEL EXTRACTOR (read-only, niet wijzigen):
```python
{extractor_context}
```

ONDERZOEKSRICHTING (program.md):
{focus}

Genereer een nieuwe extractor_config.py die een hogere extraction_score oplevert.
Baseer je op de experiment history: welke patronen werken? Wat nog niet geprobeerd?
Output ALLEEN de volledige Python-bestandsinhoud, geen uitleg.
"""


def _read_current_focus() -> str:
    if not PROGRAM_PATH.exists():
        return "Optimaliseer max_workers en grouping parameters."
    content = PROGRAM_PATH.read_text()
    # Zoek de sectie met (CURRENT FOCUS)
    match = re.search(r'### Direction \d+.*?\(CURRENT FOCUS\)(.*?)(?=### Direction|\Z)',
                      content, re.DOTALL)
    if match:
        return match.group(0).strip()[:800]
    return content[:800]


def _read_extractor_context() -> str:
    """Geef de klasse-constanten en methodesignaturen van ParallelExtractor."""
    extractor_path = REPO_ROOT / "src" / "stemmy_cli" / "audio" / "parallel_extractor.py"
    if not extractor_path.exists():
        return "# ParallelExtractor niet gevonden"
    content = extractor_path.read_text()
    # Geef alleen de klasse-definitie t/m _create_groups (eerste ~80 regels)
    lines = content.split("\n")
    return "\n".join(lines[:80])


def _advance_focus_in_program_md(stalled_direction: str) -> None:
    """Verplaats (CURRENT FOCUS) naar de volgende Direction als een richting uitgeput is."""
    if not PROGRAM_PATH.exists():
        return
    content = PROGRAM_PATH.read_text()
    # Verwijder (CURRENT FOCUS) van huidige
    content = content.replace("(CURRENT FOCUS)", "(EXHAUSTED)")
    # Voeg toe aan volgende Direction zonder tag
    match = re.search(r'(### Direction \d+[^\n]*)\n', content)
    if match:
        old = match.group(0)
        new = old.rstrip("\n") + " (CURRENT FOCUS)\n"
        content = content.replace(old, new, 1)
    PROGRAM_PATH.write_text(content)


# ---------------------------------------------------------------------------
# Git-beheer
# ---------------------------------------------------------------------------

def _git_commit_improvement(result: ExperimentResult) -> None:
    baseline = load_baseline_score()
    pct = (result.extraction_score - baseline) / max(baseline, 0.001) * 100
    msg = (
        f"benchmark: score {result.extraction_score:.4f} "
        f"(+{pct:.1f}% vs baseline) "
        f"fps={result.fragments_per_second:.3f} "
        f"exp={result.experiment_id}"
    )
    subprocess.run(["git", "add",
                    "benchmark/extractor_config.py",
                    "benchmark/BASELINE_SCORE"],
                   cwd=REPO_ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=REPO_ROOT, check=True
    )
    print(f"  ✓ Git commit: {msg[:70]}")


def _git_reset_config() -> None:
    subprocess.run(
        ["git", "checkout", "HEAD", "--", "benchmark/extractor_config.py"],
        cwd=REPO_ROOT, check=True
    )


# ---------------------------------------------------------------------------
# Syntaxcheck
# ---------------------------------------------------------------------------

def _syntax_ok(content: str) -> bool:
    try:
        ast.parse(content)
        return True
    except SyntaxError as e:
        print(f"  ✗ Syntaxfout in gegenereerde config: {e}")
        return False


def _has_required_structure(content: str) -> bool:
    """Controleer of de essentiële structuur aanwezig is."""
    required = [
        "class ExtractorConfig",
        "CONFIG = ExtractorConfig()",
        "from dataclasses import",
    ]
    return all(r in content for r in required)


# ---------------------------------------------------------------------------
# Hoofdlus
# ---------------------------------------------------------------------------

class AutoresearchLoop:
    def __init__(
        self,
        max_experiments: int = 50,
        budget_sec: int = DEFAULT_BUDGET_SEC,
        fixture_path: Path = DATASET_PATH,
        model: str = "claude-opus-4-6",
        dry_run: bool = False,
        improvement_threshold: float = IMPROVEMENT_THRESHOLD,
        no_peer_validation: bool = False,
        agent_id: str = "agent-0",
    ):
        self.max_experiments = max_experiments
        self.budget_sec = budget_sec
        self.fixture_path = fixture_path
        self.model = model
        self.dry_run = dry_run
        self.improvement_threshold = improvement_threshold
        self.no_peer_validation = no_peer_validation
        self.agent_id = agent_id
        self.evaluator = TimeBudgetedEvaluator(budget_sec=budget_sec)
        self.validator = PeerValidator(self.evaluator)

        # Fixture laden
        self.fixture_data = load_fixture(fixture_path)
        print(f"\n{'='*60}")
        print(f"STEMMY AUTORESEARCH — {agent_id}")
        print(f"Model: {model} | Budget: {budget_sec}s/exp | Max: {max_experiments} exp")
        print(f"Fixture: {fixture_path.name} ({len(self.fixture_data)} segmenten)")
        print(f"Baseline: {load_baseline_score():.4f}")
        print(f"{'='*60}\n")

    def run(self) -> None:
        for exp_num in range(1, self.max_experiments + 1):
            print(f"\n{'─'*60}")
            print(f"Experiment {exp_num}/{self.max_experiments} | Agent: {self.agent_id}")
            print(f"{'─'*60}")

            history = load_all_results()

            # Genereer nieuwe config via LLM
            new_config = self._propose_config(history)
            if new_config is None:
                print("  LLM kon geen geldige config genereren. Overslaan.")
                time.sleep(5)
                continue

            # Schrijf config
            CONFIG_PATH.write_text(new_config)

            # Draai experiment
            result = self.evaluator.run(
                self.fixture_data,
                fixture_path=str(self.fixture_path),
            )
            save_result(result)

            baseline = load_baseline_score()
            required = baseline * (1 + self.improvement_threshold)
            improved = result.extraction_score >= required

            if not improved:
                print(f"\n  Geen verbetering ({result.extraction_score:.4f} < {required:.4f})")
                if not self.dry_run:
                    _git_reset_config()
                self._maybe_advance_focus(history, exp_num)
                continue

            print(f"\n  Kandidaat-verbetering: {result.extraction_score:.4f} vs baseline {baseline:.4f}")

            # Peer-validatie
            if not self.no_peer_validation and not self.dry_run:
                # Adversariële review
                risk_level, concerns = self.validator.adversarial_review(new_config, result)
                threshold = IMPROVEMENT_THRESHOLD
                if risk_level == "high":
                    threshold = HIGH_RISK_THRESHOLD
                    print(f"  ⚠ Hoog risico gedetecteerd: {concerns[:2]}")

                # 3-run verificatie
                accepted, median_score, stdev = self.validator.verify(
                    self.fixture_data, baseline, result.extraction_score, threshold
                )
                if not accepted:
                    print("  Peer-validatie FAILED. Reset.")
                    _git_reset_config()
                    continue

                # Holdout cross-validatie
                holdout_score = validate_on_holdout(self.evaluator, median_score)
                if holdout_score is not None:
                    if holdout_score < median_score * HOLDOUT_THRESHOLD:
                        print(f"  ⚠ Holdout-regressie: {holdout_score:.4f} < {median_score*HOLDOUT_THRESHOLD:.4f}")
                        print("  Mogelijk overfit op primaire fixture. Toch committen (waarschuwing).")

                final_score = median_score
            else:
                final_score = result.extraction_score

            # Sla op als nieuwe baseline en commit
            if not self.dry_run:
                result_for_baseline = ExperimentResult(**{
                    **asdict(result),
                    "extraction_score": final_score,
                })
                save_as_baseline(result_for_baseline)
                _git_commit_improvement(result_for_baseline)
                print(f"  ✓ Nieuwe baseline: {final_score:.4f}")
            else:
                print(f"  [DRY RUN] Zou committen: score={final_score:.4f}")
                _git_reset_config()

        print(f"\n{'='*60}")
        print(f"AUTORESEARCH KLAAR: {self.max_experiments} experimenten uitgevoerd")
        print(f"Finale baseline: {load_baseline_score():.4f}")
        print(f"{'='*60}")

    def _propose_config(self, history: List[ExperimentResult]) -> Optional[str]:
        """Vraag Claude om een nieuwe config voor te stellen."""
        current_config = CONFIG_PATH.read_text()
        system = _build_system_prompt()
        user = _build_user_prompt(history, current_config)

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                print(f"  LLM ({self.model}) aanroepen... (poging {attempt+1})")
                response = _call_anthropic(system, user, model=self.model)

                # Strip mogelijke markdown code-fences
                response = re.sub(r'^```python\s*', '', response.strip())
                response = re.sub(r'\s*```$', '', response.strip())
                response = response.strip()

                if not _syntax_ok(response):
                    continue
                if not _has_required_structure(response):
                    print("  ✗ Vereiste structuur ontbreekt in gegenereerde config")
                    continue

                return response

            except Exception as e:
                print(f"  LLM-fout (poging {attempt+1}): {e}")
                time.sleep(10)

        return None

    def _maybe_advance_focus(self, history: List[ExperimentResult], exp_num: int) -> None:
        """Verschuif onderzoeksrichting als huidige 10+ experiments zonder verbetering heeft."""
        if exp_num % 10 == 0:
            print("  10 experimenten zonder verbetering — focus verschuiven")
            baseline = load_baseline_score()
            recent = [r for r in history[-10:] if r.extraction_score < baseline * 1.01]
            if len(recent) >= 8:
                _advance_focus_in_program_md("stalled")


# ---------------------------------------------------------------------------
# Multi-agent modus
# ---------------------------------------------------------------------------

def launch_parallel_agents(n_agents: int, base_args: list) -> None:
    """Start N parallelle agents in aparte git worktrees."""
    worktree_base = REPO_ROOT / "benchmark" / ".worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)
    processes = []

    print(f"\nMulti-agent modus: {n_agents} agents starten...")

    for i in range(n_agents):
        wt_path = worktree_base / f"agent-{i}"
        if wt_path.exists():
            shutil.rmtree(wt_path)

        # Maak git worktree
        try:
            subprocess.run(
                ["git", "worktree", "add", str(wt_path), "HEAD"],
                cwd=REPO_ROOT, check=True, capture_output=True
            )
        except subprocess.CalledProcessError as e:
            print(f"  Worktree agent-{i} fout: {e.stderr.decode()}")
            continue

        # Start agent in worktree
        cmd = [
            sys.executable, str(wt_path / "benchmark" / "autoresearch.py"),
            "--agent-id", f"agent-{i}",
        ] + base_args

        proc = subprocess.Popen(cmd, cwd=wt_path)
        processes.append((i, proc, wt_path))
        print(f"  Agent {i} gestart (PID: {proc.pid})")

    # Wacht op alle agents
    print("\nWachten op agents...")
    for agent_id, proc, wt_path in processes:
        proc.wait()
        print(f"  Agent {agent_id} klaar (exitcode: {proc.returncode})")

    # Merge beste config naar main
    _merge_best_from_worktrees([wt_path for _, _, wt_path in processes])

    # Opruimen
    for _, _, wt_path in processes:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)],
                       cwd=REPO_ROOT, capture_output=True)


def _merge_best_from_worktrees(worktree_paths: list) -> None:
    """Kopieer de beste config van alle worktrees naar de hoofd-repo."""
    baseline = load_baseline_score()
    best_score = baseline
    best_config = None

    for wt_path in worktree_paths:
        wt_experiments = wt_path / "benchmark" / "results" / "experiments"
        if not wt_experiments.exists():
            continue
        for exp_file in sorted(wt_experiments.glob("*.json")):
            try:
                with open(exp_file) as f:
                    data = json.load(f)
                score = data.get("extraction_score", 0)
                if score > best_score:
                    best_score = score
                    best_config = wt_path / "benchmark" / "extractor_config.py"
            except Exception:
                pass

    if best_config and best_config.exists():
        shutil.copy(best_config, CONFIG_PATH)
        print(f"\n✓ Beste config gekopieerd van {best_config.parent.parent.name}: score={best_score:.4f}")
    else:
        print("\nGeen verbetering gevonden in worktrees.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Stemmy Autoresearch Lus")
    parser.add_argument("--max-experiments", type=int, default=50)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET_SEC,
                        help="Tijdbudget per experiment (seconden)")
    parser.add_argument("--model", default="claude-opus-4-6",
                        help="Anthropic model (default: claude-opus-4-6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Draai 1 experiment, geen git commit")
    parser.add_argument("--fixture", choices=["primary", "holdout"], default="primary")
    parser.add_argument("--parallel-agents", type=int, default=1,
                        help="Aantal parallelle agents (default: 1)")
    parser.add_argument("--agent-id", default="agent-0",
                        help="Interne agent-ID voor logging")
    parser.add_argument("--no-peer-validation", action="store_true",
                        help="Sla peer-validatie over (sneller, minder nauwkeurig)")
    args = parser.parse_args()

    if args.dry_run:
        args.max_experiments = 1

    fixture_path = DATASET_PATH if args.fixture == "primary" else HOLDOUT_PATH

    if args.parallel_agents > 1 and args.agent_id == "agent-0":
        # Coördinator: start sub-agents
        forwarded = [
            "--max-experiments", str(args.max_experiments),
            "--budget", str(args.budget),
            "--model", args.model,
            "--fixture", args.fixture,
        ]
        if args.no_peer_validation:
            forwarded.append("--no-peer-validation")
        launch_parallel_agents(args.parallel_agents, forwarded)
    else:
        # Single-agent modus
        loop = AutoresearchLoop(
            max_experiments=args.max_experiments,
            budget_sec=args.budget,
            fixture_path=fixture_path,
            model=args.model,
            dry_run=args.dry_run,
            improvement_threshold=IMPROVEMENT_THRESHOLD,
            no_peer_validation=args.no_peer_validation,
            agent_id=args.agent_id,
        )
        loop.run()


if __name__ == "__main__":
    main()
