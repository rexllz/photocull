"""Local-only web server behind the review page."""
import json
import mimetypes
import os
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import index
from .trash import move_to_trash

WEB = Path(__file__).parent / "web"
PREVIEW_PX = 2400


class App:
    def __init__(self, root, groups, db, cache):
        self.root = str(root)
        self.groups = groups
        self.db = db
        self.cache = cache
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.shots = {s["id"]: s for g in groups for s in g["shots"]}

    def payload(self):
        return {"root": self.root, "groups": [
            {"id": g["id"], "kind": g["kind"], "taken": g["taken"], "shots": [
                {"id": s["id"], "name": s["name"],
                 "folder": os.path.relpath(s["folder"], self.root),
                 "video": s["video"], "raw": s["has_raw"], "size": s["size"],
                 "w": s["rep"]["w"], "h": s["rep"]["h"], "taken": s["taken"],
                 "files": len(s["files"]), "identical": s["identical"],
                 "suggest": s["suggest_trash"], "thumb": bool(s["rep"]["thumb"])}
                for s in g["shots"]]}
            for g in self.groups]}

    def trash(self, ids):
        with self.lock:
            shots = [self.shots[i] for i in ids if i in self.shots]
            paths = [f["path"] for s in shots for f in s["files"]]
            failed = set(move_to_trash(paths))
            index.forget(self.db, [p for p in paths if p not in failed])
            gone = {s["id"] for s in shots if not any(f["path"] in failed for f in s["files"])}
            for i in gone:
                del self.shots[i]
            for g in self.groups:
                g["shots"] = [s for s in g["shots"] if s["id"] not in gone]
            self.groups = [g for g in self.groups if len(g["shots"]) > 1]
            return {"trashed": len(gone), "files": len(paths) - len(failed),
                    "failed": sorted(failed)}

    def preview(self, shot):
        """Large JPEG for the lightbox, made on first view (decodes NEF via the system)."""
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

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
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
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except ValueError:
                return self.send_error(400)
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
