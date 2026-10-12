# Stemmy CLI — Codebase Instructies

## Project

Python CLI voor podcast audio-extractie, compilaties en installaties.
Main package: `src/stemmy_cli/` | DB: `data/stemmy-replica.db` (SQLite) | Branch: `feature/rpgrefactor`

## HET DUET (`duet/`)

Postmoderne meerstemmige audio-installatie. Start:
```bash
python3 duet/server.py --db data/stemmy-replica.db --browser
```

### Architectuur snel overzicht

```
server.py → state.py (datamodellen)
         → ensemble.py (voice-selectie uit DB)
         → arc_planner.py (pre-planning 10–15 beats via LLM, in background thread)
         → fragment_palette.py (dramatische rollen + entiteits-ketens, in background thread)
         → retrieval.py (text/semantic/entity search + palette-scoring)
         → audio_engine.py (TTS + ffmpeg + BeatType-dispatch)
         → providers/llm.py (beat planning Claude/Gemini)
         → providers/tts.py (ElevenLabs/Gemini TTS, gecached)
         → static/app.html (frontend, vanilla JS)
```

### HTTP endpoints

```
POST /duet/session/start   — nieuwe sessie, payload: {theme, n_voices, script_style, format_id}
POST /duet/session/beat    — volgende beat, payload: {session_id}
POST /duet/session/interrupt — interrupt + arc-reset, payload: {session_id}
GET  /duet/audio/<id>      — gecachte MP3 ophalen
GET  /duet                 — serveert static/app.html
```

### DB-valkuilen

**KRITIEK**: `voices.voice_id` is NULL voor vrijwel alle rijen. Het echte ElevenLabs ID zit in `voices.elevenlabs_id`. Altijd `COALESCE(elevenlabs_id, voice_id)` gebruiken.

**KRITIEK**: `entities` heeft geen `fragment_id` kolom — join via item_id + tijdsoverlap:
```sql
JOIN fragments f ON f.item_id = e.item_id
  AND f.start_time_seconds <= e.start_time_seconds
  AND f.end_time_seconds >= e.start_time_seconds
```

`fragments.item_id` bestaat (niet zichtbaar in `PRAGMA table_info` eerste 10 rijen, maar wel aanwezig).

### Sessie-flow

1. `session/start` → `select_ensemble_voices()` → start `_build_palette_worker` (thread)
2. Palette klaar → start `_build_arc_worker` (thread) → `ArcPlan` met 10–15 beats
3. `session/beat` → pop `arc.beats[arc_consumed]` (of live planning) → `pick_next_voice()` → `retrieve_candidates()` → `render_turn()`
4. Interrupt → arc invalide, volgende beat gebruikt live planning

### BeatTypes (`duet/state.py`)

`normal`, `silence` (sleep_ms), `interrupt` (narrator getrimd ~60%), `montage` (2–3 frags), `callback` (fragment herhaald), `contrapuntal` (overlap 500ms), `micro` (1–5 woorden via word-timing)

---

## Benchmark (`benchmark/`)

Karpathy autoresearch-lus voor fragment-extractie optimalisatie.

```bash
python3 benchmark/autoresearch.py --max-experiments 100   # nachtrun
python3 benchmark/autoresearch.py --parallel-agents 4     # multi-agent
python3 benchmark/dashboard.py --watch
```

- `extractor_impl.py` — agent-editeerbaar (de "train.py")
- `evaluate.py` — vaste evaluatie, metric: `extraction_score`
- `research_log.md` — gedeeld geheugen tussen agents (lees vóór experiment)

---

## Stijlconventies

- Nederlandstalige output/comments in duet/ en CLI-code
- Rich voor terminal output
- Geen onnodige abstracties, geen speculative features
