# GRAFSTEMMING

> *"Scoor echo's uit het verleden."*

Een web-kunstinstallatie: roep een woord, hoor de echo terug vanuit een podcastarchief. De stem van iemand anders, ergens in de tijd, zegt precies dat woord — en galmt terug uit de put.

Gebaseerd op het verhaal van Echo, de nimf die door Hera werd vervloekt om nooit zelf te spreken — alleen de laatste woorden van anderen kon ze herhalen. Totdat ze stierf van liefdesverdriet en alleen haar stem overbleef. Dit is haar graf.

---

## Snel starten

```bash
# Met alle podcasts
python3 echo/server.py

# Gefilterd op één podcast (UUID uit de database)
python3 echo/server.py --format <podcast-uuid>

# Andere poort of DB-locatie
python3 echo/server.py --port 8080 --db /pad/naar/stemmy.db

# Open automatisch in browser
python3 echo/server.py --browser
```

→ `http://localhost:7843/echo`

**Vereisten:** Python 3.10+, ffmpeg (`brew install ffmpeg`), stemmy-cli venv actief.

---

## Hoe het werkt — en waarom het zo snel is

### De woordenindex (opbouw 1x, ~8s)

Bij opstart scant de server alle fragmenten in de SQLite-database en bouwt een in-memory index:

```
word → [{ word_start_ms, word_end_ms, audio_url, ctx_before, ctx_after }, ...]
```

46.000+ unieke woorden met exacte tijdstempels en audiolocaties. Na opbouw is elk woord in milliseconden vindbaar.

### Byte-range extractie

Audio staat op S3 (of andere HTTP-bron). `ParallelExtractor` gebruikt HTTP Range-requests om alleen de bytes te downloaden die overeenkomen met het gevraagde tijdsegment. Een fragment van 3 seconden uit een aflevering van 60 minuten = ~40KB download i.p.v. 60MB.

### Echo-filter (ffmpeg)

Het fragment krijgt een layered echo:

```
aecho=1.0:0.85:120|350|700|1400:0.55|0.38|0.22|0.10
```

Vier vertragingen (120ms, 350ms, 700ms, 1400ms) met afnemende amplitude — een realistische putecho.

### Tussenruimte-audio (gaps)

De index verzamelt ook stiltegebieden tussen woorden van 600ms–3s: de hmm's, lachjes en ademhalingen. Ze worden gebruikt als ghost-fluistering op de achtergrond.

### Caching

Elk geëxtraheerd fragment wordt opgeslagen in `echo/cache/<format>/word.mp3`. Herhaalde verzoeken zijn instant (< 5ms). Cache is per podcast gesplitst zodat cross-podcast hits onmogelijk zijn.

---

## API

| Endpoint | Beschrijving |
|----------|-------------|
| `GET /echo` | De web-app |
| `GET /echo/api?word=X&v=0-3` | MP3 van woord, variant 0-3 = korte/lange context |
| `GET /echo/random` | Willekeurig zeldzaam woord + frequentie (JSON) |
| `GET /echo/gap` | Tussenruimte-audio: hmm/lach/adem (MP3) |
| `GET /echo/ready` | Index-status (JSON: `{ready, words}`) |
| `GET /echo/podcasts` | Podcastlijst uit database (JSON) |
| `GET /echo/filter?id=X` | Wissel naar specifiek podcast-archief |
| `GET /echo/music/horror2.mp3` | Achtergrondmuziek |

**Response-headers bij `/echo/api`:**
- `X-Word-Matched` — gevonden woord (zin → zeldzaamste woord)
- `X-Word-Frequency` — frequentie in het archief
- `X-Context-Before` / `X-Context-After` — URL-encoded contextwoorden

---

## Puntensysteem

```
score = rarity_bonus + length_bonus + streak_bonus

rarity_bonus  = max(1, round(500 / log10(freq + 2)))
length_bonus  = max(0, (len(word) - 3) × 3)
streak_bonus  = floor(streak × 2)
```

Een woord dat 1x voorkomt = 500 punten. Veelgebruikte woorden = 1-50 punten.

---

## Deployen

### Lokaal / achter Nginx

```bash
python3 echo/server.py --port 7843
```

Nginx-proxy:
```nginx
location /echo {
    proxy_pass http://localhost:7843;
    proxy_set_header Host $host;
}
```

### Fly.io (aanbevolen — database is 1.3GB, ffmpeg vereist)

Vercel/Netlify zijn **niet** geschikt vanwege de DB-grootte en ffmpeg-dependency.

```toml
# fly.toml
[build]
  dockerfile = "echo/Dockerfile"

[mounts]
  source = "stemgraf_data"
  destination = "/data"

[[services]]
  internal_port = 7843
  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]
```

```dockerfile
# echo/Dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install -e ".[dev]"
CMD ["python3", "echo/server.py", "--port", "7843", \
     "--db", "/data/stemmy.db", "--no-browser"]
```

```bash
fly launch
fly volumes create stemgraf_data --size 5
fly deploy
```

Kosten: ~€7/maand (shared-cpu-1x, 512MB RAM).

### Railway.app

Vergelijkbaar met Fly.io, eenvoudigere setup. Kies 512MB+ RAM vanwege de in-memory index (~200MB).

---

## Kunstinstallatie-contexten

### Digitale toepassingen

- **Poëziewebsite** — bezoekers zoeken woorden die de dichter gebruikte; horen ze in originele context
- **Collectief geheugen** — hoor hoe duizenden mensen hetzelfde woord anders uitspreken
- **Journalistiek onderzoek** — hoe gebruikten politici bepaalde termen in de afgelopen 10 jaar?
- **Educatief** — studenten horen woorden uitgesproken in échte gesprekken
- **Taalmuseum** — dialecten, uitstervende talen, historische opnames

### Commerciële toepassingen

- **Museuminstallatie** — eigen archief van interviews met bekende figuren
- **Radiostation** — luisteraars navigeren live door het archief via stem
- **Corporate memory** — bedrijf maakt installatie van vergaderopnames/speeches
- **Podcastnetwerk** — maak de collectie toegankelijk als interactieve ervaring

### Fysieke installatie: de echte put

```
                    [ microfoon ]
                         |
    ┌────────────────────┼────────────────────┐
    │                    ▼                    │
    │         ╔══════════════════╗            │
    │         ║   houten put     ║            │
    │         ║   (donker gat)   ║            │
    │         ║                  ║            │
    │         ║  [4-8 speakers]  ║            │
    │         ╚══════════════════╝            │
    │                                         │
    │         Mac Mini / RPi achter muur      │
    └─────────────────────────────────────────┘
```

**Componenten:**
- Ronde vloersectie met donkere afdekking — de "put" als sculptuur
- Microfoon op statief aan de rand — bezoeker fluistert woorden
- 4-8 kleine full-range speakers in/rond de cirkel — surround-echo
- Optioneel scherm — toont zinsfragment-context en score
- Raspberry Pi 5 (8GB) of Mac Mini — draait de server lokaal
- Horror-soundscape loopt continu op laag volume

**Software-setup voor kiosk:**
```bash
# Raspberry Pi 5 of Mac Mini
python3 echo/server.py --port 80 --db /usb/stemmy.db --no-browser

# Kiosk-browser (Chromium volledig scherm)
chromium-browser --kiosk --no-sandbox http://localhost/echo
```

**Materialenlijst (schatting €500–2000):**
| Component | Prijs |
|-----------|-------|
| Raspberry Pi 5 (8GB) + behuizing | ~€120 |
| 6× full-range speakers | ~€200 |
| Mini-versterker (2×50W) | ~€80 |
| Microfoon + statief | ~€60 |
| USB SSD 2TB (database + cache) | ~€120 |
| Houten/stenen put-constructie | €100–1500 |

---

## Veiligheid

De server heeft de volgende beveiligingslagen ingebouwd:

| Maatregel | Detail |
|-----------|--------|
| Rate limiting | Max 40 req/10s per IP (`MAX_REQ_PER_10S` aanpasbaar) |
| Input sanitisatie | Max 120 tekens, alleen veilige unicode karakters |
| Path traversal | `..` in URL geblokkeerd |
| Bestandsnaam-validatie | Music-endpoint: alleen `[\w\-]+\.mp3` |
| Log-sanitisatie | Control characters gestript uit logs |
| Cache-isolatie | Per-podcast subdirectory |

**Aanbevolen voor productie:**
```bash
# Draai achter Nginx/Caddy (geen directe poortblootstelling)
# HTTPS via Let's Encrypt (gratis via Caddy: caddy reverse-proxy --to :7843)
# Overweeg Fail2ban voor herhaaldelijke 429-responses
```

---

## Licentie

Onderdeel van `stemmy-cli`. De gebruikte audiobestanden zijn eigendom van de respectievelijke podcast-makers. Grafstemming geeft alleen toegang tot audio waarvoor je zelf de rechten bezit of een licentie hebt.
