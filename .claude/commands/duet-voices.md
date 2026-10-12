# duet-voices

Bekijk de beschikbare ElevenLabs-stemmen in de database en test voice-selectie.

```bash
# Hoeveel stemmen zijn er?
python3 -c "
import sqlite3
conn = sqlite3.connect('data/stemmy-replica.db')
total = conn.execute('SELECT COUNT(*) FROM voices WHERE elevenlabs_id IS NOT NULL AND elevenlabs_id != \"\"').fetchone()[0]
nl = conn.execute('SELECT COUNT(*) FROM voices WHERE language = \"nl\" AND elevenlabs_id IS NOT NULL').fetchone()[0]
print(f'Totaal met elevenlabs_id: {total}')
print(f'Nederlandstalig: {nl}')
print()

# Sample
rows = conn.execute('SELECT name, elevenlabs_id, language, category, tags FROM voices WHERE elevenlabs_id IS NOT NULL LIMIT 10').fetchall()
for r in rows:
    print(r)
"

# Test ensemble-selectie
python3 -c "
from duet.ensemble import select_ensemble_voices
for style in ['vrij', 'kakofonie', 'verhoor']:
    config = select_ensemble_voices('data/stemmy-replica.db', n=3, script_style=style)
    print(f'\nStijl: {style}')
    for v in config.voices:
        print(f'  {v.id}: {v.db_name!r} ({v.register}) → {v.elevenlabs_id}')
"
```

**Let op**: Gebruik altijd `elevenlabs_id`, nooit `voice_id` (die is NULL voor de meeste rijen).
