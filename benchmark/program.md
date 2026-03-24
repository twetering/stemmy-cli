# Stemmy Extractie Benchmark — Onderzoeksprogramma (v2)

## Doel

Maximaliseer `extraction_score` voor de extractie van podcastfragmenten uit S3.
De agent schrijft de volledige implementatie in `extractor_impl.py` opnieuw.
Dit is architectuuronderzoek — geen hyperparameter-tuning.

```
extraction_score = (fps / baseline_fps) × quality_multiplier × reliability_multiplier
quality_multiplier   = f(mean_duration_error) — bestraft timing-fouten > 300ms
reliability_multiplier = success_rate²
```

**Baseline v2**: ~15 fps (warm S3-cache) | success_rate = 1.00 (60/60) | score = 1.0000

---

## Harde grenzen (nooit overtreden)

- `success_rate` ≥ 0.90 (minimaal 90% van fragmenten succesvol)
- `mean_duration_error_sec` ≤ 0.50 (maximaal 500ms afwijking van gevraagde duur)
- De functiesignatuur `extract_segments(segments, output_dir, progress_callback)` is VAST
- Geen destructieve operaties buiten `output_dir`
- Gebruik ALLEEN standaard Python stdlib + `requests` + `subprocess` (geen aiohttp, tenzij geïnstalleerd)

---

## Baseline implementatie (extractor_impl.py — jouw vertrekpunt)

De huidige baseline:
- Globale ThreadPoolExecutor (20 workers) over alle groepen van alle URLs
- Python requests byte-range download per groep → tempfile
- FFmpeg twee-pass timestamp seeking (BUFFER_BEFORE=8s voor VBR-robuustheid)
- Greedy groepering (gap=10s, span=30s, size=8)
- Gebruik van `/opt/homebrew/bin/ffmpeg` via `_find_binary()` helper

**Bewezen**: 60/60 success, geen VBR-failures, ~15 fps gecached.

---

## Bekende kansen voor verbetering

### Research Question 1: FFmpeg codec copy (CURRENT FOCUS)

**Probleem**: `libmp3lame` re-encodeert elk segment (CPU-intensief, ~0.3-0.5s per segment).
Voor MP3 bronbestanden is re-encoding overbodig — we kunnen direct kopiëren.

**Hypothese**: `-c:a copy` ipv `-c:a libmp3lame -q:a 2` elimineert transcoding volledig.
Verwacht: 3-5× sneller per segment. Risk: seek imprecisie bij stream-copy.

**Aanpak**:
```python
cmd = [_FFMPEG, "-y",
       "-ss", f"{pre_seek:.3f}", "-i", tmp_path,
       "-ss", f"{fine_seek:.3f}",
       "-t", f"{duration:.3f}",
       "-c:a", "copy",           # ← geen re-encode
       "-avoid_negative_ts", "make_zero",
       outpath]
# Fallback naar libmp3lame als copy faalt (returncode != 0)
```

**Succes-criterium**: score > 1.10, success_rate ≥ 0.95, mean_duration_error < 0.5s.

---

### Research Question 2: Meer workers + kleinere HTTP chunk

**Probleem**: 20 workers is een eerste schatting. S3 ondersteunt veel meer parallelle verbindingen.
Byte-range chuck omvat 8s preroll + segment + 2s postroll = ~2-5MB per groep (veel).

**Hypothese**: 30-40 workers + BUFFER_BEFORE=3s (voldoende voor CBR S3) → minder download + meer parallellisme.

**Risico**: BUFFER_BEFORE=3s kan VBR-fouten introduceren (de 3 Megaphone failures terugkeren).
Monitor success_rate nauwlettend!

---

### Research Question 3: Batch-FFmpeg (meerdere segmenten per FFmpeg-aanroep)

**Probleem**: Elke segment = aparte FFmpeg-process (startup overhead ~0.1s per segment).
Voor dense-tier (20 segmenten dicht bij elkaar): we kunnen ze in één FFmpeg-aanroep extraheren.

**Hypothese**: FFmpeg `-filter_complex` of meerdere output-paden per aanroep → 60 segmenten in 10 FFmpeg-processen ipv 60.

**Aanpak**:
```python
# Meerdere outputs per FFmpeg aanroep:
cmd = ["ffmpeg", "-i", tmpfile,
       "-ss", "10", "-t", "3", "seg_a.mp3",
       "-ss", "20", "-t", "4", "seg_b.mp3"]
```

**Complexiteit**: hoog (FFmpeg output-selector logica). Alleen als RQ1+RQ2 uitgeput zijn.

---

### Research Question 4: Gecombineerde optimalisatie

Na bewijs van individuele verbeteringen: combineer codec-copy + 32 workers + kleinere buffers.
Verwacht: 2-3× sneller dan huidige baseline.

---

## Stopped Directions

*(Automatisch bijgewerkt door `autoresearch.py`)*

- **FFmpeg direct HTTP (zonder download)**: Geprobeerd. Voor Megaphone CDN (3 URLs) faalt seek → timeout 30s+ per segment. S3 werkt prima maar andere CDN niet → niet robuust genoeg.

---

## Notities voor de agent

1. **Schrijf echte code, geen configuratiewaarden.** De volledige implementatie mag worden herschreven.
2. **Documenteer hypothese** bovenaan: `# HYPOTHESE: <één zin>`
3. **Bouw op de research log.** Lees wat vorige agents ontdekten.
4. **Één architecturale verandering per experiment.**
5. **De `_find_binary()` helper is cruciaal** — gebruik die altijd voor ffmpeg/ffprobe (niet hardcoded strings).
6. **Fixture bevat 3 tiers**: dense (20 segs samen), sparse (20 segs ver uit elkaar), cross-episode (20 segs van 20 verschillende URLs). Test mentaal op alle 3.
7. **3 Megaphone CDN URLs** (traffic.megaphone.fm) supporten geen Range requests → fall back naar full download. De huidige baseline doet dit correct.
8. **S3 CDN caching**: herhaalde runs zijn sneller (warm cache). Jouw innovatie wordt eerlijk vergeleken als de baseline ook warm-cache gebruikt.
