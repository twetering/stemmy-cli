---
name: supercut
description: Maak artistieke supercuts/videoclips uit het stemmy-podcastarchief (zinnen zoeken, knippen, componeren op muziek, AI-stem, verticale video). Gebruik bij vragen om compilaties, supercuts, montages of videoclips van podcastfragmenten.
---

# Supercut-werkwijze (stemmy)

## Snel naar iets bekijkbaars
- Eerst een PoC van ~1 min binnen ~5 min rekentijd; pas opschalen na feedback.
- Voorbeeld en sjabloon: `supercut/pieces/ik_denk.py` (zoeken → knippen → componeren → mix → video).
  `PYTHONPATH=src venv/bin/python -m supercut.pieces.ik_denk [--stills]`

## Materiaal
- Zoeken: `supercut.corpus.Corpus.load()`; woordindex met timing, ontdubbeld (de DB bevat fragmenten dubbel).
  `c.vocab[woord]` → recordindices; `c.texts`/`c.rec` geven woorden + ms-timing per aflevering.
- **Werk met hele zinnen**, geen losse woorden: losse woorden zitten in vloeiende spraak vast aan hun buren.
  Zin = vanaf een woord met hoofdletter na een zinseinde, tot het eerste woord dat op `. ? !` eindigt; 1,2–6,5 s.
- Knippen: bestaande `stemmy_cli.audio.parallel_extractor.ParallelExtractor` (byte-range, ffprobe-bitrate). 24 zinnen ≈ 5 s.
  Pad 0,06 s ervoor en 0,18 s erna.
- De DB-timing is soms fout (bijv. bij een aflevering van "Man man man"). Controleer de mix daarom met **ElevenLabs STT**
  (`POST /v1/speech-to-text`, model `scribe_v1`, key in `.env`) en vervang wat niet klopt. Geen lokale Whisper: te traag.
- "Ervaring voor Beginners" heeft geen sprekerlabels (alles A) en geïnterpoleerde woordtiming.

## Klank
- `supercut.mix`: Event-tijdlijn → `render()` (pan, galmsend) → `master()` (-14 LUFS, two-pass loudnorm).
- Leg alles op een beat-grid (bv. 96 BPM). Bruikbare vormen: stotter op achtsten, zin per maat, canon (om de 2 tellen, L/R), stilte als slot.
- AI-stem: `stemmy_cli.tts.elevenlabs.generate_tts(text, voice_id, output_path=…)`; voice-ids uit de tabel `voices`
  (gebruik `COALESCE(elevenlabs_id, voice_id)`).

## Beeld
- `supercut.video.render(paint, duur, audio, out)`: PIL-frames worden naar ffmpeg gepijpt, formaat 1080×1920.
- Glow werkt met een aparte `glow`-laag, die op ¼ resolutie vervaagd wordt en bij het beeld wordt opgeteld (zie `ik_denk.painter`).
- Laat woorden verschijnen op hun echte woordtiming; dat geeft directe synchronisatie.
- Controleer eerst met `--stills` en één contactsheet (`ffmpeg … tile=7x1`) voordat je de hele video rendert (~2,5 min).
