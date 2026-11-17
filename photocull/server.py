"""Local-only web server: the whole app (pick folder, scan, review, trash) lives in the page."""
import json
import mimetypes
import os
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import grouping, index
from .trash import move_to_trash

WEB = Path(__file__).parent / "web"
RECENT = Path.home() / "Library" / "Caches" / "photocull" / "recent.json"
PREVIEW_PX = 2400

_PICK = """
var app = Application.currentApplication();
app.includeStandardAdditions = true;
app.activate();
app.chooseFolder({withPrompt: "Choose the folder with your photos and videos"}).toString();
"""


def pick_folder():
    """Show the native macOS folder chooser. None if cancelled."""
    proc = subprocess.run(["osascript", "-l", "JavaScript", "-e", _PICK],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    path = proc.stdout.decode().strip()
    return path if proc.returncode == 0 and path else None


def recent_folders():
    try:
        return [p for p in json.loads(RECENT.read_text()) if os.path.isdir(p)]
    except (OSError, ValueError):
        return []


def remember_folder(path):
    paths = [path] + [p for p in recent_folders() if p != path]
    RECENT.parent.mkdir(parents=True, exist_ok=True)
    RECENT.write_text(json.dumps(paths[:6]))


class App:
    def __init__(self, threshold=10, window=30):
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.RLock()
        self.threshold, self.window = threshold, window
        self.status = "idle"  # idle | scanning | ready | error
        self.root = self.error = self.db = self.cache = None
        self.done = self.total = 0
        self.phase = ""
        self.rows, self.groups, self.shots, self.singles = [], [], {}, []

    # ---- scanning -------------------------------------------------------
    def open(self, folder, rescan=False):
        """Start scanning a folder in the background. Returns an error message or None."""
        root = Path(folder or "").expanduser()
        if not folder or not root.is_dir():
            return "That folder doesn't exist."
        with self.lock:
            if self.status == "scanning":
                return "A scan is already running."
            self.status, self.root, self.error = "scanning", str(root.resolve()), None
            self.done = self.total = 0
            self.phase = ""
            self.rows, self.groups, self.shots, self.singles = [], [], {}, []
        threading.Thread(target=self._scan, args=(rescan,), daemon=True).start()
        return None

    def _progress(self, done, total, phase=""):
        self.done, self.total, self.phase = done, total, phase

    def _scan(self, rescan):
        try:
            cache = index.cache_dir(self.root)
            db = index.open_db(cache)
            rows = index.scan(self.root, db, cache, self._progress, rescan=rescan)
            index.fill_qhashes(rows, db)
            with self.lock:
                self.db, self.cache, self.rows = db, cache, rows
                self._group()
                self.status = "ready"
            remember_folder(self.root)
        except Exception as exc:  # surface any failure in the page instead of dying silently
            self.status, self.error = "error", "Scan failed: %s" % exc

    def _group(self):
        shots = grouping.build_shots(self.rows)
        self.groups = grouping.group_shots(shots, threshold=self.threshold, window=self.window)
        self.shots = {s["id"]: s for s in shots}
        grouped = {s["id"] for g in self.groups for s in g["shots"]}
        self.singles = sorted((s for s in shots if s["id"] not in grouped), key=lambda s: s["taken"])
        for s in self.singles:
            s["identical"] = s["suggest_trash"] = False
            s["score"] = grouping.assess_alone(s)

    def regroup(self, threshold, window):
        with self.lock:
            self.threshold = max(2, min(16, int(threshold)))
            self.window = max(1, min(600, int(window)))
            if self.status == "ready":
                self._group()

    # ---- data for the page ----------------------------------------------
    def state(self):
        return {"status": self.status, "root": self.root, "error": self.error,
                "done": self.done, "total": self.total, "phase": self.phase, "files": len(self.rows),
                "unreadable": sum(1 for r in self.rows if not r["dhash"]),
                "threshold": self.threshold, "window": self.window,
                "videos": index.INCLUDE_VIDEOS, "recent": recent_folders()}

    def _shot_json(self, s):
        return {"id": s["id"], "name": s["name"],
                "folder": os.path.relpath(s["folder"], self.root),
                "video": s["video"], "raw": s["has_raw"], "size": s["size"],
                "w": s["rep"]["w"], "h": s["rep"]["h"], "taken": s["taken"],
                "files": len(s["files"]), "identical": s["identical"],
                "suggest": s["suggest_trash"], "thumb": bool(s["rep"]["thumb"]),
                "score": s.get("score")}

    def payload(self):
        """Groups to review, plus every shot that has no look-alike."""
        with self.lock:
            return {"groups": [{"id": g["id"], "kind": g["kind"], "taken": g["taken"],
                                "shots": [self._shot_json(s) for s in g["shots"]]}
                               for g in self.groups],
                    "singles": [self._shot_json(s) for s in self.singles]}

    # ---- actions --------------------------------------------------------
    def trash(self, ids):
        with self.lock:
            # Second line of defence: a video is never moved to the Trash from here.
            shots = [self.shots[i] for i in ids if i in self.shots and not self.shots[i]["video"]
                     and not any(f["kind"] == "video" for f in self.shots[i]["files"])]
            paths = [f["path"] for s in shots for f in s["files"]]
            failed = set(move_to_trash(paths))
            moved = {p for p in paths if p not in failed}
            index.forget(self.db, sorted(moved))
            self.rows = [r for r in self.rows if r["path"] not in moved]
            gone = sum(1 for s in shots if not any(f["path"] in failed for f in s["files"]))
            self._group()  # a group left with one shot becomes a single again
            return {"trashed": gone, "files": len(moved), "failed": sorted(failed)}

    def preview(self, shot):
        """Large JPEG for full-size viewing, made on first view (decodes NEF via the system)."""
        dest = str(self.cache / "previews" / (shot["id"] + ".jpg"))
        if not os.path.exists(dest):
            longest = max(shot["rep"]["w"] or 0, shot["rep"]["h"] or 0) or PREVIEW_PX
            if not index.make_jpeg(shot["rep"]["path"], dest, min(PREVIEW_PX, longest), quality=85):
                return shot["rep"]["thumb"]
        return dest


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _local(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, body, ctype, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(json.dumps(obj).encode(), "application/json", status)

        def _file(self, path, ctype=None):
            if not path or not os.path.exists(path):
                return self.send_error(404)
            size = os.path.getsize(path)
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):  # needed for video seeking
                a, _, b = rng[6:].split(",")[0].partition("-")
                try:
                    if a:
                        start, end = int(a), (int(b) if b else size - 1)
                    else:
                        start = max(0, size - int(b))
                except ValueError:
                    return self.send_error(400)
                end = min(end, size - 1)
                if start > end:
                    return self.send_error(416)
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0]
                             or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        block = f.read(min(262144, left))
                        if not block:
                            break
                        self.wfile.write(block)
                        left -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self._local():
                return self.send_error(403)
            path = self.path.split("?")[0]
            if path == "/":
                html = (WEB / "index.html").read_text().replace("__TOKEN__", app.token)
                return self._send(html.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(app.state())
            if path == "/api/groups":
                return self._json(app.payload())
            kind, _, shot_id = path.strip("/").partition("/")
            shot = app.shots.get(shot_id)
            if not shot:
                return self.send_error(404)
            if kind == "thumb":
                return self._file(shot["rep"]["thumb"], "image/jpeg")
            if kind == "preview":
                return self._file(app.preview(shot), "image/jpeg")
            if kind == "media":
                return self._file(shot["rep"]["path"])
            self.send_error(404)

        def do_POST(self):
            if not self._local() or self.headers.get("X-Token") != app.token:
                return self.send_error(403)
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.send_error(400)
            if self.path == "/api/pick":
                return self._json({"path": pick_folder()})
            if self.path == "/api/open":
                return self._json({"error": app.open(data.get("path"), bool(data.get("rescan")))})
            if self.path == "/api/settings":
                try:
                    app.regroup(data.get("threshold", app.threshold), data.get("window", app.window))
                except (TypeError, ValueError):
                    return self.send_error(400)
                return self._json(app.state())
            if self.path == "/api/trash":
                return self._json(app.trash(data.get("ids", [])))
            if self.path == "/api/reveal":
                shot = app.shots.get(data.get("id"))
                if shot:
                    subprocess.run(["open", "-R", shot["rep"]["path"]])
                return self._json({"ok": bool(shot)})
            self.send_error(404)

    return Handler


def serve(app, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server
