# duet-test

Smoke-test HET DUET 3.0: start server, doe een sessie + 2 beats, stop server.

```bash
# Start server op vrije poort
python3 duet/server.py --db data/stemmy-replica.db --port 7790 &
SRV_PID=$!
sleep 3

# Sessie starten
SESSION_JSON=$(curl -s -X POST http://localhost:7790/duet/session/start \
  -H 'Content-Type: application/json' \
  -d '{"theme":"muziek","n_voices":3,"script_style":"vrij"}')
echo "Session response:"
echo "$SESSION_JSON" | python3 -m json.tool | grep -E '"session_id|ensemble_voices|arc_building|palette_ready'

SESSION_ID=$(echo "$SESSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['session']['session_id'])")
echo "Session ID: $SESSION_ID"

# Beat 1
echo "\n--- Beat 1 ---"
curl -s -X POST http://localhost:7790/duet/session/beat \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\"}" | python3 -m json.tool | grep -E '"beat_type|voice_id|voice_color|degraded_mode|narrator_audio_id|archive_audio_id'

# Beat 2
echo "\n--- Beat 2 ---"
curl -s -X POST http://localhost:7790/duet/session/beat \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\"}" | python3 -m json.tool | grep -E '"beat_type|voice_id|voice_color|degraded_mode'

kill $SRV_PID 2>/dev/null
echo "\nDone."
```

Controleer in de output:
- `ensemble_voices` bevat 3 stemmen met echte elevenlabs IDs (niet "fallback")
- `beat_type` is aanwezig per beat
- `voice_id` verschilt tussen beats (rotatie werkt)
- `degraded_mode` is "full" of "narrator-only-bridge" (niet "browser-fallback" tenzij TTS faalt)
