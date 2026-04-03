# Stemmy Extractie Benchmark — Autoresearch Systeem

Karpathy-stijl autoresearch-lus voor het optimaliseren van podcastfragment-extractie.
Een AI-agent schrijft de volledige implementatie (`extractor_impl.py`) opnieuw,
evalueert het resultaat via een vaste metric, en bewaart verbeteringen via git.

## Resultaten (maart 2026)

| Aanpak | fps (warm S3) | Score | Verbetering |
|---|---|---|---|
| **Globale ThreadPoolExecutor** ← huidig | ~15 | 11.24 | **+1023%** |
| Workers=10 (config-tuning) | ~1.27 | 1.05 | +5% |
| Originele baseline (4 workers, per-URL) | ~1.08 | 1.00 | — |

**Grootste inzicht**: De bottleneck zat niet in FFmpeg of HTTP-download snelheid,
maar in de seriële per-URL ThreadPoolExecutor. Door alle groepen van alle URLs
in één globale pool te verwerken verdwijnt de seriële bottleneck volledig.

De benchmark heeft ook het VBR-timing-probleem opgespoord (3 van 60 segmenten
mislukten met de Megaphone CDN). Opgelost door BUFFER_BEFORE van 3s naar 8s te
verhogen — ruimere preroll absorbeert VBR seek-imprecisie.

## Architectuur

```
benchmark/
├── extractor_impl.py   ← AGENT-EDITEERBAAR ("train.py" van Karpathy)
│                          Volledige Python implementatie, vaste interface
├── evaluate.py         ← VAST: evaluator, tijdsbudget (150s), scoring
├── autoresearch.py     ← VAST: LLM-lus, git-beheer, worktrees
├── prepare.py          ← VAST: fixture builder (eenmalig draaien)
├── dashboard.py        ← VAST: Rich terminal + HTML export
├── program.md          ← Onderzoeksdirectieven (CURRENT FOCUS tag)
├── research_log.md     ← Gedeeld geheugen tussen agents
└── results/
    ├── baseline.json   ← Huidige baseline (fps, score)
    ├── BASELINE_SCORE  ← Enkele float voor vergelijking
    └── fixtures/       ← dataset.json + holdout.json
```

### De vaste interface (nooit wijzigen)

```python
def extract_segments(
    segments: List[Dict[str, Any]],
    output_dir: Path,
    progress_callback=None,
) -> Tuple[List[ExtractionResult], List[ExtractionResult]]:
    ...
```

### Scoring formule

```
extraction_score = (fps / baseline_fps)
                 × quality_multiplier      # f(mean_duration_error), 1.0 bij < 300ms afwijking
                 × reliability_multiplier  # success_rate²
```

Harde grenzen: `success_rate ≥ 0.90`, `mean_duration_error ≤ 0.50s`

## Gebruik

```bash
# Eenmalig: fixture bouwen
python benchmark/prepare.py --db data/stemmy-replica.db

# Baseline opslaan
python benchmark/evaluate.py --save-baseline

# Single-agent run (3 experimenten testen)
python benchmark/autoresearch.py --max-experiments 3

# Nacht-run (overnight)
python benchmark/autoresearch.py --max-experiments 100

# Parallelle run (3 agents tegelijk)
python benchmark/autoresearch.py --parallel-agents 3 --max-experiments 15

# Dashboard in apart terminal-venster
python benchmark/dashboard.py --watch

# Handmatig evalueren (zonder LLM)
python benchmark/evaluate.py
```

### Model opties

```bash
--model smart          # standaard: claude-sonnet-4-6 / gpt-4o / gemini-3.1-pro-preview
--model anthropic:claude-sonnet-4-6
--model google:gemini-3-flash-preview
--model openai:gpt-4o-mini
```

## Wat wel/niet werkt (geleerde lessen)

### Werkt ✅
- **Globale ThreadPoolExecutor**: één pool voor alle groepen van alle URLs → +1023% fps
- **BUFFER_BEFORE=8s**: lost VBR-timing-problemen op (Megaphone CDN)
- **Requests byte-range**: robuust, met fallback naar full download voor CDNs zonder Range support
- **Twee-pass FFmpeg seek**: nauwkeurige extractie zonder timing-artifacts
- **MAX_GROUP_SIZE=8**: minder FFmpeg-processes, minder startup-overhead

### Werkt niet ❌
- **FFmpeg direct HTTP** (`-i https://...`): Megaphone CDN timeout, niet robuust
  (3 van 43 URLs supporten geen Range requests → seek faalt)
- **`-c:a copy`**: MP3 frame-alignment fouten bij byte-range chunks;
  met 20 workers is de bottleneck het netwerk, niet CPU → kopiëren helpt niet
- **Full-URL download** (geen byte-range): te traag, timeout bij 90s budget

### Nog niet geprobeerd (RQ2-RQ4)
- RQ2: 30-40 workers (netwerk laat meer parallellisme toe?)
- RQ3: Batch-FFmpeg (meerdere outputs per aanroep, minder startup-overhead)
- RQ4: Gecombineerd (code copy + meer workers + kleinere buffers)

## Fixture-structuur

De test-fixture bevat 60 segmenten in 3 tiers:

| Tier | Segmenten | Doel |
|---|---|---|
| Dense | 20 | Korte segmenten dicht bij elkaar → test groepering |
| Sparse | 20 | Segmenten ver uit elkaar in dezelfde URL → test single-group pad |
| Cross-episode | 20 | 1 segment uit elk van 20 verschillende URLs → test URL-parallellisme |

De cross-episode tier is de meest veelzeggende: 20 URLs serieel verwerken kostte
~40s in de originele implementatie, ~3s met de globale executor.

## Tips voor toekomstig onderzoek

1. **Lees research_log.md vóór elk experiment** — beschrijft wat al geprobeerd is
2. **Bouw op bewezen baseline**: BUFFER_BEFORE=8s, 20 workers, groepen van 8
3. **Eén architectuurwijziging per experiment** — anders weet je niet wat hielp
4. **Monitor success_rate**: < 90% = automatische disqualificatie
5. **S3 warm cache**: herhaalde runs zijn sneller; vergelijk eerlijk
6. **3 Megaphone CDN URLs** supporten geen Range requests → full download fallback nodig
7. **Batch-FFmpeg** is de meest veelbelovende ongeteste richting (RQ3)

## Karpathy's patroon vs onze implementatie

Gebaseerd op het patroon van [karpathy/autoresearch](https://github.com/karpathy/autoresearch),
maar geheel zelf ontworpen voor audio-extractie:

| Karpathy (LLM training) | Stemmy benchmark (audio extractie) |
|---|---|
| `train.py` | `extractor_impl.py` |
| Validation loss | `extraction_score` |
| Training dataset | S3 audio fixture (60 segmenten) |
| git commit bij verbetering | identiek |
| Agent proposeert hyperparams | Agent schrijft volledige Python implementatie |

Extra features t.o.v. Karpathy: PeerValidator (3-run median), multi-agent worktrees,
multi-provider LLM (Anthropic/OpenAI/Google), Rich dashboard, HTML export.
