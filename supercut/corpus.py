"""
Woordindex over het hele stemmy-corpus.

Elk gesproken woord wordt één record met exacte timing, spreker, confidence en
de stilte ervoor/erna. Die stilte is de belangrijkste kwaliteitsfactor voor
supercuts: een woord met >120ms lucht aan beide kanten knipt schoon; een woord
midden in een zin ("eigenlijkdusja") niet.

Gebruik:
    idx = Corpus.load()                 # bouwt cache bij eerste run (~1 min)
    hits = idx.word("anyway")           # alle voorkomens, beste eerst
    hits = idx.phrase("hoe dan ook")    # aaneengesloten woordreeks
"""

from __future__ import annotations

import json
import pickle
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "stemmy-replica.db"
CACHE = Path(__file__).resolve().parent / "cache"
INDEX_PATH = CACHE / "index.pkl"


def norm(w: str) -> str:
    """Normaliseer een woord voor matching: lowercase, geen leestekens/accenten."""
    w = unicodedata.normalize("NFKD", w.lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    return re.sub(r"[^\w']", "", w).strip("'")


@dataclass
class Hit:
    """Eén voorkomen van een woord of frase in het corpus."""
    item_id: str
    audio_url: str
    start: float          # seconden
    end: float
    text: str             # originele tekst incl. leestekens
    speaker: str
    conf: float
    gap_before: float     # seconden stilte vóór (binnen fragment; 9 = fragmentgrens)
    gap_after: float
    fmt: str              # podcastnaam
    published: str
    ctx: str              # omringende zin

    @property
    def dur(self) -> float:
        return self.end - self.start

    @property
    def clean(self) -> float:
        """Knipbaarheid 0..1: lucht aan beide kanten + confidence."""
        air = min(self.gap_before, 0.4) / 0.4 * 0.5 + min(self.gap_after, 0.4) / 0.4 * 0.5
        return 0.65 * air + 0.35 * self.conf

    def __repr__(self):
        return (f"<{self.text!r} {self.fmt[:18]} {self.start:.2f}-{self.end:.2f} "
                f"gap={self.gap_before:.2f}/{self.gap_after:.2f} c={self.conf:.2f}>")


class Corpus:
    def __init__(self, data: dict):
        self.items = data["items"]          # item_id → (audio_url, fmt, published)
        self.vocab = data["vocab"]          # norm word → np.array van record-indices
        self.rec = data["rec"]              # structured array
        self.texts = data["texts"]          # originele woordtekst per record
        self.frag_text = data["frag_text"]  # fragment-index → zin
        self.item_list = data["item_list"]

    # ── bouwen ────────────────────────────────────────────────────────────
    @classmethod
    def build(cls, db_path: Path = DB_PATH) -> "Corpus":
        con = sqlite3.connect(db_path)
        items = {}
        for iid, url, fmt, pub in con.execute(
            "SELECT i.id, i.audio_url, f.title, i.published_at FROM items i "
            "LEFT JOIN formats f ON f.id = i.format_id"
        ):
            items[iid] = (url or "", fmt or "?", pub or "")
        item_list = list(items)
        item_pos = {iid: n for n, iid in enumerate(item_list)}

        dtype = [("item", "i4"), ("frag", "i4"), ("pos", "i2"), ("start", "i4"), ("end", "i4"),
                 ("conf", "f2"), ("gb", "i4"), ("ga", "i4"), ("spk", "U2")]
        rows, texts, frag_text = [], [], []
        q = ("SELECT item_id, item_audio_url, words, text FROM fragments "
             "WHERE words LIKE '[{%' ORDER BY item_id")

        def flush(iid, words):
            # Ontdubbelen (de DB bevat fragmenten meerdere keren) en gaps over
            # fragmentgrenzen heen berekenen op de hele aflevering.
            seen, uniq = set(), []
            for w in sorted(words, key=lambda w: (w[0], w[1])):
                key = (w[0] // 30, norm(w[2]))
                if key in seen or (uniq and w[0] < uniq[-1][1] - 30):
                    continue
                seen.add(key)
                uniq.append(w)
            n = len(uniq)
            for k, (s, e, t, c, spk, fi) in enumerate(uniq):
                gb = s - uniq[k - 1][1] if k > 0 else 9000
                ga = uniq[k + 1][0] - e if k < n - 1 else 9000
                rows.append((item_pos[iid], fi, min(k, 32000), s, e, c, gb, ga, spk))
                texts.append(t)

        cur_iid, cur_words = None, []
        for iid, furl, words_json, text in con.execute(q):
            if iid not in item_pos:
                continue
            if furl and not items[iid][0]:
                items[iid] = (furl,) + items[iid][1:]
            try:
                words = json.loads(words_json)
            except Exception:
                continue
            if iid != cur_iid:
                if cur_iid:
                    flush(cur_iid, cur_words)
                cur_iid, cur_words = iid, []
            fi = len(frag_text)
            frag_text.append(text or "")
            for w in words:
                cur_words.append((int(w.get("start", 0)), int(w.get("end", 0)), w.get("text", ""),
                                  float(w.get("confidence") or 0), str(w.get("speaker") or "")[:2], fi))
        if cur_iid:
            flush(cur_iid, cur_words)
        rec = np.array(rows, dtype=dtype)
        vocab: dict[str, list[int]] = {}
        for i, t in enumerate(texts):
            k = norm(t)
            if k:
                vocab.setdefault(k, []).append(i)
        vocab = {k: np.array(v, dtype=np.int32) for k, v in vocab.items()}
        data = dict(items=items, vocab=vocab, rec=rec, texts=texts,
                    frag_text=frag_text, item_list=item_list)
        CACHE.mkdir(exist_ok=True)
        with open(INDEX_PATH, "wb") as fh:
            pickle.dump(data, fh, protocol=5)
        return cls(data)

    @classmethod
    def load(cls) -> "Corpus":
        if INDEX_PATH.exists():
            with open(INDEX_PATH, "rb") as fh:
                return cls(pickle.load(fh))
        return cls.build()

    # ── zoeken ────────────────────────────────────────────────────────────
    def _hit(self, i0: int, i1: int) -> Hit:
        r0, r1 = self.rec[i0], self.rec[i1]
        iid = self.item_list[r0["item"]]
        url, fmt, pub = self.items[iid]
        return Hit(
            item_id=iid, audio_url=url, start=r0["start"] / 1000, end=r1["end"] / 1000,
            text=" ".join(self.texts[i0:i1 + 1]), speaker=str(r0["spk"]), conf=float(min(r0["conf"], r1["conf"])),
            gap_before=r0["gb"] / 1000, gap_after=r1["ga"] / 1000, fmt=fmt, published=pub,
            ctx=self.frag_text[r0["frag"]],
        )

    def word(self, w: str, min_conf: float = 0.0) -> list[Hit]:
        """Alle voorkomens van één woord, schoonste knip eerst."""
        idxs = self.vocab.get(norm(w), np.array([], dtype=np.int32))
        hits = [self._hit(i, i) for i in idxs if self.rec[i]["conf"] >= min_conf]
        return sorted(hits, key=lambda h: -h.clean)

    def phrase(self, p: str, min_conf: float = 0.0) -> list[Hit]:
        """Aaneengesloten woordreeks binnen één fragment."""
        toks = [norm(t) for t in p.split() if norm(t)]
        if len(toks) == 1:
            return self.word(toks[0], min_conf)
        out = []
        for i in self.vocab.get(toks[0], []):
            j = i + len(toks) - 1
            if j >= len(self.rec) or self.rec[j]["item"] != self.rec[i]["item"]:
                continue
            if all(norm(self.texts[i + k]) == toks[k] for k in range(1, len(toks))):
                h = self._hit(i, j)
                if h.conf >= min_conf:
                    out.append(h)
        return sorted(out, key=lambda h: -h.clean)

    def freq(self, w: str) -> int:
        return len(self.vocab.get(norm(w), ()))


if __name__ == "__main__":
    import sys, time
    t = time.time()
    c = Corpus.build() if "--rebuild" in sys.argv else Corpus.load()
    print(f"{len(c.rec):,} woorden, {len(c.vocab):,} uniek, {time.time()-t:.1f}s")
    for q in sys.argv[1:]:
        if q.startswith("--"):
            continue
        hs = c.phrase(q)
        print(f"\n{q!r}: {len(hs)} hits")
        for h in hs[:8]:
            print("  ", h)
