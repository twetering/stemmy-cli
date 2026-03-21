# Stemmy Extractie Benchmark — Onderzoeksprogramma

## Primaire Doelstelling

Maximaliseer `extraction_score` voor de extractie van podcastfragmenten uit S3-opgeslagen afleveringen.

```
extraction_score = (fps / baseline_fps) × quality_multiplier × reliability_multiplier

quality_multiplier   = clamp(1 - silence_ratio×2 - clipping_ratio×3, 0, 1)
reliability_multiplier = success_rate²
```

Hoger is beter. Baseline = 1.00 (huidige standaardconfiguratie).

---

## Harde Kwaliteitsgrenzen (nooit overtreden)

- `success_rate` ≥ 0.95 (minimaal 95% van fragmenten succesvol)
- `mean_duration_error_sec` ≤ 0.15 (maximaal 150ms afwijking van gevraagde duur)
- `clipping_ratio` ≤ 0.05 (maximaal 5% van samples klipt)
- `silence_ratio` ≤ 0.10 (maximaal 10% stilte aan begin/eind)

---

## Wat NOOIT te wijzigen

- De scoring-formule in `evaluate.py` (niet aanpasbaar)
- De fixture-dataset in `results/fixtures/` (read-only)
- Imports in `extractor_config.py`
- De `CONFIG = ExtractorConfig()` regel
- `parallel_extractor.py` (productiecode)

---

## Optimalisatierichtingen

### Direction 1: Concurrency Tuning (CURRENT FOCUS)

**Hypothese**: Het ThreadPoolExecutor draait standaard op 4 workers. Met 60 fragmenten
verdeeld over ~10 unieke audio-URLs is de bottleneck waarschijnlijk I/O-wachttijd op
S3 HTTP-requests. Meer workers = meer parallelle downloads = hogere throughput.

**Te proberen**:
- `max_workers`: 6, 8, 10, 12
- Let op: boven 12 workers kan S3 rate-limiting optreden (minder kans bij Range requests)
- Combineer met `max_group_size` verhoging zodat elke thread meer werk heeft

**Verwacht effect**: Elke verdubbeling van workers (tot S3-limiet) ≈ +30-50% fps.

**Aandachtspunten**:
- Hogere worker-counts verhogen geheugengebruik (~10MB per actieve thread)
- `executor_strategy="process"` is zwaarder qua overhead voor I/O-gebonden taken; probeer pas als "thread" verzadigd is

---

### Direction 2: Grouping Parameters

**Hypothese**: De huidige grouping (gap=10s, span=30s, size=5) is conservatief.
Ruimere groepen = minder HTTP-requests = minder round-trip latency.

**Te proberen**:
- `max_time_gap`: 6s, 8s, 12s, 15s
- `max_group_size`: 6, 8, 10
- `max_group_span`: 45s, 60s
- Combinaties: tight groups (gap=5, size=3) vs loose groups (gap=15, size=10)

**Verwacht effect**: Minder HTTP requests, grotere downloads per request. Netto-effect
afhankelijk van S3-latency vs. bandbreedte-tradeoff.

**Aandachtspunten**:
- Te grote groepen → grotere temp-bestanden → meer FFmpeg-overhead per extractie
- `sort_before_grouping=True` altijd bewaren voor correcte groepering

---

### Direction 3: Buffer Size Reductie

**Hypothese**: `buffer_before=3s` is conservatief. De byte-range berekening in
`_process_group()` alignt al op MP3-framegrenzen. Met `buffer_before=1.5s` of `2.0s`
kan de download kleiner zonder kwaliteitsverlies.

**Te proberen**:
- `buffer_before`: 2.5, 2.0, 1.5, 1.0
- `buffer_after`: 0.5, 0.3
- NOOIT onder `buffer_before=0.5` (evaluate.py blokkeert dit, risico clipping)

**Verwacht effect**: Kleinere downloads per groep = minder bytes = hogere fps.
Verwacht ~5-15% fps-verbetering per halvering van buffer_before.

**Meten**: Controleer `silence_ratio` zorgvuldig. Stijging boven 0.03 = buffer te klein.

---

### Direction 4: FFmpeg Optimalisatie

**Hypothese**: Twee FFmpeg-optimalisaties bieden significante snelheidswinst:
1. `ffmpeg_fade_duration=0` elimineert de fade-berekening (~15% sneller per segment)
2. `ffmpeg_codec="copy"` vermijdt re-encoding volledig (maar: geen fade mogelijk)

**Te proberen**:
- `ffmpeg_fade_duration=0.0` (met `ffmpeg_codec="libmp3lame"`)
- `ffmpeg_codec="copy"` + `ffmpeg_fade_duration=0.0` (let op: copy vereist exact-byte-aligned MP3)
- `ffmpeg_vbr_quality=4` of `ffmpeg_vbr_quality=6` (lagere kwaliteit, sneller)
- `ffmpeg_parallel_within_group=True` (segmenten binnen groep parallel extracten)

**Verwacht effect**:
- Fade verwijderen: +10-20% fps
- codec=copy: +30-50% fps maar risico op artefacten (silence/clipping check!)
- parallel_within_group: winst bij groups met ≥3 segmenten

**Aandachtspunten**:
- `codec=copy` werkt alleen correct als de byte-range perfect aligned is
- Monitor `clipping_ratio` en `mean_duration_error_sec` bij `codec=copy`

---

### Direction 5: HTTP Optimalisatie

**Hypothese**: De huidige `http_chunk_size=8192` en synchrone requests kunnen
geoptimaliseerd worden. Grotere chunks = minder iter_content-iteraties.

**Te proberen**:
- `http_chunk_size`: 16384, 32768, 65536 (grotere chunks)
- `http_max_retries`: 1 (minder overhead bij stabiele verbinding)
- `enable_prefetch=True` (prefetch volgende groep tijdens FFmpeg-verwerking)

**Verwacht effect**: Klein (~5%), maar gratis in combinatie met andere optimalisaties.

---

### Direction 6: Gecombineerde Optimalisatie (geavanceerd)

Na het uitputten van afzonderlijke richtingen: combineer de beste bevindingen.

**Te proberen**:
- Best workers + best grouping params + reduced buffers + no fade
- Systematische grid-search over 2-3 parameters tegelijk
- Pas `ffmpeg_parallel_within_group=True` toe in combinatie met hogere `max_group_size`

---

## Stopped Directions

*(Automatisch bijgewerkt door `autoresearch.py` als een richting 10+ experimenten zonder verbetering heeft)*

---

## Experiment Budget

- Standaard overnight run: 100 experimenten
- Tijdbudget per experiment: 90 seconden
- Verwachte looptijd: ~2.5 uur
- Multi-agent (4x): ~4 × 25 exp parallel = 100 exp in ~45 minuten

---

## Noten voor de Agent

1. **Kijk naar de history**: Wat werkte? Wat niet? Bouw op successen.
2. **Één ding tegelijk**: Wijzig bij voorkeur 1-2 parameters per experiment voor interpreteerbare resultaten.
3. **Let op silence_ratio**: Dit is een vroeg waarschuwingssignaal voor kwaliteitsproblemen.
4. **fps × kwaliteit**: Een 2× snellere maar incorrect extracterende config scoort slechter dan de baseline.
5. **Git is je geheugen**: Alle commits zijn zichtbaar in de dashboard — bouw voort op wat al gecommit is.
