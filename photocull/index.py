"""Scan a folder of photos/videos and cache a thumbnail + fingerprint per file.

Everything here uses tools that ship with macOS (`sips`, `qlmanage`), so there
is nothing to install and Nikon RAW (.NEF) files are decoded by the system.
"""
import hashlib
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAW_EXTS = {".nef", ".nrw"}
JPEG_EXTS = {".jpg", ".jpeg"}
IMAGE_EXTS = RAW_EXTS | JPEG_EXTS | {".heic", ".heif", ".png", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi"}

THUMB_PX = 480
QHASH_CHUNK = 1 << 20  # 1 MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path  TEXT PRIMARY KEY,
    size  INTEGER NOT NULL,
    mtime REAL NOT NULL,
    taken REAL NOT NULL,
    w     INTEGER,
    h     INTEGER,
    dhash TEXT,
    qhash TEXT,
    thumb TEXT,
    kind  TEXT NOT NULL,
    color TEXT
)
"""
COLUMNS = ("path", "size", "mtime", "taken", "w", "h", "dhash", "qhash", "thumb", "kind", "color")
HASH_SIDE = 16                 # 16x16 comparisons -> 256-bit shape fingerprint
HASH_HEX = HASH_SIDE * HASH_SIDE // 4
COLOR_GRID = 4                 # 4x4 grid of average colours


def cache_dir(root):
    """Per-library cache, kept out of the photo folder itself."""
    key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    d = Path.home() / "Library" / "Caches" / "photocull" / key
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    (d / "previews").mkdir(exist_ok=True)
    return d


def open_db(cache):
    db = sqlite3.connect(str(cache / "index.db"), check_same_thread=False)
    db.execute(SCHEMA)
    if "color" not in [c[1] for c in db.execute("PRAGMA table_info(files)")]:
        db.execute("ALTER TABLE files ADD COLUMN color TEXT")  # cache from an older version
    return db


def find_media(root):
    """Yield (path, kind) for every photo/video under root, skipping hidden files."""
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):  # includes ._ sidecars found on camera cards
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXTS:
                yield os.path.join(dirpath, name), "image"
            elif ext in VIDEO_EXTS:
                yield os.path.join(dirpath, name), "video"


def _run(cmd, timeout=180):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def make_jpeg(src, dest, max_px, quality=75):
    """Resize any image the system can read (incl. NEF) to a JPEG. True on success."""
    _run(["sips", "-Z", str(max_px), "-s", "format", "jpeg",
          "-s", "formatOptions", str(quality), src, "--out", dest])
    return os.path.exists(dest) and os.path.getsize(dest) > 0


def _video_thumb(src, dest):
    tmp = tempfile.mkdtemp(prefix="photocull-")
    try:
        _run(["qlmanage", "-t", "-s", str(THUMB_PX), "-o", tmp, src], timeout=60)
        png = os.path.join(tmp, os.path.basename(src) + ".png")
        return os.path.exists(png) and make_jpeg(png, dest, THUMB_PX)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _image_meta(src):
    """Capture time and pixel size from the file's own metadata."""
    taken = w = h = None
    out = _run(["sips", "-g", "creation", "-g", "pixelWidth", "-g", "pixelHeight", src],
               timeout=60).stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        key, _, val = line.strip().partition(": ")
        try:
            if key == "creation":
                taken = time.mktime(time.strptime(val.strip(), "%Y:%m:%d %H:%M:%S"))
            elif key == "pixelWidth":
                w = int(val)
            elif key == "pixelHeight":
                h = int(val)
        except ValueError:
            pass
    return taken, w, h


def bmp_pixels(data):
    """Rows of (r, g, b) from an uncompressed 24/32-bit BMP."""
    (offset,) = struct.unpack_from("<I", data, 10)
    w, h = struct.unpack_from("<ii", data, 18)
    (bpp,) = struct.unpack_from("<H", data, 28)
    step = bpp // 8
    stride = (w * step + 3) & ~3
    rows = []
    for y in range(abs(h)):
        base = offset + y * stride
        rows.append([(data[base + x * step + 2], data[base + x * step + 1], data[base + x * step])
                     for x in range(w)])
    if h > 0:  # positive height means the rows are stored bottom-up
        rows.reverse()
    return rows


def dhash_rows(rows):
    """Difference hash: one bit per horizontally adjacent pair of brightness values."""
    bits = 0
    for row in rows:
        for x in range(len(row) - 1):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def color_grid(pixels):
    """Average colour of each cell in a COLOR_GRID x COLOR_GRID grid, as a flat list."""
    cell = HASH_SIDE // COLOR_GRID
    out = []
    for gy in range(COLOR_GRID):
        for gx in range(COLOR_GRID):
            block = [pixels[y][x] for y in range(gy * cell, (gy + 1) * cell)
                     for x in range(gx * cell, (gx + 1) * cell)]
            out.extend(sum(p[c] for p in block) // len(block) for c in range(3))
    return out


def fingerprint(image):
    """(shape hash, colour grid) as hex strings, or (None, None) if unreadable.

    The shape hash says where the picture gets brighter or darker; the colour grid
    says what colours sit where. Two shots must agree on both to count as similar.
    """
    fd, tmp = tempfile.mkstemp(suffix=".bmp", prefix="photocull-")
    os.close(fd)
    try:
        _run(["sips", "-z", str(HASH_SIDE), str(HASH_SIDE + 1), "-s", "format", "bmp",
              image, "--out", tmp], timeout=60)
        with open(tmp, "rb") as f:
            data = f.read()
        if len(data) < 54:
            return None, None
        pixels = bmp_pixels(data)
        if len(pixels) == HASH_SIDE + 1:  # rotated images come back as 16 wide x 17 high
            pixels = [list(col) for col in zip(*pixels)]
        if len(pixels) != HASH_SIDE or len(pixels[0]) != HASH_SIDE + 1:
            return None, None
        gray = [[r * 299 + g * 587 + b * 114 for r, g, b in row] for row in pixels]
        return ("%0*x" % (HASH_HEX, dhash_rows(gray)),
                "".join("%02x" % v for v in color_grid(pixels)))
    except (OSError, struct.error, IndexError):
        return None, None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def quick_hash(path, size):
    """Content hash. Whole file when small, else size + start/middle/end samples."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        if size <= 4 * QHASH_CHUNK:
            for block in iter(lambda: f.read(QHASH_CHUNK), b""):
                h.update(block)
        else:
            for pos in (0, size // 2, size - QHASH_CHUNK):
                f.seek(pos)
                h.update(f.read(QHASH_CHUNK))
    return h.hexdigest()


def _process(path, kind, st, thumbs):
    thumb = str(thumbs / (hashlib.sha1(path.encode()).hexdigest() + ".jpg"))
    taken = w = h = None
    try:
        if kind == "image":
            ok = make_jpeg(path, thumb, THUMB_PX)
            taken, w, h = _image_meta(path)
        else:
            ok = _video_thumb(path, thumb)
        dhash, color = fingerprint(thumb) if ok else (None, None)
    except (subprocess.TimeoutExpired, OSError):
        ok, dhash, color = False, None, None
    return {"path": path, "size": st.st_size, "mtime": st.st_mtime,
            "taken": taken or st.st_mtime, "w": w, "h": h, "dhash": dhash,
            "qhash": None, "thumb": thumb if ok else None, "kind": kind, "color": color}


def _refingerprint(row):
    """Upgrade a cached row made by an older version, reusing its thumbnail."""
    try:
        row["dhash"], row["color"] = fingerprint(row["thumb"])
    except (subprocess.TimeoutExpired, OSError):
        row["dhash"], row["color"] = None, None
    return row


def _save(db, row):
    db.execute("INSERT OR REPLACE INTO files VALUES (%s)" % ",".join("?" * len(COLUMNS)),
               [row[c] for c in COLUMNS])


def forget(db, paths):
    """Drop files (and their thumbnails) from the cache."""
    for p in paths:
        for (thumb,) in db.execute("SELECT thumb FROM files WHERE path=?", (p,)):
            if thumb:
                try:
                    os.unlink(thumb)
                except OSError:
                    pass
        db.execute("DELETE FROM files WHERE path=?", (p,))
    db.commit()


def scan(root, db, cache, progress=None, rescan=False):
    """Index every media file under root. Unchanged files are reused from the cache."""
    cached = {}
    if not rescan:
        for values in db.execute("SELECT %s FROM files" % ",".join(COLUMNS)):
            row = dict(zip(COLUMNS, values))
            cached[row["path"]] = row

    rows, todo, stale, seen = [], [], [], set()
    for path, kind in find_media(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        seen.add(path)
        old = cached.get(path)
        fresh = old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1
        if fresh and old["thumb"] is None:
            rows.append(old)
        elif fresh and os.path.exists(old["thumb"]):
            current = old["dhash"] and len(old["dhash"]) == HASH_HEX and old["color"]
            (rows if current else stale).append(old)
        else:
            todo.append((path, kind, st))

    forget(db, [p for p in cached if p not in seen])

    thumbs = cache / "thumbs"
    done, total = 0, len(todo) + len(stale)

    def work(job):
        return _refingerprint(job) if isinstance(job, dict) else _process(job[0], job[1], job[2], thumbs)

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        for row in pool.map(work, stale + todo):
            _save(db, row)
            rows.append(row)
            done += 1
            if done % 25 == 0:
                db.commit()
            if progress:
                progress(done, total)
    db.commit()
    return rows


def fill_qhashes(rows, db):
    """Content-hash only files whose byte size collides with another file's."""
    by_size = {}
    for row in rows:
        by_size.setdefault(row["size"], []).append(row)
    for size, same in by_size.items():
        if len(same) < 2:
            continue
        for row in same:
            if row["qhash"]:
                continue
            try:
                row["qhash"] = quick_hash(row["path"], size)
            except OSError:
                continue
            db.execute("UPDATE files SET qhash=? WHERE path=?", (row["qhash"], row["path"]))
    db.commit()
