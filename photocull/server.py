"""Local-only web server: the whole app (pick folder, scan, review, trash) lives in the page."""
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import grouping, index
from .trash import move_to_trash

WEB = Path(__file__).parent / "web"
RECENT = Path.home() / "Library" / "Caches" / "photocull" / "recent.json"
PREVIEW_PX = 2400
MAX_BODY = 1 << 20   # largest request body accepted (a list of shot ids is far smaller)
# Sent with every response: no framing by other sites, no outside resources, no sniffing.
SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy",
     "default-src 'none'; img-src 'self'; media-src 'self'; connect-src 'self'; "
     "style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"),
)


def same(a, b):
    """Constant-time comparison that tolerates odd characters in a header."""
    return secrets.compare_digest((a or "").encode("utf-8", "replace"), (b or "").encode("utf-8"))

DENIED = b"""<!doctype html><meta charset="utf-8"><title>photocull</title>
<body style="font:16px -apple-system,sans-serif;background:#0e1012;color:#eceef1;padding:60px;text-align:center">
<h2>This link is missing its key or has expired</h2>
<p style="color:#99a1ab">__HINT__</p>"""

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


_QR = """
ObjC.import("AppKit"); ObjC.import("CoreImage");
function run(argv) {
  var data = $.NSString.alloc.initWithUTF8String(argv[0]).dataUsingEncoding($.NSUTF8StringEncoding);
  var f = $.CIFilter.filterWithName("CIQRCodeGenerator");
  f.setValueForKey(data, "inputMessage"); f.setValueForKey("M", "inputCorrectionLevel");
  var rep = $.NSBitmapImageRep.alloc.initWithCIImage(f.outputImage);
  var png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary);
  return png.writeToFileAtomically(argv[1], true) ? "ok" : "fail";
}
"""


def qr_png(text):
    """PNG bytes of a QR code (one pixel per module), made by macOS's own generator."""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="photocull-qr-")
    os.close(fd)
    try:
        subprocess.run(["osascript", "-l", "JavaScript", "-e", _QR, text, path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def lan_address():
    """This Mac's address on the local network, or None when it has none."""
    for interface in ("en0", "en1"):  # Wi-Fi first, then wired
        out = subprocess.run(["ipconfig", "getifaddr", interface], stdout=subprocess.PIPE).stdout
        if out.strip():
            return out.decode().strip()
    try:  # whichever interface the default route uses; connecting a UDP socket sends nothing
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
        probe.close()
        return None if address.startswith("127.") else address
    except OSError:
        return None


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
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.threshold, self.window = threshold, window
        self.status = "idle"  # idle | scanning | ready | error
        self.root = self.error = self.db = self.cache = None
        self.done = self.total = 0
        self.phase = ""
        self.rows, self.groups, self.shots, self.singles = [], [], {}, []
        # Phone access: off until switched on from the Mac. It gets its own key and
        # its own listener on the network address, both discarded when switched off.
        self.handler = self.port = None
        self.phone_token = self.phone_server = self.phone_ip = self.phone_qr = None

    # ---- phone access ---------------------------------------------------
    def phone_url(self):
        if not self.phone_token:
            return None
        return "http://%s:%d/?k=%s" % (self.phone_ip, self.port, self.phone_token)

    def phone_on(self):
        """Start answering on the local network. Returns an error message or None."""
        with self.lock:
            if self.phone_token:
                return None
            ip = lan_address()
            if not ip:
                return "This Mac is not connected to a network."
            try:
                server = ThreadingHTTPServer((ip, self.port), self.handler)
            except OSError as exc:
                return "Could not open the network port: %s" % exc
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.phone_server, self.phone_ip = server, ip
            self.phone_token = secrets.token_urlsafe(24)
            self.phone_qr = qr_png(self.phone_url())
            return None

    def phone_off(self):
        with self.lock:
            server, self.phone_server = self.phone_server, None
            self.phone_token = self.phone_ip = self.phone_qr = None
        if server:
            server.shutdown()
            server.server_close()

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
                "videos": index.INCLUDE_VIDEOS, "phone": bool(self.phone_token),
                "recent": recent_folders()}

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
        timeout = 60  # drop connections that go quiet, so idle sockets cannot pile up

        def log_message(self, *args):
            pass

        def end_headers(self):
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            super().end_headers()

        def _remote(self):
            """True for a request from another device (the phone), false for this Mac."""
            return self.client_address[0] not in ("127.0.0.1", "::1")

        def _expected_key(self):
            # The Mac's key only works from the Mac; the phone key only from the network.
            return app.phone_token if self._remote() else app.token

        def _local(self):
            """Reject requests addressed to any other host name (DNS rebinding)."""
            host = (self.headers.get("Host") or "").split(":")[0]
            if self._remote():
                return bool(app.phone_ip) and host == app.phone_ip
            return host in ("127.0.0.1", "localhost")

        def _cookie_name(self):
            return "photocull_%d" % self.server.server_address[1]

        def _authed(self):
            """Every request must carry the session key, so neither other users of this
            Mac nor other devices on the network can see or trash anything."""
            expected = self._expected_key()
            if not expected:
                return False
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == self._cookie_name() and same(value, expected):
                    return True
            return False

        def _state(self):
            state = app.state()
            state["remote"] = self._remote()
            if self._remote():
                state["recent"] = []   # the phone has no use for other folder paths on the Mac
            else:
                state["phone_url"] = app.phone_url()
            return state

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
            parsed = urlsplit(self.path)
            path = parsed.path
            key = parse_qs(parsed.query).get("k", [""])[0]
            expected = self._expected_key()
            signing_in = bool(path == "/" and key and expected and same(key, expected))
            if not signing_in and not self._authed():
                hint = (b"On the Mac, click Phone and scan the QR code again." if self._remote()
                        else b"Open the link printed in the terminal where photocull is running.")
                return self._send(DENIED.replace(b"__HINT__", hint), "text/html; charset=utf-8", 403)
            if path == "/":
                # A link with the right key gets the page straight away plus a cookie for
                # everything after it (the page then drops the key from the address bar).
                # No redirect: a phone opening the link from a QR scanner app would not
                # send a just-set cookie on the redirected request.
                body = (WEB / "index.html").read_text().replace("__TOKEN__", expected).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if signing_in:
                    self.send_header("Set-Cookie", "%s=%s; HttpOnly; SameSite=Lax; Path=/"
                                     % (self._cookie_name(), expected))
                self.end_headers()
                return self.wfile.write(body)
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/phone/qr.png" and not self._remote() and app.phone_qr:
                return self._send(app.phone_qr, "image/png")
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
            if not self._local() or not self._authed() or \
                    not same(self.headers.get("X-Token"), self._expected_key()):
                return self.send_error(403)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= MAX_BODY:
                    return self.send_error(413)
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.send_error(400)
            if not isinstance(data, dict):
                return self.send_error(400)
            if self._remote():
                # The phone may review and trash within the folder already open. Choosing
                # folders, Finder and the phone switch itself stay on the Mac.
                reopen = self.path == "/api/open" and data.get("path") == app.root
                if self.path not in ("/api/settings", "/api/trash") and not reopen:
                    return self.send_error(403)
            if self.path == "/api/phone":
                error = app.phone_on() if data.get("on") else app.phone_off()
                return self._json({"error": error, "on": bool(app.phone_token), "url": app.phone_url()})
            if self.path == "/api/pick":
                return self._json({"path": pick_folder()})
            if self.path == "/api/open":
                return self._json({"error": app.open(data.get("path"), bool(data.get("rescan")))})
            if self.path == "/api/settings":
                try:
                    app.regroup(data.get("threshold", app.threshold), data.get("window", app.window))
                except (TypeError, ValueError):
                    return self.send_error(400)
                return self._json(self._state())
            if self.path == "/api/trash":
                ids = data.get("ids")
                if not isinstance(ids, list):
                    return self.send_error(400)
                return self._json(app.trash([i for i in ids if isinstance(i, str)]))
            if self.path == "/api/reveal":
                shot = app.shots.get(data.get("id"))
                if shot:
                    subprocess.run(["open", "-R", shot["rep"]["path"]])
                return self._json({"ok": bool(shot)})
            self.send_error(404)

    return Handler


def serve(app, port):
    app.handler = make_handler(app)
    server = ThreadingHTTPServer(("127.0.0.1", port), app.handler)
    server.daemon_threads = True
    app.port = server.server_address[1]
    return server
