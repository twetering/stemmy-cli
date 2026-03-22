# Stemmy Extractie Benchmark — Research Log

Gedeeld geheugen voor alle agents. Elke agent voegt een entry toe na een experiment.
Lees dit vóór je een nieuw experiment ontwerpt — niet opnieuw uitvinden wat al geprobeerd is.

Formaat per entry:
```
## [experiment_id] | score: X.XXXX | status: COMMIT / FAIL / PARTIAL
**Hypothese**: Wat wilde je bereiken?
**Verandering**: Wat heb je concreet anders gedaan?
**Resultaat**: Wat meet de evaluator?
**Conclusie**: Werkte het? Waarom wel/niet? Wat suggereert dit voor de volgende stap?
```

---

## Baseline e-20260321-210047 | score: 1.0000 | status: BASELINE

**Implementatie**: Referentie-implementatie uit `parallel_extractor.py`.

**Architectuur**:
- ThreadPoolExecutor per URL (niet globaal) → seriële URL-verwerking
- Download naar tempfile, dan FFmpeg per segment
- ffprobe synchroon per URL voor metadata
- Greedy lineaire groepering (gap/span/size limieten)

**Gemeten**:
- fps: ~1.08 | success_rate: 0.95 (57/60) | total_time: ~52s
- 3 failures: waarschijnlijk byte-range fout bij VBR audio

**Bekende zwakheden**:
1. Cross-episode tier (43 URLs × 1 segment) = serieel → geen parallellisme
2. Tempfile schrijf/lees = dubbele I/O per groep
3. Fixed bitrate aanname werkt niet voor VBR

**Aanbeveling voor volgende agent**: Begin met RQ1 (globale executor).
Dit is de laaghangende vrucht — één architectuurwijziging, grote impact.

---

## NIEUWE BASELINE e-20260322 | score: 1.0000 | status: BASELINE (v2)

**Implementatie**: Handmatig geschreven baseline in `extractor_impl.py` (globale executor + VBR-fix).

**Architectuur**:
- Globale ThreadPoolExecutor (20 workers) over ALLE groepen van ALLE URLs tegelijk
- Python requests byte-range download per groep → tempfile
- FFmpeg twee-pass timestamp seeking per segment (BUFFER_BEFORE=8s voor VBR-robuustheid)
- Greedy groepering (gap=10s, span=30s, size=8)

**Gemeten**:
- fps: ~15-17 (gecached S3) | success_rate: 1.00 (60/60) | total_time: ~3-4s (warm cache)
- 0 failures! VBR-probleem opgelost via ruime buffer (8s preroll)
- Eerste run (cold cache): fps≈2.2, time≈27s

**Wat werkt**:
1. Globale executor elimineert serieel-per-URL bottleneck
2. BUFFER_BEFORE=8s absorbeert VBR-timing imprecisie
3. Twee-pass FFmpeg seek voor nauwkeurige extractie

**Wat verder onderzocht kan worden**:
1. `-c:a copy` ipv libmp3lame (geen re-encode → 5-10× sneller per segment)
2. Meer workers (30-40) voor meer parallellisme
3. Async HTTP (aiohttp) voor minder context-switch overhead
4. Kleinere byte-ranges (minder download voor veraf segmenten)
5. aiofiles + async FFmpeg voor volledige async pipeline

**Waarschuwing**: Baseline meet warm S3-cache performance. Agents die ANDERE byte-ranges gebruiken zullen koudere cache zien → enigszins oneerlijk. Focus op algoritme-verbeteringen die ook cold-cache beter presteren.

---
<!-- Nieuwe entries hieronder toevoegen -->

## e-20260321-223022-e110531 | score: 0.0000 | status: FAIL
**Hypothese**: Gebruik volledige URL-download (geen byte-range) voor VBR-correctheid, gecombineerd met globale ThreadPoolExecutor en per-URL caching om zowel betrouwbaarheid als parallelisme te maximaliseren.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.000 | success_rate=0.000 | dur_err=0.000s | time=90.0s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.00 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---
