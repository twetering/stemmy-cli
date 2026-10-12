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

## e-20260322-172343-4fb91da | score: 0.9541 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL (geen lokale download) met `-c:a copy` en een nauwkeurige pre-seek van 1.5s, gecombineerd met maximaal parallellisme (20 workers) voor alle groepen tegelijk.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=14.665 | success_rate=1.000 | dur_err=0.147s | time=4.1s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 13.6× baseline fps.

---

---

## e-20260322-172229-4fb91da | score: 0.2689 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 1.5s én een nauwkeurige fine-seek, maar voeg een trim-filter toe via `-af atrim=start=0:end=duration,asetpts=PTS-STARTPTS` zodat de exacte duur wordt afgedwongen zonder re-encode overhead.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=4.165 | success_rate=1.000 | dur_err=0.102s | time=14.4s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 3.9× baseline fps.

---

---

## e-20260322-172127-4fb91da | score: 0.3473 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 1.5s én een nauwkeurige fine-seek, maar voeg ook een trim-filter toe via `-af atrim` zodat de output exact de gewenste duur heeft, wat de duration_error minimaliseert terwijl copy-snelheid behouden blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=5.380 | success_rate=1.000 | dur_err=0.010s | time=11.2s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Sterke snelheidswinst: 5.0× baseline fps.

---

---

## e-20260322-172029-4fb91da | score: 0.3266 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 1.5s maar voeg daarna een nauwkeurige trim toe via `-af atrim` zodat de output-duur exact overeenkomt met de gewenste segment-duur, waardoor timing-afwijking < 0.3s wordt en quality_multiplier = 1.0 is.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=4.982 | success_rate=1.000 | dur_err=0.208s | time=12.0s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 4.6× baseline fps.

---

---

## e-20260322-171929-4fb91da | score: 0.7750 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 1.5s én een nauwkeurige fine-seek, maar trim het output-bestand daarna opnieuw met libmp3lame als de duration-error > 0.15s is, zodat we snelheid van copy combineren met timing-precisie van re-encode.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=11.912 | success_rate=1.000 | dur_err=0.074s | time=5.0s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 11.0× baseline fps.

---

---

## e-20260322-171828-4fb91da | score: 0.3634 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 1.0s voor optimale balans tussen snelheid en timing-nauwkeurigheid, gebaseerd op de observatie dat 1.5s buffer de beste score (0.9463) gaf maar dur_err=0.419s net boven de 0.3s quality_multiplier drempel zat — 1.0s buffer zou sneller zijn én de dur_err onder 0.3s brengen voor quality_multiplier=1.0.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=5.629 | success_rate=1.000 | dur_err=0.131s | time=10.7s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 5.2× baseline fps.

---

---

## e-20260322-171741-4fb91da | score: 0.9463 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 1.5s zodat FFmpeg voldoende context heeft voor nauwkeurige MP3-frame-seeking terwijl de timing-afwijking onder 0.3s blijft voor quality_multiplier=1.0.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=15.073 | success_rate=1.000 | dur_err=0.419s | time=4.0s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.419s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 14.0× baseline fps.

---

---

## e-20260322-171645-4fb91da | score: 0.3762 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van precies 0.5s (minimale preroll) zodat FFmpeg bijna geen audio hoeft te verwerken voor de seek, wat de timing-afwijking onder 0.3s houdt terwijl de copy-snelheid behouden blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=5.696 | success_rate=1.000 | dur_err=0.216s | time=10.5s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 5.3× baseline fps.

---

---

## e-20260322-171544-4fb91da | score: 0.2288 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een pre-seek buffer van exact 2s (kleiner dan 4s maar groter dan 1s) om de timing-afwijking onder 0.3s te houden terwijl stream-copy snelheid behouden blijft, met libmp3lame fallback.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=3.686 | success_rate=1.000 | dur_err=0.445s | time=16.3s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.445s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 3.4× baseline fps.

---

---

## e-20260322-171436-4fb91da | score: 0.3288 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een nauwkeurige pre-seek van precies 4s vóór het segment en een fine-seek van 4s na inlezen, zodat FFmpeg op een MP3-frame-boundary aligneert en de duration-error onder 0.3s blijft (quality_multiplier=1.0).
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=5.327 | success_rate=1.000 | dur_err=0.457s | time=11.3s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.457s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 4.9× baseline fps.

---

---

## e-20260322-171338-4fb91da | score: 0.8409 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` met een grotere pre-seek buffer (4s) zodat FFmpeg altijd op een frame-boundary begint, gecombineerd met `-af atrim` als fallback-correctie voor timing-nauwkeurigheid onder 0.3s, terwijl directe HTTP-streaming de download-overhead elimineert.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=12.732 | success_rate=1.000 | dur_err=0.180s | time=4.7s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 11.8× baseline fps.

---

---

## e-20260322-171207-4fb91da | score: 0.0979 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL (zonder download) met `-c:a libmp3lame` en nauwkeurige twee-pass seek, maar reduceer timing-afwijking door de pre-seek op precies `start_time - 0.5s` te zetten (minimale preroll) zodat duration_error < 0.3s en quality_multiplier = 1.0, terwijl globale parallelisme (20 workers) de fps hoog houdt.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=1.493 | success_rate=1.000 | dur_err=0.033s | time=40.2s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid.

---

---

## e-20260322-170858-4fb91da | score: 0.0066 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL (geen download, geen byte-range) met `-c:a copy` voor maximale snelheid, maar voeg een nauwkeurige twee-stap seek toe (pre-seek + fine-seek) zodat de timing-afwijking onder 0.3s blijft en de quality_multiplier = 1.0 is.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.267 | success_rate=0.617 | dur_err=0.000s | time=138.6s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.62 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-170807-4fb91da | score: 0.2207 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` als primaire extractiemethode met automatische fallback naar libmp3lame, maar beperk timing-afwijking door pre-seek te aligneren op een GOP-boundary via een grotere preroll voor copy-mode.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=3.727 | success_rate=1.000 | dur_err=0.548s | time=16.1s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.548s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 3.5× baseline fps.

---

---

## e-20260322-170712-4fb91da | score: 0.6557 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` als primaire extractiemethode (geen re-encode) om FFmpeg-verwerkingstijd drastisch te reduceren, met libmp3lame als fallback bij seek-imprecisie of corrupt output.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=11.968 | success_rate=1.000 | dur_err=0.709s | time=5.0s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.709s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 11.1× baseline fps.

---

---

## e-20260322-172912-4fb91da | score: 0.4447 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL met nauwkeurige pre-seek en libmp3lame re-encode, maar verhoog MAX_WORKERS naar 30 en verlaag buffers zodat meer groepen parallel lopen en de totale doorlooptijd daalt.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=6.941 | success_rate=1.000 | dur_err=0.035s | time=8.6s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Sterke snelheidswinst: 6.4× baseline fps.

---

---

## e-20260322-172818-4fb91da | score: 0.4560 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL met nauwkeurige `-ss` pre-seek en libmp3lame re-encode, maar met een GROTERE ThreadPoolExecutor (40 workers) en agressievere groepering om maximaal parallellisme te bereiken voor alle 3 fixture-lagen.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=6.956 | success_rate=1.000 | dur_err=0.035s | time=8.6s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Sterke snelheidswinst: 6.4× baseline fps.

---

---

## e-20260322-172544-4fb91da | score: 0.0053 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct op HTTP-URL met pre-seek en libmp3lame re-encode, maar verhoog het aantal workers naar 30 en optimaliseer groepering (grotere gap/span) voor maximale parallellisme-benutting op de cross-episode fixture.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.333 | success_rate=0.500 | dur_err=0.037s | time=90.1s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.50 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172409-4fb91da | score: 0.0079 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct op HTTP-URL met `-ss` pre-seek, libmp3lame re-encode, en maximale parallelisme (30 workers) — gebaseerd op het beste resultaat (score=0.7780) maar met hogere worker-count om throughput verder te verhogen.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.498 | success_rate=0.500 | dur_err=0.035s | time=60.2s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.50 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172209-4fb91da | score: 0.0034 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct op HTTP-URL met `-ss` pre-seek, libmp3lame re-encode, en maximaal 30 parallelle workers om de snelheid te maximaliseren terwijl timing-nauwkeurigheid behouden blijft (beste combinatie uit experiment e-20260322-172135: fps=7.990, dur_err=0.036s, maar met meer workers).
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.287 | success_rate=0.433 | dur_err=0.035s | time=90.5s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.43 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172135-4fb91da | score: 0.5119 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op HTTP-URL met pre-seek, libmp3lame re-encode, maar maximaal 30 parallelle workers en grotere groepen voor hogere throughput terwijl timing-nauwkeurigheid behouden blijft (beste score was e-20260322-171456 met fps=11.959, sr=1.00, dur_err=0.108s).
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=7.990 | success_rate=1.000 | dur_err=0.036s | time=7.5s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Sterke snelheidswinst: 7.4× baseline fps.

---

---

## e-20260322-172024-4fb91da | score: 0.2808 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL met nauwkeurige `-ss` pre-seek en `-t` duration, met `-c:a libmp3lame` re-encode maar met maximaal parallellisme (20 workers) en een minimale buffer (2s) om de beste score te repliceren van e-20260322-171456 (fps=11.959, score=0.7780).
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=4.252 | success_rate=1.000 | dur_err=0.031s | time=14.1s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Sterke snelheidswinst: 3.9× baseline fps.

---

---

## e-20260322-171757-4fb91da | score: 0.0159 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL met nauwkeurige `-ss` pre-seek + `-t` duration en `-c:a copy` voor snelheid, maar val terug op libmp3lame met atrim als copy te veel duration-error geeft — beste score was 0.7780 met deze aanpak, nu verfijn met betere seek-strategie voor lagere dur_err.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.417 | success_rate=0.767 | dur_err=0.035s | time=110.3s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.77 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-171456-4fb91da | score: 0.7780 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL met nauwkeurige `-ss` pre-seek + fine-seek en `-c:a libmp3lame` re-encode, maar optimaliseer timing door buffer te verkleinen naar 2s en fine_seek exact op chunk_time_offset te zetten zonder extra aftrek, zodat duration_error daalt terwijl fps hoog blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=11.959 | success_rate=1.000 | dur_err=0.108s | time=5.0s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Sterke snelheidswinst: 11.1× baseline fps.

---

---

## e-20260322-171407-4fb91da | score: 0.4512 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op HTTP-URL met nauwkeurige twee-pass seek (pre-seek + fine-seek) en libmp3lame re-encoding, maar met geoptimaliseerde groepering (MAX_TIME_GAP=60s, MAX_GROUP_SPAN=120s) en 30 workers voor maximaal parallellisme, waarbij de beste score (0.4315) als basis dient maar timing-nauwkeurigheid wordt verbeterd door BUFFER_BEFORE te vergroten naar 15s voor betere VBR-robuustheid.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=7.331 | success_rate=1.000 | dur_err=0.446s | time=8.2s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.446s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 6.8× baseline fps.

---

---

## e-20260322-171255-4fb91da | score: 0.2057 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct op de HTTP-URL met `-ss` pre-seek en nauwkeurige `-t` extractie via `libmp3lame`, maar beperk de timing-fout door de seek-offset te verfijnen: gebruik `-ss` ALLEEN vóór `-i` (één-pass seek) zonder fine_seek correctie die de dubbele seek introduceert, gecombineerd met asyncio-gebaseerde parallelle uitvoering voor maximale doorvoer.
**Verandering**: async I/O (aiohttp/asyncio), ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=3.138 | success_rate=1.000 | dur_err=0.033s | time=19.1s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Sterke snelheidswinst: 2.9× baseline fps.

---

---

## e-20260322-171142-4fb91da | score: 0.4315 | status: PARTIAL
**Hypothese**: Verbeter timing-nauwkeurigheid door FFmpeg twee-pass seek te vervangen door een enkele nauwkeurige seek met `-c:a copy` voor snelheid, maar gebruik `-af atrim` voor frame-exacte trimming om zowel snelheid als lage duration_error te combineren.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=6.692 | success_rate=1.000 | dur_err=0.357s | time=9.0s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.357s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 6.2× baseline fps.

---

---

## e-20260322-171044-4fb91da | score: 0.3882 | status: PARTIAL
**Hypothese**: Gebruik `-c:a copy` (geen re-encode) ipv `libmp3lame` voor snellere extractie, met automatische fallback naar libmp3lame als copy mislukt, zodat zowel snelheid als betrouwbaarheid behouden blijven.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=6.728 | success_rate=1.000 | dur_err=0.603s | time=8.9s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.603s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 6.2× baseline fps.

---

---

## e-20260322-170726-4fb91da | score: 0.0000 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct op HTTP-URL (zonder byte-range download) met globale ThreadPoolExecutor, zodat FFmpeg zelf de HTTP-streaming en seeking beheert — elimineert de volledige download+tempfile overhead.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.000 | success_rate=0.000 | dur_err=0.000s | time=150.0s
**Conclusie**: TIMEOUT: alle 150s opgebruikt, 0 segmenten klaar. Implementatie te traag (waarschijnlijk volledige bestanden downloaden of serieel). Kritiek: success_rate=0.00 < 0.90 (disqualificatie). Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172805-4fb91da | score: 0.0068 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming met -ss pre-seek en libmp3lame re-encoding, maar met nauwkeurige seek-kalibratie via een kleine download-buffer (2s pre-roll) en 40 workers voor maximaal parallellisme, gebaseerd op de beste score van e-20260322-170805 (score=0.3352, dur_err=0.305s) maar met betere timing-nauwkeurigheid door langere pre-roll.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.356 | success_rate=0.550 | dur_err=0.038s | time=92.7s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.55 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172451-4fb91da | score: 0.0000 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming met -ss pre-seek en libmp3lame re-encoding maar met ruime BUFFER_BEFORE (30s) en nauwkeurige twee-pass seek om timing-nauwkeurigheid te maximaliseren terwijl snelheid behouden blijft via 40 workers en globale executor.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.000 | success_rate=0.000 | dur_err=0.000s | time=150.0s
**Conclusie**: TIMEOUT: alle 150s opgebruikt, 0 segmenten klaar. Implementatie te traag (waarschijnlijk volledige bestanden downloaden of serieel). Kritiek: success_rate=0.00 < 0.90 (disqualificatie). Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172222-4fb91da | score: 0.0086 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming met -c:a copy als primaire methode én libmp3lame fallback, maar voeg een nauwkeurige -af atrim filter toe voor precieze tijdsafkapping, om zowel snelheid (geen re-encode) als timing-nauwkeurigheid (dur_err < 0.3s) te combineren.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.372 | success_rate=0.600 | dur_err=0.012s | time=96.8s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.60 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-172020-4fb91da | score: 0.0217 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct-from-URL met -ss pre-seek en libmp3lame re-encoding voor nauwkeurige timing, maar met optimale worker count (40) en geen byte-range overhead — directe URL-streaming elimineert download+tempfile latentie terwijl timing nauwkeurig blijft (dur_err < 0.3s).
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.530 | success_rate=0.800 | dur_err=0.007s | time=90.5s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.80 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-171802-4fb91da | score: 0.0371 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming met -c:a copy als primaire methode en libmp3lame als fallback, maar voeg een nauwkeurige -af atrim filter toe om timing-afwijking onder 0.3s te houden terwijl snelheid maximaal blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.579 | success_rate=1.000 | dur_err=0.037s | time=103.6s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-171709-4fb91da | score: 0.3143 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming met -c:a copy voor maximale snelheid, maar voeg een nauwkeurige re-encode fallback toe met libmp3lame én verhoog BUFFER_BEFORE naar 30s zodat de timestamp-seek veel nauwkeuriger is en duration_error onder 0.3s blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=9.812 | success_rate=1.000 | dur_err=1.874s | time=6.1s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (1.874s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 9.1× baseline fps.

---

---

## e-20260322-171350-4fb91da | score: 0.0000 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming met -c:a copy voor maximale snelheid, maar voeg een nauwkeurige libmp3lame fallback toe met ruimere pre-seek buffer (4s) om de timing-afwijking van 0.507s terug te brengen tot onder 0.3s terwijl hoge fps behouden blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.000 | success_rate=0.000 | dur_err=0.000s | time=150.0s
**Conclusie**: TIMEOUT: alle 150s opgebruikt, 0 segmenten klaar. Implementatie te traag (waarschijnlijk volledige bestanden downloaden of serieel). Kritiek: success_rate=0.00 < 0.90 (disqualificatie). Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-171129-4fb91da | score: 0.0374 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct-from-URL met -ss timestamp-seek en -c:a copy voor snelheid, maar voeg nauwkeurige -af atrim+asetpts toe voor exacte duratie-controle om dur_err onder 0.3s te brengen terwijl success_rate 1.0 blijft.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.571 | success_rate=1.000 | dur_err=0.005s | time=105.2s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-170900-4fb91da | score: 0.0040 | status: FAIL
**Hypothese**: Gebruik FFmpeg direct-from-URL met -ss timestamp-seek en -c:a copy (stream-copy zonder re-encoding), maar voeg een nauwkeurige fijn-seek toe via -af atrim voor exacte duratie-controle, gecombineerd met fallback naar libmp3lame bij copy-mislukking en 40 workers voor maximaal parallellisme.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range)
**Resultaat**: fps=0.252 | success_rate=0.500 | dur_err=0.000s | time=118.9s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.50 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---

---

## e-20260322-170805-4fb91da | score: 0.3352 | status: PARTIAL
**Hypothese**: Vervang libmp3lame re-encoding door -c:a copy voor stream-copy zonder transcoding, met fallback naar libmp3lame bij mislukking, om CPU-overhead te elimineren en extractiesnelheid te maximaliseren.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=5.086 | success_rate=1.000 | dur_err=0.305s | time=11.8s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.305s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 4.7× baseline fps.

---

---

## e-20260322-170717-4fb91da | score: 0.6482 | status: PARTIAL
**Hypothese**: Gebruik FFmpeg direct-from-URL streaming (geen byte-range download) met `-c:a copy` voor maximale snelheid, waarbij FFmpeg zelf HTTP-range-seeking beheert via `-ss` voor nauwkeurige MP3-extractie zonder lokale tempfiles.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=10.741 | success_rate=1.000 | dur_err=0.507s | time=5.6s
**Conclusie**: Geen verbetering tov baseline. Perfecte success_rate (1.0) — VBR-probleem opgelost? Timing-afwijking hoog (0.507s) — seek-nauwkeurigheid controleren. Sterke snelheidswinst: 9.9× baseline fps.

---

---


## e-20260321-223022-e110531 | score: 0.0000 | status: FAIL
**Hypothese**: Gebruik volledige URL-download (geen byte-range) voor VBR-correctheid, gecombineerd met globale ThreadPoolExecutor en per-URL caching om zowel betrouwbaarheid als parallelisme te maximaliseren.
**Verandering**: ThreadPoolExecutor, FFmpeg direct HTTP (geen byte-range), globale executor (cross-URL parallelisme)
**Resultaat**: fps=0.000 | success_rate=0.000 | dur_err=0.000s | time=90.0s
**Conclusie**: Geen verbetering tov baseline. Kritiek: success_rate=0.00 < 0.90 (disqualificatie). Uitstekende timing-nauwkeurigheid. Trager dan baseline — overhead analyse nodig.

---
