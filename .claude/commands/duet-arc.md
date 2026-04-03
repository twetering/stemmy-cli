# duet-arc

Plan en inspecteer een volledige arc (10–15 beats) voor een thema zonder een echte sessie te starten.

```bash
python3 -c "
import os
from duet.state import SceneState, ARC_STAGES
from duet.arc_planner import plan_full_arc
from duet.ensemble import select_ensemble_voices
from duet.fragment_palette import build_palette

theme = '${ARGUMENTS:-muziek}'
db = 'data/stemmy-replica.db'

print(f'Arc plannen voor thema: {theme!r}')

# Bouw minimale scène
ensemble = select_ensemble_voices(db, n=3)
palette = build_palette(theme, db)

scene = SceneState(session_id='test', theme=theme)
scene.ensemble = ensemble
scene.fragment_palette = palette
scene.script_style = 'vrij'

arc = plan_full_arc(scene)
print(f'\nScript-stijl: {arc.script_style}')
print(f'Motieven: {arc.motifs}')
print(f'Beats ({len(arc.beats)}):')
for i, b in enumerate(arc.beats):
    cb = f' [callback→{arc.callback_map.get(i,\"?\")}]' if i in arc.callback_map else ''
    print(f'  {i+1:2d}. [{b.beat_type:12s}] [{b.voice_id or \"?\":8s}] {b.spoken_line[:60]!r}{cb}')
"
```
