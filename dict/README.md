# 📖 Stemmy Woordenboek

Van Dale-achtig interactief woordenboek gegenereerd uit podcast-audio.
Elk woord is afspeelbaar als **losse uitspraak** én **in context** (de zin waarin het gezegd werd).

---

## 🚀 Server starten

```bash
# Snelste manier — opent browser automatisch op poort 7842
python3 dict/server.py

# Andere letter direct openen
python3 dict/server.py --letter G

# Andere poort
python3 dict/server.py 8080

# Alleen server, geen browser
python3 dict/server.py --no-browser
```

Daarna open je: **http://localhost:7842/woordenboek_P.html**

---

## 🔨 Nieuw woordenboek genereren

```bash
# Letter P (standaard — thematisch voor POM podcast)
./scripts/run_dictionary.sh P

# Letter G (287 unieke woorden in POM)
./scripts/run_dictionary.sh G

# Met meer woorden en clips
./scripts/run_dictionary.sh A 80 4
#                            ^  ^  ^
#                          letter  limit  clips per woord

# Of direct via Python
python3 scripts/dictionary.py --letter W --limit 50 --clips 3
```

Gegenereerde bestanden staan in `output/dictionary/`:
```
output/dictionary/
  woordenboek_P.html    ← de dictionary pagina
  audio/
    podcast_0_word.mp3  ← los woord
    podcast_0_ctx.mp3   ← in context (de zin)
    ...
```

---

## 📊 Beschikbare data (POM Podcast)

| Letter | Unieke woorden |
|--------|---------------|
| A | 216 |
| B | 282 |
| D | 202 |
| G | 287 |
| M | 199 |
| P | 167 |
| S | 282 |
| V | 271 |
| ... | ... |
| **Totaal** | **3.839** |

---

## 🏗 Architectuur

```
scripts/dictionary.py     ← hoofdscript (generatie)
scripts/run_dictionary.sh ← bash-wrapper met handige defaults
dict/server.py            ← HTTP-server voor lokale preview
dict/README.md            ← dit bestand
output/dictionary/        ← gegenereerde output (niet in git)
```

**Pipeline:**
1. **DB-query** — alle fragmenten van POM met word-timing (milliseconden)
2. **Zin-splitsing** — groepeert woorden op pauze >800ms of leesteken
3. **Audio-extractie** — ParallelExtractor (20 workers, ~15 fps)
   - Woord-clip: ±80ms buffer rondom het woord
   - Context-clip: ±200ms buffer rondom de volledige zin
4. **HTML-generatie** — statische pagina, audio via `<Audio>` in browser

---

## 🔧 Opties `dictionary.py`

| Argument | Default | Beschrijving |
|----------|---------|-------------|
| `--letter` | `P` | Letter om te indexeren |
| `--db` | `data/stemmy-replica.db` | SQLite database |
| `--format` | POM UUID | Format/podcast UUID |
| `--limit` | `60` | Max unieke woorden (op frequentie) |
| `--clips` | `3` | Clips per woord |
| `--out` | `output/dictionary` | Output map |

---

## 💡 Tips

- **Andere podcast**: zoek de format UUID in de DB:
  ```bash
  python3 -c "
  import sqlite3
  conn = sqlite3.connect('data/stemmy-replica.db')
  for r in conn.execute('SELECT id, title FROM formats'): print(r)
  "
  ```
  Geef dan `--format <uuid>` mee.

- **Alle letters genereren** (lang!):
  ```bash
  for L in A B C D E F G H I J K L M N O P Q R S T U V W X Y Z; do
    ./scripts/run_dictionary.sh $L 60 3
  done
  ```

- **Server op achtergrond**:
  ```bash
  python3 dict/server.py --no-browser &
  echo "Server draait op http://localhost:7842"
  ```

---

## 🐛 Bekende beperkingen

- Woord-clips van korte woorden (<200ms) kunnen abrupt klinken — de 80ms padding helpt maar is niet altijd genoeg
- Megaphone CDN-URL's vereisen correcte bitrate-detectie (ffprobe per URL, +3s startup)
- Gegenereerde HTML is statisch — geen zoekfunctie of filtering
