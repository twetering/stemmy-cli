"""Afleveringspool voor Nederlandstalige stukken: veel verschillende stemmen."""
from __future__ import annotations

import random
import sqlite3

from .corpus import DB_PATH

NL_FORMATS = ("Ervaring voor Beginners", "POM%", "Man man man%", "%Poki%", "Slimmer Presteren%",
              "Maarten van Rossem%", "HKU en AI", "Voorheen Schaamteloos%", "De Jortcast", "Kunstmatig",
              "Live Slow Ride Fast%")


def dutch_pool(n_evb: int = 125, n_other: int = 16, seed: int = 11) -> list[tuple[str, str, str, str]]:
    """→ [(item_id, audio_url, format, titel)] met ruime woorddekking."""
    con = sqlite3.connect(DB_PATH)
    rows = []
    for pat in NL_FORMATS:
        rows += con.execute(
            "SELECT i.id, i.audio_url, f.title, i.title, COUNT(fr.id) n FROM items i "
            "JOIN formats f ON f.id=i.format_id JOIN fragments fr ON fr.item_id=i.id "
            "WHERE f.title LIKE ? AND i.audio_url LIKE 'http%' GROUP BY i.id HAVING n > 150",
            (pat,)).fetchall()
    rng = random.Random(seed)
    evb = [r for r in rows if r[2] == "Ervaring voor Beginners"]
    other = [r for r in rows if r[2] != "Ervaring voor Beginners"]
    rng.shuffle(evb)
    rng.shuffle(other)
    # andere podcasts: max 2 per format (zelfde presentatoren → stemcheck vangt dubbelen)
    picked, per = [], {}
    for r in other:
        if per.get(r[2], 0) < 2:
            picked.append(r)
            per[r[2]] = per.get(r[2], 0) + 1
    return [r[:4] for r in evb[:n_evb] + picked[:n_other]]
