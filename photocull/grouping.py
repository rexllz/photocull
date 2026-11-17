"""Turn indexed files into shots, then group shots that are duplicates or look alike."""
import hashlib
import os
import re

from .index import JPEG_EXTS, RAW_EXTS

COPY_NAME = re.compile(r"( copy( \d+)?|\(\d+\)|-\d+)$", re.IGNORECASE)
BANDS = 4          # 64-bit hash split into 4 x 16 bits
MAX_BUCKET = 400   # skip huge buckets (e.g. hundreds of black frames)


def hamming(a, b):
    return bin(a ^ b).count("1")


def build_shots(rows):
    """One shot per picture. A RAW and its same-named JPEG in one folder are one shot."""
    buckets = {}
    for row in rows:
        folder, name = os.path.split(row["path"])
        stem, ext = os.path.splitext(name)
        row["ext"] = ext.lower()
        buckets.setdefault((folder, stem.lower()), []).append(row)

    shots = []
    for files in buckets.values():
        exts = {f["ext"] for f in files}
        paired = len(files) > 1 and exts & RAW_EXTS and exts <= (RAW_EXTS | JPEG_EXTS)
        for members in ([files] if paired else [[f] for f in files]):
            shots.append(_shot(members))
    return shots


def _shot(files):
    files = sorted(files, key=lambda f: (f["ext"] in RAW_EXTS, f["path"]))
    rep = files[0]  # the JPEG when there is one: faster to preview
    stem = os.path.splitext(os.path.basename(rep["path"]))[0]
    return {
        "id": hashlib.sha1(rep["path"].encode()).hexdigest()[:12],
        "files": files,
        "rep": rep,
        "name": os.path.basename(rep["path"]),
        "folder": os.path.dirname(rep["path"]),
        "video": rep["kind"] == "video",
        "has_raw": any(f["ext"] in RAW_EXTS for f in files),
        "pixels": max((f["w"] or 0) * (f["h"] or 0) for f in files),
        "size": sum(f["size"] for f in files),
        "taken": rep["taken"],
        "dhash": int(rep["dhash"], 16) if rep["dhash"] else None,
        "sig": (rep["size"], rep["qhash"]) if rep["qhash"] else None,
        "copy_name": bool(COPY_NAME.search(stem)),
    }


def rank(shot):
    """Sort key, best shot first: RAW kept, most pixels, biggest file, original name."""
    return (not shot["has_raw"], -shot["pixels"], -shot["size"],
            shot["copy_name"], len(shot["rep"]["path"]), shot["rep"]["path"])


class _Union:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def group_shots(shots, threshold=10, window=30, near=3):
    """Group shots that are byte-identical, near-identical, or similar and taken together.

    threshold: max differing hash bits (of 64) for shots taken within `window` seconds.
    near:      max differing bits for shots taken at any time (re-saved / resized copies).
    """
    u = _Union(len(shots))

    # 1. Identical content.
    by_sig = {}
    for i, s in enumerate(shots):
        if s["sig"]:
            by_sig.setdefault(s["sig"], []).append(i)
    for same in by_sig.values():
        for i in same[1:]:
            u.join(same[0], i)

    hashed = [i for i, s in enumerate(shots) if s["dhash"] is not None]

    # 2. Near-identical at any time. If two 64-bit hashes differ in <= 3 bits, at
    #    least one of their four 16-bit bands is equal, so only compare within bands.
    if near < BANDS:
        for band in range(BANDS):
            buckets = {}
            for i in hashed:
                key = (shots[i]["video"], (shots[i]["dhash"] >> (16 * band)) & 0xFFFF)
                buckets.setdefault(key, []).append(i)
            for members in buckets.values():
                if len(members) > MAX_BUCKET:
                    continue
                for x in range(len(members)):
                    for y in range(x + 1, len(members)):
                        a, b = members[x], members[y]
                        if hamming(shots[a]["dhash"], shots[b]["dhash"]) <= near:
                            u.join(a, b)

    # 3. Similar and taken close together (bursts, retakes of the same scene).
    ordered = sorted(hashed, key=lambda i: shots[i]["taken"])
    for x, a in enumerate(ordered):
        for b in ordered[x + 1:]:
            if shots[b]["taken"] - shots[a]["taken"] > window:
                break
            if shots[a]["video"] == shots[b]["video"] and \
                    hamming(shots[a]["dhash"], shots[b]["dhash"]) <= threshold:
                u.join(a, b)

    components = {}
    for i in range(len(shots)):
        components.setdefault(u.find(i), []).append(shots[i])

    groups = []
    for members in components.values():
        if len(members) < 2:
            continue
        members.sort(key=rank)
        sigs = {s["sig"] for s in members}
        exact = len(sigs) == 1 and None not in sigs
        # Suggest trashing only byte-identical extras; similar shots are the user's call.
        kept = set()
        for s in members:
            s["identical"] = s["sig"] is not None and sum(m["sig"] == s["sig"] for m in members) > 1
            s["suggest_trash"] = s["sig"] is not None and s["sig"] in kept
            if s["sig"]:
                kept.add(s["sig"])
        groups.append({"kind": "exact" if exact else "similar", "shots": members,
                       "taken": min(s["taken"] for s in members)})
    groups.sort(key=lambda g: (g["kind"] != "exact", g["taken"]))
    for n, g in enumerate(groups):
        g["id"] = n
    return groups
