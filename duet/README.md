# HET DUET 3.0

Postmoderne, meerstemmige audio-installatie gebouwd op 193k+ gearchiveerde radiofragmenten en 149 ElevenLabs-stemmen. Bezoekers vallen in een vreemd maar enigszins coherent groepsgesprek waarbij identiteiten vaag blijven.

## Starten

```bash
python3 duet/server.py --db data/stemmy-replica.db --port 7777 --browser
```

Opties:
- `--db` — pad naar de SQLite-database (verplicht)
- `--port` — standaard 7777
- `--browser` — open de browser automatisch

URL: `http://localhost:7777/duet`

## Architectuur

```
duet/
├── server.py           # HTTP-server (stdlib, geen framework) + sessie-lifecycle
├── state.py            # Datamodellen: BeatType, BeatPlan, SceneState, RenderedTurn
├── ensemble.py         # Multi-stem beheer: voice-selectie uit DB, rotatie
├── arc_planner.py      # Volledige arc pre-planning (10–15 beats via LLM)
├── fragment_palette.py # Fragment-intelligentie: dramatische rollen, entiteits-ketens
├── retrieval.py        # Fragment-zoeken: text + semantic + entity + palette-scoring
├── audio_engine.py     # Audio-rendering per BeatType (ffmpeg, TTS, micro-extract)
├── providers/
│   ├── llm.py          # Beat-planning via Claude/Gemini
│   └── tts.py          # TTS via ElevenLabs/Gemini (gecached)
└── static/
    └── app.html        # Frontend (vanilla JS, geen bundler)
```

## HTTP API

Alle endpoints onder `/duet/`:

| Method | Path | Beschrijving |
|--------|------|--------------|
| GET | `/duet` | Serveert `static/app.html` |
| GET | `/duet/audio/<id>` | Serveert gecached MP3-fragment |
| POST | `/duet/session/start` | Start nieuwe sessie |
| POST | `/duet/session/beat` | Bereken + render volgende beat |
| POST | `/duet/session/interrupt` | Interrupt huidige beat (reset arc) |
| POST | `/duet/audiomatch` | Zoek fragment op audio-hash (no rate-limit) |
| POST | `/duet/live/respond` | Gemini Live response (experimenteel) |

### POST /duet/session/start

Request:
```json
{
  "theme": "muziek",
  "n_voices": 3,
  "script_style": "vrij",
  "format_id": null,
  "mode": "orchestrated",
  "tts_provider": "elevenlabs",
  "planner_provider": "claude"
}
```

Response bevat `session` object met o.a.:
- `session_id` — gebruik in volgende requests
- `ensemble_voices` — lijst van `{id, name, color, register}`
- `arc_total` / `arc_consumed` / `arc_beat_types` — arc-voortgang
- `arc_building` / `palette_ready` — achtergrondstatus

### POST /duet/session/beat

Request: `{"session_id": "..."}`

Response bevat naast het beat-resultaat ook het volledige `session`-object.

## BeatTypes

| Type | Beschrijving | Gedrag |
|------|-------------|--------|
| `normal` | Standaard narrator + fragment | Crossfade |
| `silence` | Opzettelijke pauze | `silence_ms` veld, geen audio |
| `interrupt` | Fragment kapt narrator af | Narrator getrimd tot 60%, daarna fragment |
| `montage` | 2–3 fragmenten snel achter elkaar | `montage_audio_ids` lijst |
| `callback` | Eerder fragment herhaald | Uit arc `callback_map` |
| `contrapuntal` | Narrator + fragment overlappen | JS speelt met 500ms offset |
| `micro` | 1–5 woorden geëxtraheerd | Echo-stijl uit word-level timing |

## Sessie-lifecycle

```
session/start
  └─ select_ensemble_voices()       # kiest n stemmen uit DB
  └─ _build_palette_worker (thread) # bouwt FragmentPalette op achtergrond
        └─ _build_arc_worker (thread)  # zodra palette klaar: plan 10–15 beats

session/beat (herhaald)
  └─ pop ArcPlan.beats[arc_consumed]  # of live planning als arc leeg
  └─ pick_next_voice()                # rotatie, max 2x zelfde stem op rij
  └─ retrieve_candidates()            # text + semantic + entity search
  └─ _apply_palette_scoring()         # +0.25 rol-bonus, +0.12 entity-ketens
  └─ render_turn()                    # TTS + ffmpeg fragment-extract
  └─ _apply_turn()                    # update scène, voice_history, arc_consumed
```

## Database-eigenaardig heden

- `voices.elevenlabs_id` — het échte ElevenLabs voice ID (gebruik dit!)
- `voices.voice_id` — is NULL voor de meeste rijen (niet gebruiken als ID)
- `entities` heeft geen `fragment_id` — join via `item_id` + tijdsoverlap:
  ```sql
  JOIN fragments f ON f.item_id = e.item_id
    AND f.start_time_seconds <= e.start_time_seconds
    AND f.end_time_seconds >= e.start_time_seconds
  ```
- `fragments.item_id` bestaat (is niet zichtbaar in eerste 10 PRAGMA-kolommen)

## Environment variabelen

| Variabele | Beschrijving | Standaard |
|-----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Claude API | — |
| `GOOGLE_API_KEY` | Gemini API | — |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS | — |
| `DUET_VOICE_ID` | Voorkeurs ElevenLabs voice ID (stem_0) | `PIpnKISoU0pJEeZu0rOq` |
| `DUET_MODEL` | Claude-model voor arc/beat planning | `claude-sonnet-4-6` |
| `DUET_GEMINI_TTS_MODEL` | Gemini TTS-model | `gemini-2.5-flash-preview-tts` |
| `DUET_GEMINI_TEXT_MODEL` | Gemini text-model (palette summary) | `gemini-2.5-flash` |

## Script-stijlen

| Stijl | Kenmerken |
|-------|-----------|
| `vrij` | Gebalanceerde defaults, LLM kiest dramaturgie |
| `kakofonie` | Hoge simultaneity (35%), veel interrupts |
| `verhoor` | Veel stiltes (20%), callbacks, lage gelijktijdigheid |
| `requiem` | Lange stiltes (28%), veel callbacks, trage opbouw |

## Caches

- `duet/cache/tts/` — gecachte TTS-audio (hash van tekst+stem)
- `duet/cache/frags/` — gecachte archief-fragmenten (hash van URL+tijdsstempel)
- `duet/cache/bridges/` — gecachte gap/bridge-fragmenten

Cache-bestanden zijn lokaal en worden niet opgeruimd automatisch.
