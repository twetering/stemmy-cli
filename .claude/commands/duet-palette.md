# duet-palette

Bouw en inspecteer een FragmentPalette voor een thema — handig voor het debuggen van fragment-intelligentie en entiteits-detectie.

Gebruik: `/duet-palette` (past het thema aan via $ARGUMENTS of vraag de gebruiker)

```bash
THEME="${ARGUMENTS:-politiek}"

python3 -c "
from duet.fragment_palette import build_palette
import sys

theme = '$THEME'
print(f'Bouwen palette voor: {theme!r}')
palette = build_palette(theme, 'data/stemmy-replica.db')

print(f'\nTotaal fragmenten: {palette.total_found}')
print('Dramatische rollen:')
for role, frags in palette.by_role.items():
    if frags:
        print(f'  {role}: {len(frags)} fragmenten')
        for f in frags[:2]:
            print(f'    • [{f.energy_score:.2f}] {f.text[:80]!r}')

print(f'\nTop entiteiten: {palette.top_entities}')
print(f'Entiteits-ketens: {list(palette.entity_chains.keys())}')
print(f'\nSamenvatting: {palette.summary}')
"
```

**DB-note**: Entiteiten worden gevonden via `item_id` + tijdsoverlap (niet via `fragment_id` — die kolom bestaat niet).
