"""
Casting: wijs elke tekst-eenheid één stem toe uit het archief.

Optimalisatieprobleem: eenheden × afleveringen, elke aflevering (≈ stem) maar
één keer. Opgelost met de Hongaarse methode op knipkwaliteit + prosodie,
daarna iteratief: Whisper-verificatie, stem-uniciteit, en eenheden die nergens
schoon te knippen zijn worden samengevoegd met een buur ('heb' → 'ik heb').
"""

from __future__ import annotations

import hashlib
import pickle
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from .audio import SR, decode
from .corpus import CACHE, Corpus
from .harvest import Clip, candidates, context_voice, make_clip, voice
from .verify import judge


def recurring_voices(c: Corpus, items: list[str], fmt: str, per_item: int = 6,
                     share: float = 0.4, seed: int = 3) -> np.ndarray | None:
    """
    Stemmen die in ≥ `share` van de afleveringen van één format terugkomen
    (= presentatoren). → matrix van centroids, of None.
    """
    cache = CACHE / f"hosts_{hashlib.sha1(fmt.encode()).hexdigest()[:10]}.npy"
    if cache.exists():
        return np.load(cache)
    rng = np.random.default_rng(seed)
    jobs = []
    for iid in items:
        if c.items[iid][1] != fmt:
            continue
        it = c.item_list.index(iid)
        starts = c.rec["start"][c.rec["item"] == it]
        if len(starts) < 50:
            continue
        for t in rng.choice(starts, per_item, replace=False) / 1000:
            jobs.append((iid, round(float(t), 2)))

    def fetch(job):
        iid, t = job
        try:
            return iid, np.asarray(decode(c.items[iid][0])[int(t * SR):int((t + 3) * SR)])
        except Exception:
            return iid, None
    embs, owner = [], []
    with ThreadPoolExecutor(16) as ex:
        for iid, x in ex.map(fetch, jobs):
            if x is not None and len(x) > SR:
                embs.append(voice(x))
                owner.append(iid)
    if not embs:
        return None
    E = np.stack(embs)
    owner = np.array(owner)
    n_items = len(set(owner))
    S = E @ E.T
    rec = np.array([len(set(owner[(S[k] > 0.55) & (owner != owner[k])])) for k in range(len(E))])
    host = E[rec >= share * n_items]
    if not len(host):
        return None
    # cluster de host-embeddings grof (er kunnen twee presentatoren zijn)
    cents = []
    for v in host / np.linalg.norm(host, axis=1, keepdims=True):
        if not cents or max(float(v @ c_) for c_ in cents) < 0.6:
            cents.append(v)
    cents = np.stack([h_ / np.linalg.norm(h_) for h_ in
                      [host[(host @ c_) > 0.55].mean(0) for c_ in cents]])
    np.save(cache, cents)
    return cents


class Casting:
    def __init__(self, c: Corpus, items: list[str], hosts: np.ndarray | None = None,
                 lang: str = "nl", host_thr: float = 0.47, same_voice_thr: float = 0.62):
        self.c, self.items, self.hosts, self.lang = c, items, hosts, lang
        self.host_thr, self.same_voice_thr = host_thr, same_voice_thr
        self.pool: dict[int, dict[str, list[Clip]]] = {}     # unit → item → clips
        self.banned: set[tuple[int, str, float]] = set()
        self.verified: dict[tuple[int, str, float], tuple[str, str]] = {}

    def harvest(self, units: list[tuple[str, str]], per_item: int = 2, limit: int = 140, log=print):
        """Verzamel knipbare kandidaten voor alle eenheden, per aflevering één decode."""
        todo = defaultdict(list)
        itemset = set(self.items)
        for u, (text, want) in enumerate(units):
            if u in self.pool:
                continue
            self.pool[u] = defaultdict(list)
            for p, h in candidates(self.c, text, want, itemset, per_item=per_item, limit=limit):
                todo[h.item_id].append((u, text, p, h))
        flat = [x for lst in todo.values() for x in lst]
        log(f"  {len(flat)} kandidaten uit {len(todo)} afleveringen ophalen (range-requests)…")

        def work(job):
            u, text, p, h = job
            try:
                cl = make_clip(h, text, p)
                if cl is not None and cl.edge_db >= 9 and self.hosts is not None:
                    context_voice(h, embed=False)      # venster alvast ophalen
                return u, cl
            except Exception:
                return u, None
        done = 0
        with ThreadPoolExecutor(16) as ex:
            for u, cl in ex.map(work, flat):
                done += 1
                if done % 250 == 0:
                    log(f"    {done}/{len(flat)}")
                if cl is None or cl.edge_db < 9:
                    continue
                if self.hosts is not None:
                    cl.emb = context_voice(cl.hit)
                    if float(np.max(self.hosts @ cl.emb)) > self.host_thr:
                        continue                       # presentator, niet de gast
                self.pool[u][cl.hit.item_id].append(cl)
        for u in self.pool:
            for iid in self.pool[u]:
                self.pool[u][iid].sort(key=lambda cl: -cl.score)

    def _best(self, u: int, iid: str) -> Clip | None:
        for cl in self.pool[u].get(iid, []):
            if (u, iid, cl.t0) not in self.banned:
                return cl
        return None

    def assign(self, units, max_rounds: int = 12, verify: bool = True, log=print) -> dict[int, Clip]:
        items = self.items
        for rnd in range(max_rounds):
            cost = np.full((len(units), len(items)), 50.0)
            for u in range(len(units)):
                for j, iid in enumerate(items):
                    cl = self._best(u, iid)
                    if cl is not None:
                        cost[u, j] = -cl.score
            rows, cols = linear_sum_assignment(cost)
            chosen = {}
            for u, j in zip(rows, cols):
                if cost[u, j] < 50:
                    chosen[u] = self._best(u, items[j])
            # 1) blind luisteren
            bad = 0
            for u, cl in chosen.items():
                key = (u, cl.hit.item_id, cl.t0)
                if not verify:
                    continue
                if key not in self.verified:
                    self.verified[key] = judge(cl.x, units[u][0], self.lang)
                cl.verdict, cl.heard = self.verified[key]
                if cl.verdict != "exact":
                    self.banned.add(key)
                    bad += 1
            if not verify:
                return chosen
            # 2) twee keer dezelfde stem?
            embs = {u: (cl.emb if cl.emb is not None else voice(cl.x)) for u, cl in chosen.items()}
            for u, cl in chosen.items():
                cl.emb = embs[u]
            us = sorted(chosen, key=lambda u: -chosen[u].score)
            for a_i, ua in enumerate(us):
                for ub in us[a_i + 1:]:
                    if (u_key := (ub, chosen[ub].hit.item_id, chosen[ub].t0)) in self.banned:
                        continue
                    if float(embs[ua] @ embs[ub]) > self.same_voice_thr:
                        self.banned.add(u_key)
                        bad += 1
            missing = [u for u in range(len(units)) if u not in chosen]
            log(f"  ronde {rnd+1}: {len(chosen)} gecast, {bad} afgekeurd, {len(missing)} zonder kandidaat")
            if bad == 0:
                return chosen
        return {u: cl for u, cl in chosen.items() if (u, cl.hit.item_id, cl.t0) not in self.banned}

    def save(self, path: Path, chosen: dict[int, Clip]):
        with open(path, "wb") as fh:
            pickle.dump(chosen, fh)
