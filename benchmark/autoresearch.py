"""
benchmark/autoresearch.py — VASTE agent orchestrator.

Implementeert de Karpathy-autoresearch-lus voor extractie-optimalisatie:
  1. Laad baseline + experiment history + research_log.md
  2. Vraag LLM om een VOLLEDIGE NIEUWE extractor_impl.py te schrijven
     (architectuuronderzoek, geen hyperparameter-tuning)
  3. Syntaxcheck + interface-validatie
  4. Schrijf naar extractor_impl.py → evaluate.py → ExperimentResult
  5. Schrijf conclusie naar research_log.md
  6. PeerValidator: 3 runs → git commit bij PASS, anders reset
  7. Herhaal tot max_experiments bereikt

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

# Laad .env vanuit projectroot — zelfde patroon als orchestrator.py
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_file = REPO_ROOT / ".env"
    if _env_file.exists():
        _load_dotenv(_env_file, override=True)
except ImportError:
    pass

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

IMPL_PATH         = Path(__file__).parent / "extractor_impl.py"
RESEARCH_LOG_PATH = Path(__file__).parent / "research_log.md"
PROGRAM_PATH      = Path(__file__).parent / "program.md"
# Behoud CONFIG_PATH voor backward-compat (niet meer actief gebruikt)
CONFIG_PATH = Path(__file__).parent / "extractor_config.py"

IMPROVEMENT_THRESHOLD = 0.01   # Minimaal 1% beter dan baseline om te committen
PEER_VALIDATION_RUNS = 3        # Aantal runs voor peer-validatie
HOLDOUT_THRESHOLD = 0.85        # Holdout-score moet >= 85% van primaire score zijn
HIGH_RISK_THRESHOLD = 0.03      # Bij hoog risico: 3% verbetering vereist


# ---------------------------------------------------------------------------
# LLM-aanroepen: Anthropic, OpenAI, Google
# ---------------------------------------------------------------------------

# Model-aliases per provider
# "auto"  → snel/goedkoop (adversariële review, quick checks)
# "smart" → architectuurresearch (AANBEVOLEN voor nachtrun)
PROVIDER_DEFAULTS = {
    "anthropic": "claude-haiku-4-5-20251001",   # Snel + goedkoop (alleen voor review/checks)
    "openai":    "gpt-4o-mini",                  # Snel + goedkoop
    "google":    "gemini-2.5-flash",             # Snel + goedkoop (2.0-flash deprecated)
}
PROVIDER_SMART = {
    "anthropic": "claude-sonnet-4-6",      # Aanbevolen voor research: ~12× duurder dan haiku,
                                           # maar 100 runs ≈ €4-5. Betere architectuurcode.
    "openai":    "gpt-4o",
    "google":    "gemini-3.1-pro-preview", # Meest capabele Gemini (2026-03)
                                           # Alternatief: gemini-3-flash-preview (sneller)
}


def _detect_provider() -> str:
    """Auto-detecteer beschikbare provider op basis van env-variabelen."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    elif os.environ.get("OPENAI_API_KEY"):
        return "openai"
    elif os.environ.get("GOOGLE_API_KEY"):
        return "google"
    raise RuntimeError(
        "Geen API-key gevonden. Stel ANTHROPIC_API_KEY, OPENAI_API_KEY of GOOGLE_API_KEY in."
    )


def _parse_model_spec(model_spec: str) -> tuple[str, str]:
    """
    Parseer 'provider:model-name' of 'model-name'.
    Voorbeelden:
      'anthropic:claude-haiku-3-5' → ('anthropic', 'claude-haiku-3-5')
      'openai:gpt-4o-mini'         → ('openai', 'gpt-4o-mini')
      'auto'                       → auto-detecteer provider + goedkoop model
      'smart'                      → auto-detecteer provider + slim model
    """
    if model_spec in ("auto", "cheap"):
        provider = _detect_provider()
        return provider, PROVIDER_DEFAULTS[provider]
    if model_spec == "smart":
        provider = _detect_provider()
        return provider, PROVIDER_SMART[provider]
    if ":" in model_spec:
        provider, _, model_name = model_spec.partition(":")
        return provider.lower(), model_name
    # Geen provider-prefix: raad provider uit model-naam
    m = model_spec.lower()
    if "claude" in m:
        return "anthropic", model_spec
    elif "gpt" in m or "o1" in m or "o3" in m or "o4" in m:
        return "openai", model_spec
    elif "gemini" in m:
        return "google", model_spec
    # Fallback: gebruik beschikbare provider
    return _detect_provider(), model_spec



def _call_llm(system_prompt: str, user_prompt: str, model_spec: str = "auto") -> str:
    """
    Uniforme LLM-aanroep. model_spec = 'provider:model' of 'auto'/'smart'.
    Geen fallback: bij een API-fout wordt een RuntimeError gegooid en stopt autoresearch.
    """
    provider, model_name = _parse_model_spec(model_spec)

    print(f"    [{provider}:{model_name}]", end=" ", flush=True)

    if provider == "anthropic":
        result = _call_anthropic(system_prompt, user_prompt, model_name)
    elif provider == "openai":
        result = _call_openai(system_prompt, user_prompt, model_name)
    elif provider == "google":
        result = _call_google(system_prompt, user_prompt, model_name)
    else:
        raise RuntimeError(f"Onbekende provider: {provider}")
    return result


def _call_anthropic(system_prompt: str, user_prompt: str, model: str) -> str:
    """Roep de Anthropic Messages API aan."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY niet gevonden in omgeving.")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return message.content[0].text
    except ImportError:
        import urllib.request
        payload = json.dumps({
            "model": model,
            "max_tokens": 8192,
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
            return json.loads(resp.read())["content"][0]["text"]


def _call_openai(system_prompt: str, user_prompt: str, model: str) -> str:
    """Roep de OpenAI Chat Completions API aan."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY niet gevonden in omgeving.")
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content
    except ImportError:
        import urllib.request
        payload = json.dumps({
            "model": model,
            "max_tokens": 8192,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]


def _call_google(system_prompt: str, user_prompt: str, model: str) -> str:
    """Roep de Google Gemini API aan via google-generativeai of REST."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY niet gevonden in omgeving.")
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        gmodel = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_prompt,
        )
        response = gmodel.generate_content(
            user_prompt,
            generation_config={"max_output_tokens": 2048},
        )
        return response.text
    except ImportError:
        pass

    # REST fallback voor Gemini (geen google-generativeai package)
    import urllib.request
    # System instructie als eerste "user" bericht (Gemini ondersteunt geen aparte system role in REST)
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.7},
    }).encode()
    # Probeer v1beta (ondersteunt nieuwere modellen), fallback naar v1
    for api_version in ("v1beta", "v1"):
        url = (f"https://generativelanguage.googleapis.com/{api_version}"
               f"/models/{model}:generateContent?key={api_key}")
        req = urllib.request.Request(url, data=payload,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            if e.code == 404 and api_version == "v1beta":
                continue  # Probeer v1
            raise


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

    def adversarial_review(self, impl_content: str, result: ExperimentResult) -> Tuple[str, List[str]]:
        """
        Vraag een aparte LLM-instantie om edge cases en risico's te identificeren.
        Returns: (risk_level: "low"|"medium"|"high", concerns: List[str])
        """
        system = (
            "Je bent een kritische software-ingenieur die een audio-extractie-implementatie "
            "beoordeelt op correctheid, robuustheid en edge cases. "
            "Geef een JSON-object terug met: "
            '{"risk_level": "low"|"medium"|"high", "concerns": ["concern 1", ...]}'
        )
        # Stuur alleen de eerste 2000 tekens om tokens te sparen
        impl_preview = impl_content[:2000]
        user = (
            f"Beoordeel deze extractie-implementatie (eerste 2000 tekens):\n\n"
            f"```python\n{impl_preview}\n```\n\n"
            f"Resultaten: score={result.extraction_score:.4f}, "
            f"fps={result.fragments_per_second:.3f}, "
            f"success_rate={result.success_rate:.3f}, "
            f"dur_err={result.mean_duration_error_sec:.3f}s\n\n"
            "Kijk specifiek naar:\n"
            "- Thread-safety bij gedeelde state (resultatenlijsten)\n"
            "- Tempfile leaks (finally-blok aanwezig?)\n"
            "- Race conditions bij parallelle FFmpeg subprocessen\n"
            "- Geheugenrisico bij grote audiochunks in-memory\n"
            "- asyncio.run() in sub-threads (event-loop conflict)\n"
            "Geef alleen JSON terug, geen uitleg."
        )
        try:
            response = _call_llm(system, user, model_spec="auto")
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
    return """\
Je bent een expert software-architect en performance-ingenieur.
Je taak: schrijf de volledige Python-implementatie van `benchmark/extractor_impl.py` opnieuw
om de `extraction_score` te maximaliseren.

Dit is architectuuronderzoek — geen hyperparameter-tuning. Je mag de volledige code herschrijven:
imports, klassen, download-strategie, groeperingsalgoritme, FFmpeg-aanroepen, threading-model.

VASTE INTERFACE (mag NOOIT wijzigen):
  def extract_segments(
      segments: List[Dict[str, Any]],
      output_dir: Path,
      progress_callback=None,
  ) -> Tuple[List[ExtractionResult], List[ExtractionResult]]:

  @dataclass
  class ExtractionResult:
      segment_id: str
      output_path: str
      duration: float
      success: bool
      error: Optional[str] = None

SCORING FORMULE:
  extraction_score = (fps / baseline_fps) × quality_multiplier × reliability_multiplier
  quality_multiplier   = clamp(1 - max(0, mean_duration_error_sec - 0.3) × 3, 0, 1)
  reliability_multiplier = success_rate²
  baseline_fps ≈ 1.08

HARDE GRENZEN (evaluator controleert dit, schendingen = disqualificatie):
  - success_rate ≥ 0.90 (minimaal 54/60 segmenten succesvol)
  - mean_duration_error_sec ≤ 0.50 (timing-afwijking max 500ms)
  - Geen destructieve operaties buiten output_dir
  - Geen wijzigingen aan evaluate.py, prepare.py, parallel_extractor.py

CONTEXT OVER DE FIXTURE:
  - 60 segmenten totaal in 3 lagen:
    * 20 "dense": korte segmenten dicht bij elkaar per URL → test grouping
    * 20 "sparse": segmenten >60s apart per URL → test single-group path
    * 20 "cross-episode": 1 segment uit elk van 20 verschillende URLs → test URL-parallellisme
  - Audio op S3, HTTP Range requests, MP3 met soms VBR (variabele bitrate)
  - De 3 bekende failures in baseline zijn waarschijnlijk VBR byte-range miscalculatie

AANBEVOLEN AANPAK VOOR CODERING:
  - Schrijf een # HYPOTHESE: comment bovenaan het bestand (één zin)
  - Eén architecturale verandering per experiment (voor interpreteerbaarheid)
  - Test mentaal op alle 3 fixture-lagen voor je de code schrijft

OUTPUT: Alleen de volledige Python-bestandsinhoud. Geen markdown code-fences. Geen uitleg.
"""


def _build_user_prompt(history: List[ExperimentResult], current_impl: str) -> str:
    baseline = load_baseline_score()
    best = max((r.extraction_score for r in history), default=0.0)

    # Experiment history (laatste 10, nieuwste eerst)
    history_lines = []
    for r in history[-10:][::-1]:
        committed = " | COMMITTED" if r.extraction_score > baseline else ""
        hypothese = r.config_summary or ""
        history_lines.append(
            f"  {r.experiment_id} | score={r.extraction_score:.4f} | "
            f"fps={r.fragments_per_second:.3f} | sr={r.success_rate:.2f} | "
            f"dur_err={r.mean_duration_error_sec:.3f}s | {hypothese}{committed}"
        )
    history_text = "\n".join(history_lines) if history_lines else "  (geen experimenten nog)"

    # Research log (laatste 10 entries voor context — meer geheugen = minder herhaling)
    research_log = _read_research_log(max_entries=10)

    # Focus uit program.md
    focus = _read_current_focus()

    # Voeg error-context toe als het laatste experiment crashte
    last_error = ""
    if history:
        last = history[-1]
        if last.error and last.success_rate == 0.0:
            last_error = f"\n⚠ LAATSTE EXPERIMENT CRASHTE:\n  Error: {last.error}\n  Vermijd dezelfde fout!\n"

    return f"""RESEARCH LOG (meest recente entries):
{research_log}

EXPERIMENT HISTORY (nieuwste eerst):
{history_text}
{last_error}
BASELINE: {baseline:.4f} | HUIDIG BESTE: {best:.4f} | EXPERIMENT #{len(history) + 1}

HUIDIGE extractor_impl.py (jouw vertrekpunt):
```python
{current_impl}
```

ONDERZOEKSRICHTING (program.md — CURRENT FOCUS):
{focus}

TAAK:
1. Lees de research log — vermijd wat al geprobeerd is, bouw op successen
2. Kies één architecturale hypothese (niet meerdere tegelijk)
3. Schrijf de volledige nieuwe extractor_impl.py implementatie
4. Zet bovenaan: # HYPOTHESE: <jouw hypothese in één zin>
5. Controleer: alle imports bestaan in stdlib + requests + subprocess (geen exotische packages)

Output ALLEEN de volledige Python-bestandsinhoud. Geen markdown. Geen uitleg.
"""


def _read_research_log(max_entries: int = 6) -> str:
    """Lees de laatste N entries uit de research log."""
    if not RESEARCH_LOG_PATH.exists():
        return "  (geen research log gevonden)"
    content = RESEARCH_LOG_PATH.read_text()
    # Splits op ## headers
    entries = re.split(r'\n(?=## )', content)
    # Filter op echte experiment entries (niet de header)
    exp_entries = [e for e in entries if re.match(r'## [^\n]+ \| score:', e)]
    recent = exp_entries[-max_entries:]
    if not recent:
        return "  (nog geen experiment-entries)"
    return "\n\n".join(recent)[-3000:]  # Hard cap om tokens te sparen


def _append_to_research_log(
    experiment_id: str,
    result: ExperimentResult,
    impl_content: str,
    conclusion: str,
) -> None:
    """Voeg een experiment-entry toe aan de research log."""
    if not RESEARCH_LOG_PATH.exists():
        return

    # Extraheer HYPOTHESE comment uit implementatie
    hypothese_match = re.search(r'#\s*HYPOTHESE[:\s]+(.+)', impl_content)
    hypothese = hypothese_match.group(1).strip() if hypothese_match else "(geen hypothese gevonden)"

    # Extraheer verandering (eerste paar BEKENDE BEPERKINGEN of implementatie-samenvatting)
    verandering = _summarize_impl_changes(impl_content)

    baseline = load_baseline_score()
    status = "COMMIT" if result.extraction_score > baseline * (1 + IMPROVEMENT_THRESHOLD) else (
        "PARTIAL" if result.success_rate >= 0.90 else "FAIL"
    )

    entry = f"""
## {experiment_id} | score: {result.extraction_score:.4f} | status: {status}
**Hypothese**: {hypothese}
**Verandering**: {verandering}
**Resultaat**: fps={result.fragments_per_second:.3f} | success_rate={result.success_rate:.3f} | dur_err={result.mean_duration_error_sec:.3f}s | time={result.total_time_sec:.1f}s
**Conclusie**: {conclusion}

---"""

    current = RESEARCH_LOG_PATH.read_text()
    # Voeg toe voor de slotmarker of aan het einde
    if "<!-- Nieuwe entries hieronder toevoegen -->" in current:
        new = current.replace(
            "<!-- Nieuwe entries hieronder toevoegen -->",
            "<!-- Nieuwe entries hieronder toevoegen -->\n" + entry,
        )
    else:
        new = current.rstrip() + "\n" + entry + "\n"
    RESEARCH_LOG_PATH.write_text(new)


def _summarize_impl_changes(impl_content: str) -> str:
    """Detecteer architectuurwijzigingen in de implementatie t.o.v. bekende patronen."""
    hints = []
    if "asyncio" in impl_content or "aiohttp" in impl_content:
        hints.append("async I/O (aiohttp/asyncio)")
    if "ProcessPool" in impl_content:
        hints.append("ProcessPoolExecutor")
    elif "ThreadPool" in impl_content:
        hints.append("ThreadPoolExecutor")
    if "pipe:0" in impl_content or "stdin=subprocess.PIPE" in impl_content:
        hints.append("streaming HTTP→FFmpeg stdin")
    if "ffmpeg" in impl_content and '"-i"' in impl_content and "http" in impl_content.lower():
        hints.append("FFmpeg direct HTTP (geen byte-range)")
    if "all_groups" in impl_content or "globale executor" in impl_content.lower():
        hints.append("globale executor (cross-URL parallelisme)")
    if not hints:
        hints.append("(geen specifieke architectuurwijziging herkend)")
    return ", ".join(hints)


def _read_current_focus() -> str:
    """Lees de CURRENT FOCUS sectie uit program.md."""
    if not PROGRAM_PATH.exists():
        return "Implementeer globale ThreadPoolExecutor voor alle URL-groepen."
    content = PROGRAM_PATH.read_text()
    # Zoek naar Research Question met CURRENT FOCUS
    match = re.search(
        r'### Research Question \d+.*?\(CURRENT FOCUS\)(.*?)(?=###|\Z)',
        content, re.DOTALL
    )
    if match:
        return match.group(0).strip()[:1000]
    # Fallback: geef eerste 800 tekens van program.md
    return content[:800]


def _advance_focus_in_program_md(stalled_direction: str) -> None:
    """Verplaats (CURRENT FOCUS) naar de volgende Research Question als een richting uitgeput is."""
    if not PROGRAM_PATH.exists():
        return
    content = PROGRAM_PATH.read_text()
    # Verwijder (CURRENT FOCUS) van huidige richting
    content = content.replace("(CURRENT FOCUS)", "(EXHAUSTED)")
    # Zoek de eerstvolgende Research Question zonder tag en voeg CURRENT FOCUS toe
    match = re.search(r'(### Research Question \d+[^\n]*(?<!EXHAUSTED))\n', content)
    if match:
        old = match.group(0)
        new = old.rstrip("\n") + " (CURRENT FOCUS)\n"
        content = content.replace(old, new, 1)
        print(f"  ↪ Focus verschoven naar: {match.group(1)[:60]}")
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
    files_to_add = ["benchmark/extractor_impl.py", "benchmark/BASELINE_SCORE"]
    if RESEARCH_LOG_PATH.exists():
        files_to_add.append("benchmark/research_log.md")
    subprocess.run(["git", "add"] + files_to_add, cwd=REPO_ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=REPO_ROOT, check=True
    )
    print(f"  ✓ Git commit: {msg[:70]}")


IMPL_BACKUP_PATH = IMPL_PATH.with_suffix(".py.bak")


def _save_impl_backup() -> None:
    """Sla een backup op van de huidige extractor_impl.py vóór elk experiment."""
    if IMPL_PATH.exists():
        shutil.copy(IMPL_PATH, IMPL_BACKUP_PATH)


def _git_reset_impl() -> None:
    """
    Reset extractor_impl.py naar de versie van vóór het experiment.
    Probeert eerst git checkout; valt terug op de .bak die we zelf bewaren
    (nodig in worktrees waar extractor_impl.py niet in git gecommit staat).
    """
    # Probeer git checkout (werkt alleen als het bestand getrackt is)
    result = subprocess.run(
        ["git", "checkout", "HEAD", "--", "benchmark/extractor_impl.py"],
        cwd=REPO_ROOT, capture_output=True
    )
    if result.returncode == 0:
        return

    # Fallback: herstel uit .bak-bestand (voor worktrees of ongecommitte bestanden)
    if IMPL_BACKUP_PATH.exists():
        shutil.copy(IMPL_BACKUP_PATH, IMPL_PATH)
    else:
        print("  ⚠ Geen backup gevonden voor extractor_impl.py — reset overgeslagen")


# Backward-compat alias
def _git_reset_config() -> None:
    _git_reset_impl()


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
    """Controleer of de essentiële interface aanwezig is in de implementatie."""
    required = [
        "def extract_segments",
        "ExtractionResult",
        "output_dir",
    ]
    missing = [r for r in required if r not in content]
    if missing:
        print(f"  ✗ Vereiste elementen ontbreken: {missing}")
        return False
    return True


# ---------------------------------------------------------------------------
# Hoofdlus
# ---------------------------------------------------------------------------

class AutoresearchLoop:
    def __init__(
        self,
        max_experiments: int = 50,
        budget_sec: int = DEFAULT_BUDGET_SEC,
        fixture_path: Path = DATASET_PATH,
        model: str = "smart",
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

        # Sla startversie van impl op als backup voor reset-functie
        _save_impl_backup()

        # Modelcheck bij opstarten — liever nu falen dan na experiment 1
        self._verify_model_on_startup()

    def _verify_model_on_startup(self) -> None:
        """Stuur een mini-verzoek om te bevestigen dat het model bereikbaar is. Stopt bij fout."""
        print(f"  Modelcheck: {self.model}...", end=" ", flush=True)
        try:
            _call_llm("Zeg alleen: OK", "Ping", model_spec=self.model)
            print("✓")
        except Exception as e:
            print(f"\n✗ MODELCHECK MISLUKT: {e}")
            print(f"  Model '{self.model}' is niet bereikbaar. Fix dit vóór je autoresearch start.")
            print("  Controleer: ANTHROPIC_API_KEY, model-naam, account-tier.")
            sys.exit(1)

    def run(self) -> None:
        for exp_num in range(1, self.max_experiments + 1):
            print(f"\n{'─'*60}")
            print(f"Experiment {exp_num}/{self.max_experiments} | Agent: {self.agent_id}")
            print(f"{'─'*60}")

            history = load_all_results()

            # Sla huidige impl op als backup vóór we overschrijven
            _save_impl_backup()

            # Genereer nieuwe implementatie via LLM
            new_impl = self._propose_implementation(history)
            if new_impl is None:
                print("  LLM kon geen geldige implementatie genereren. Overslaan.")
                time.sleep(5)
                continue

            # Schrijf implementatie
            IMPL_PATH.write_text(new_impl)

            # Import-check: kan de module geladen worden zonder te crashen?
            check = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0,'{REPO_ROOT / 'benchmark'}'); import extractor_impl"],
                capture_output=True, timeout=15,
            )
            if check.returncode != 0:
                err = check.stderr.decode(errors="replace")[-300:]
                print(f"  ✗ IMPORT-FOUT (overgeslagen):\n    {err.strip()}")
                _append_to_research_log(
                    f"SKIP-{int(time.time())}",
                    ExperimentResult(
                        experiment_id="skip", config_hash="", git_commit="", timestamp="",
                        extraction_score=0.0, total_time_sec=0.0, fragments_per_second=0.0,
                        baseline_fps=0.0, median_group_time_sec=0.0, p95_group_time_sec=0.0,
                        mean_duration_error_sec=0.0, silence_ratio=0.0, clipping_ratio=0.0,
                        success_count=0, failure_count=0, success_rate=0.0,
                        mean_http_time_sec=0.0, total_bytes_downloaded=0,
                        network_penalty_applied=False, peak_memory_mb=0.0,
                        error=f"IMPORT-FOUT: {err.strip()[:200]}",
                    ),
                    new_impl,
                    f"CRASH bij import. Python-fout: {err.strip()[:200]}. Fix syntax/imports vóór retry.",
                )
                _git_reset_impl()
                continue

            # Draai experiment
            result = self.evaluator.run(
                self.fixture_data,
                fixture_path=str(self.fixture_path),
            )
            save_result(result)

            baseline = load_baseline_score()
            required = baseline * (1 + self.improvement_threshold)
            improved = result.extraction_score >= required

            # Genereer conclusie voor research log — altijd schrijven, ook bij failures.
            # (env_failure guard verwijderd: import-check boven vangt echte env-fouten af)
            conclusion = self._generate_conclusion(result, improved, baseline)
            _append_to_research_log(result.experiment_id, result, new_impl, conclusion)

            if not improved:
                print(f"\n  Geen verbetering ({result.extraction_score:.4f} < {required:.4f})")
                if not self.dry_run:
                    _git_reset_impl()
                self._maybe_advance_focus(history, exp_num)
                continue

            print(f"\n  Kandidaat-verbetering: {result.extraction_score:.4f} vs baseline {baseline:.4f}")

            # Peer-validatie
            if not self.no_peer_validation and not self.dry_run:
                # Adversariële review
                risk_level, concerns = self.validator.adversarial_review(new_impl, result)
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
                    _git_reset_impl()
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
                _git_reset_impl()

        print(f"\n{'='*60}")
        print(f"AUTORESEARCH KLAAR: {self.max_experiments} experimenten uitgevoerd")
        print(f"Finale baseline: {load_baseline_score():.4f}")
        print(f"{'='*60}")

    def _propose_implementation(self, history: List[ExperimentResult]) -> Optional[str]:
        """Vraag LLM om een nieuwe volledige extractor_impl.py te schrijven."""
        current_impl = IMPL_PATH.read_text() if IMPL_PATH.exists() else ""
        system = _build_system_prompt()
        user = _build_user_prompt(history, current_impl)

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                print(f"  LLM ({self.model}) aanroepen... (poging {attempt+1})")
                response = _call_llm(system, user, model_spec=self.model)

                # Strip mogelijke markdown code-fences
                response = re.sub(r'^```python\s*\n?', '', response.strip())
                response = re.sub(r'\n?```\s*$', '', response.strip())
                response = response.strip()

                if not _syntax_ok(response):
                    continue
                if not _has_required_structure(response):
                    continue

                return response

            except Exception as e:
                print(f"  LLM-fout (poging {attempt+1}): {e}")
                time.sleep(10)

        return None

    def _generate_conclusion(
        self, result: ExperimentResult, improved: bool, baseline: float
    ) -> str:
        """Genereer een korte conclusie voor de research log (geen LLM-aanroep, heuristisch)."""
        lines = []
        timed_out = result.total_time_sec >= self.budget_sec - 2  # ~90s = timeout

        if improved:
            pct = (result.extraction_score - baseline) / max(baseline, 0.001) * 100
            lines.append(f"Verbetering van +{pct:.1f}% tov baseline bevestigd.")
        elif timed_out and result.success_count == 0:
            lines.append(f"TIMEOUT: alle {self.budget_sec}s opgebruikt, 0 segmenten klaar. "
                         f"Implementatie te traag (waarschijnlijk volledige bestanden downloaden of serieel).")
        else:
            lines.append("Geen verbetering tov baseline.")

        if result.success_rate < 0.90:
            lines.append(f"Kritiek: success_rate={result.success_rate:.2f} < 0.90 (disqualificatie).")
        elif result.success_rate == 1.0:
            lines.append("Perfecte success_rate (1.0) — VBR-probleem opgelost?")
        elif result.success_rate < 0.97:
            lines.append(f"Nog {int(round((1-result.success_rate)*60))} failures — VBR-issue persistent.")

        if result.success_count > 0:
            if result.mean_duration_error_sec > 0.3:
                lines.append(f"Timing-afwijking hoog ({result.mean_duration_error_sec:.3f}s) — seek-nauwkeurigheid controleren.")
            elif result.mean_duration_error_sec < 0.05:
                lines.append("Uitstekende timing-nauwkeurigheid.")

        fps_ratio = result.fragments_per_second / max(1.08, 0.001)
        if fps_ratio > 2.0:
            lines.append(f"Sterke snelheidswinst: {fps_ratio:.1f}× baseline fps.")
        elif fps_ratio < 0.8:
            lines.append("Trager dan baseline — overhead analyse nodig.")

        return " ".join(lines) or "Onvoldoende data voor conclusie."

    # Backward-compat alias
    def _propose_config(self, history):
        return self._propose_implementation(history)

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

def _cleanup_stale_worktrees(worktree_base: Path) -> None:
    """
    Ruim achtergebleven worktree-registraties op (bv. na Ctrl+C).
    git worktree prune verwijdert registraties van paden die niet meer bestaan.
    """
    subprocess.run(["git", "worktree", "prune"], cwd=REPO_ROOT, capture_output=True)
    # Verwijder ook achtergebleven mappen die git nog WEL kent (force-remove)
    if worktree_base.exists():
        for wt_dir in worktree_base.iterdir():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=REPO_ROOT, capture_output=True
            )
            if wt_dir.exists():
                shutil.rmtree(wt_dir, ignore_errors=True)
    # Nog een keer prune voor de zekerheid
    subprocess.run(["git", "worktree", "prune"], cwd=REPO_ROOT, capture_output=True)


def launch_parallel_agents(n_agents: int, base_args: list) -> None:
    """Start N parallelle agents in aparte git worktrees."""
    worktree_base = REPO_ROOT / "benchmark" / ".worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)
    processes = []

    print(f"\nMulti-agent modus: {n_agents} agents starten...")

    # Ruim eventuele stale registraties op van eerdere (afgebroken) runs
    print("  Stale worktrees opruimen...")
    _cleanup_stale_worktrees(worktree_base)

    for i in range(n_agents):
        wt_path = worktree_base / f"agent-{i}"

        # Maak git worktree (pad is nu zeker schoon na cleanup)
        try:
            subprocess.run(
                ["git", "worktree", "add", str(wt_path), "HEAD"],
                cwd=REPO_ROOT, check=True, capture_output=True
            )
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode().strip()
            print(f"  Worktree agent-{i} fout: {err}")
            # Laatste redmiddel: force-add
            try:
                subprocess.run(
                    ["git", "worktree", "add", "-f", str(wt_path), "HEAD"],
                    cwd=REPO_ROOT, check=True, capture_output=True
                )
            except subprocess.CalledProcessError:
                print(f"  Agent {i} kon niet gestart worden — overgeslagen.")
                continue

        # Kopieer huidige (uncommitted) benchmark-bestanden naar worktree
        # zodat de agents ALTIJD de nieuwste code draaien, niet de gecommitte versie
        benchmark_src = REPO_ROOT / "benchmark"
        benchmark_dst = wt_path / "benchmark"
        for fname in ["autoresearch.py", "evaluate.py", "extractor_impl.py",
                      "program.md", "research_log.md"]:
            src = benchmark_src / fname
            if src.exists():
                shutil.copy(src, benchmark_dst / fname)

        # Kopieer results (baseline.json, BASELINE_SCORE) — staan niet in git
        results_src = benchmark_src / "results"
        results_dst = benchmark_dst / "results"
        results_dst.mkdir(parents=True, exist_ok=True)
        for fname in ["baseline.json", "BASELINE_SCORE"]:
            # BASELINE_SCORE staat één niveau hoger
            src = benchmark_src / fname
            if src.exists():
                shutil.copy(src, benchmark_dst / fname)
        # baseline.json zit in results/
        baseline_src = results_src / "baseline.json"
        if baseline_src.exists():
            shutil.copy(baseline_src, results_dst / "baseline.json")

        # Kopieer .env naar worktree zodat de API-sleutels beschikbaar zijn
        env_src = REPO_ROOT / ".env"
        if env_src.exists():
            shutil.copy(env_src, wt_path / ".env")

        # Start agent in worktree
        cmd = [
            sys.executable, str(wt_path / "benchmark" / "autoresearch.py"),
            "--agent-id", f"agent-{i}",
        ] + base_args

        proc = subprocess.Popen(cmd, cwd=wt_path)
        processes.append((i, proc, wt_path))
        print(f"  Agent {i} gestart (PID: {proc.pid})")

    if not processes:
        print("  Geen agents gestart. Controleer git worktree status.")
        return

    # Wacht op alle agents
    print("\nWachten op agents...")
    for agent_id, proc, wt_path in processes:
        try:
            proc.wait()
        except KeyboardInterrupt:
            print(f"\n  Ctrl+C ontvangen — agents stoppen...")
            for _, p, _ in processes:
                p.terminate()
            break
        print(f"  Agent {agent_id} klaar (exitcode: {proc.returncode})")

    # Merge beste impl naar main
    _merge_best_from_worktrees([wt_path for _, _, wt_path in processes])

    # Opruimen (ook na Ctrl+C)
    print("  Worktrees opruimen...")
    _cleanup_stale_worktrees(worktree_base)


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
                    best_config = wt_path / "benchmark" / "extractor_impl.py"
            except Exception:
                pass

    if best_config and best_config.exists():
        shutil.copy(best_config, IMPL_PATH)
        print(f"\n✓ Beste implementatie gekopieerd van {best_config.parent.parent.name}: score={best_score:.4f}")
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
    parser.add_argument("--model", default="smart",
                        help=(
                            "LLM model spec. Voorbeelden:\n"
                            "  'smart'  (default)           → sonnet-4.6 / gpt-4o / gemini-3.1-pro-preview\n"
                            "  'auto'                       → haiku / gpt-4o-mini / gemini-2.5-flash\n"
                            "  'anthropic:claude-sonnet-4-6' → specifiek model\n"
                            "  'openai:gpt-4o'\n"
                            "  'google:gemini-3-flash-preview'\n"
                            "(default: smart)"
                        ))
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
