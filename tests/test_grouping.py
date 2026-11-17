import json
import unittest

from photocull.grouping import build_shots, group_shots
from photocull.index import dhash_rows


def row(path, dhash, taken=0.0, size=1000, qhash=None, w=6000, h=4000, kind="image", color=100,
        quality=None):
    return {"path": path, "size": size, "mtime": taken, "taken": taken, "w": w, "h": h,
            "dhash": "%064x" % dhash, "qhash": qhash, "thumb": None, "kind": kind,
            "color": "%02x" % color * 48, "quality": json.dumps(quality) if quality else None}


def quality(sharp=1000, roll=0.0, line=0.9, blown=0.0, mean=120, subject="salient"):
    return {"v": 1, "sharp": sharp, "sharp_top": sharp, "subject": subject, "roll": roll,
            "roll_length": line, "blown": blown, "crushed": 0.0, "mean": mean}


A = int("0f" * 32, 16)
B = int("f0" * 32, 16)  # every bit differs from A


def names(groups):
    return [(g["kind"], sorted(s["name"] for s in g["shots"])) for g in groups]


class GroupingTest(unittest.TestCase):
    def test_dhash_bits(self):
        self.assertEqual(dhash_rows([[2, 1, 1, 3]]), 0b100)

    def test_raw_and_jpeg_are_one_shot(self):
        shots = build_shots([row("/p/DSC_1.NEF", A), row("/p/DSC_1.JPG", A), row("/p/DSC_2.JPG", B, color=200)])
        self.assertEqual(sorted(len(s["files"]) for s in shots), [1, 2])
        self.assertEqual(group_shots(shots), [])

    def test_identical_copies_suggest_trashing_the_extra(self):
        shots = build_shots([row("/a/DSC_1.JPG", A, qhash="x"), row("/b/DSC_1 copy.JPG", A, qhash="x"),
                             row("/a/DSC_2.JPG", B, taken=5000)])
        (g,) = group_shots(shots)
        self.assertEqual(g["kind"], "exact")
        self.assertEqual([(s["folder"], s["suggest_trash"]) for s in g["shots"]],
                         [("/a", False), ("/b", True)])

    def test_similar_only_when_taken_together(self):
        near = A ^ (2 ** 30 - 1)  # 30 of 256 bits differ
        together = build_shots([row("/a/1.JPG", A, taken=100), row("/a/2.JPG", near, taken=110)])
        apart = build_shots([row("/a/1.JPG", A, taken=100), row("/a/2.JPG", near, taken=9000)])
        self.assertEqual(names(group_shots(together)), [("similar", ["1.JPG", "2.JPG"])])
        self.assertEqual(group_shots(apart), [])

    def test_resaved_copy_matches_at_any_time(self):
        shots = build_shots([row("/a/1.JPG", A, taken=100), row("/b/1s.JPG", A ^ 0b11, taken=9e6, w=800, h=600)])
        (g,) = group_shots(shots)
        self.assertEqual([s["name"] for s in g["shots"]], ["1.JPG", "1s.JPG"])  # bigger one ranked best
        self.assertFalse(any(s["suggest_trash"] for s in g["shots"]))

    def test_same_shape_but_different_colours_do_not_group(self):
        shots = build_shots([row("/a/1.JPG", A, taken=1, color=40), row("/a/2.JPG", A, taken=2, color=160)])
        self.assertEqual(group_shots(shots), [])

    def test_gradual_drift_does_not_chain_into_one_group(self):
        # Each shot is close to the previous one, but the last is far from the first.
        colors = [100, 112, 124, 136, 148]
        shots = build_shots([row("/a/%d.JPG" % i, B if i % 2 else A, taken=i, color=c)
                             for i, c in enumerate(colors)])
        groups = group_shots(shots)
        self.assertTrue(groups and all(len(g["shots"]) < 5 for g in groups))

    def test_moved_subject_in_same_scene_groups(self):
        # Shapes disagree completely (the subject moved) but colours sit in the same places.
        shots = build_shots([row("/a/1.JPG", A, taken=1, color=100), row("/a/2.JPG", B, taken=4, color=104)])
        self.assertEqual(names(group_shots(shots)), [("similar", ["1.JPG", "2.JPG"])])

    def best(self, **shots):
        rows = [row("/a/%s.JPG" % name, A, taken=i, size=1000 + i, quality=q)
                for i, (name, q) in enumerate(sorted(shots.items()))]
        (g,) = group_shots(build_shots(rows))
        return g["shots"][0]["name"][0], {s["name"][0]: s["score"] for s in g["shots"]}

    def test_sharper_shot_is_best(self):
        best, scores = self.best(a=quality(sharp=300), b=quality(sharp=1200))
        self.assertEqual(best, "b")
        self.assertEqual(scores["b"]["focus"], 100)
        self.assertEqual(scores["a"]["focus"], 50)

    def test_level_shot_beats_slightly_sharper_tilted_one(self):
        best, scores = self.best(a=quality(sharp=1000, roll=-4.0), b=quality(sharp=900, roll=0.3))
        self.assertEqual(best, "b")
        self.assertEqual(scores["a"]["tilt"], 4.0)

    def test_steep_or_short_lines_are_not_treated_as_tilt(self):
        _, scores = self.best(a=quality(roll=7.9), b=quality(roll=3.0, line=0.2))
        self.assertIsNone(scores["a"]["tilt"])
        self.assertIsNone(scores["b"]["tilt"])

    def test_clearly_overexposed_shot_is_never_best(self):
        best, scores = self.best(a=quality(sharp=5000, blown=0.4), b=quality(sharp=500))
        self.assertEqual(best, "b")
        self.assertEqual(scores["a"]["flag"], "over")

    def test_photo_and_video_never_group(self):
        shots = build_shots([row("/a/1.JPG", A, taken=1), row("/a/2.MOV", A, taken=2, kind="video")])
        self.assertEqual(group_shots(shots), [])


class VideoSafetyTest(unittest.TestCase):
    def test_videos_are_not_scanned(self):
        import os
        import tempfile
        from photocull import index
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.JPG", "b.MOV", "c.mp4"):
                open(os.path.join(d, name), "w").close()
            found = sorted(os.path.basename(p) for p, _ in index.find_media(d))
        self.assertFalse(index.INCLUDE_VIDEOS)
        self.assertEqual(found, ["a.JPG"])

    def test_trash_refuses_videos(self):
        from unittest import mock
        from photocull import server
        app = server.App()
        video = {"id": "v", "video": True, "files": [{"path": "/x/clip.MOV", "kind": "video"}]}
        app.shots = {"v": video}
        with mock.patch.object(server, "move_to_trash") as mover, \
                mock.patch.object(server.index, "forget"), mock.patch.object(app, "_group"):
            mover.return_value = []
            result = app.trash(["v"])
        mover.assert_called_once_with([])
        self.assertEqual(result["files"], 0)


class AccessTest(unittest.TestCase):
    def setUp(self):
        import threading
        from photocull import server
        self.app = server.App()
        self.httpd = server.serve(self.app, 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, headers=None, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    def test_page_and_data_need_the_key(self):
        self.assertEqual(self.request("GET", "/").status, 403)
        self.assertEqual(self.request("GET", "/api/groups").status, 403)
        self.assertEqual(self.request("GET", "/?k=wrong").status, 403)

    def test_key_link_signs_in_with_a_cookie(self):
        resp = self.request("GET", "/?k=" + self.app.token)
        self.assertEqual(resp.status, 200)   # the page itself, no redirect to lose the cookie on
        self.assertIn("SameSite=Lax", resp.getheader("Set-Cookie"))
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", resp.getheader("Set-Cookie"))
        self.assertEqual(self.request("GET", "/", {"Cookie": cookie}).status, 200)
        self.assertEqual(self.request("GET", "/api/state", {"Cookie": cookie}).status, 200)

    def test_responses_forbid_framing_and_outside_resources(self):
        for path in ("/", "/?k=" + self.app.token):
            resp = self.request("GET", path)
            self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
            self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))

    def test_malformed_requests_are_rejected(self):
        cookie = "photocull_%d=%s" % (self.port, self.app.token)
        headers = {"Cookie": cookie, "X-Token": self.app.token}
        post = lambda path, body: self.request("POST", path, headers, body=body).status
        self.assertEqual(post("/api/trash", '{"ids": "abc"}'), 400)
        self.assertEqual(post("/api/trash", '[1, 2]'), 400)
        self.assertEqual(post("/api/trash", "not json"), 400)
        self.assertEqual(self.request("GET", "/", {"Cookie": "photocull_%d=\xff\xfe" % self.port}).status, 403)

    def test_changes_need_cookie_and_token_and_a_local_host(self):
        cookie = "photocull_%d=%s" % (self.port, self.app.token)
        post = lambda h: self.request("POST", "/api/settings", h, body="{}").status
        self.assertEqual(post({"Cookie": cookie}), 403)
        self.assertEqual(post({"X-Token": self.app.token}), 403)
        self.assertEqual(post({"Cookie": cookie, "X-Token": self.app.token}), 200)
        self.assertEqual(post({"Cookie": cookie, "X-Token": self.app.token, "Host": "evil.example"}), 403)


class PhoneAccessTest(unittest.TestCase):
    """Phone access, exercised by calling this Mac's own network address."""

    def setUp(self):
        import threading
        from unittest import mock
        from photocull import server
        self.server = server
        self.LAN = server.lan_address()
        if not self.LAN:
            self.skipTest("no network connection")
        self.app = server.App()
        self.app.root = "/photos"
        self.httpd = server.serve(self.app, 0)
        self.port = self.app.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.patches = [mock.patch.object(server, "qr_png", return_value=b"png")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        self.app.phone_off()
        for p in self.patches:
            p.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, host, method, path, key=None, body=None):
        import http.client
        conn = http.client.HTTPConnection(host, self.port, timeout=5)
        headers = {}
        if key:
            headers = {"Cookie": "photocull_%d=%s" % (self.port, key), "X-Token": key}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def phone(self, method, path, key=None, body=None):
        return self.request(self.LAN, method, path, key, body)

    def test_network_is_closed_until_switched_on(self):
        with self.assertRaises(OSError):
            self.phone("GET", "/")

    def test_phone_gets_in_only_with_the_phone_key(self):
        self.assertIsNone(self.app.phone_on())
        self.assertEqual(self.phone("GET", "/api/state"), 403)
        self.assertEqual(self.phone("GET", "/api/state", self.app.token), 403)       # Mac key is Mac-only
        self.assertEqual(self.phone("GET", "/api/state", self.app.phone_token), 200)
        self.assertEqual(self.request("127.0.0.1", "GET", "/api/state", self.app.phone_token), 403)

    def test_phone_cannot_choose_folders_or_change_phone_access(self):
        self.app.phone_on()
        key = self.app.phone_token
        self.assertEqual(self.phone("POST", "/api/open", key, '{"path": "/etc"}'), 403)
        self.assertEqual(self.phone("POST", "/api/pick", key, "{}"), 403)
        self.assertEqual(self.phone("POST", "/api/reveal", key, "{}"), 403)
        self.assertEqual(self.phone("POST", "/api/phone", key, '{"on": false}'), 403)
        self.assertEqual(self.phone("GET", "/api/phone/qr.png", key), 404)
        self.assertEqual(self.phone("POST", "/api/settings", key, "{}"), 200)

    def test_switching_off_revokes_the_key_and_closes_the_port(self):
        self.app.phone_on()
        old = self.app.phone_token
        self.app.phone_off()
        with self.assertRaises(OSError):
            self.phone("GET", "/api/state", old)
        self.app.phone_on()
        self.assertNotEqual(self.app.phone_token, old)
        self.assertEqual(self.phone("GET", "/api/state", old), 403)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        import threading
        from photocull import server
        self.dir = tempfile.TemporaryDirectory()
        self.app = server.App()
        self.app.root = self.dir.name
        os.mkdir(os.path.join(self.dir.name, "day 2"))
        for i, rel in enumerate(["DSC_0001.JPG", os.path.join("day 2", "DSC_0001.JPG")]):
            path = os.path.join(self.dir.name, rel)
            with open(path, "wb") as f:
                f.write(b"photo-%d" % i * 1000)
            self.app.shots["s%d" % i] = {"id": "s%d" % i, "video": False,
                                         "rep": {"path": path, "thumb": None, "size": 7000}}
        self.httpd = server.serve(self.app, 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()

    def get(self, path, signed_in=True):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        cookie = {"Cookie": "photocull_%d=%s" % (self.app.port, self.app.token)} if signed_in else {}
        conn.request("GET", path, headers=cookie)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_single_download_is_the_original_as_an_attachment(self):
        resp, body = self.get("/download/s0")
        self.assertEqual(resp.status, 200)
        self.assertIn('attachment; filename="DSC_0001.JPG"', resp.getheader("Content-Disposition"))
        self.assertEqual(body, b"photo-0" * 1000)

    def test_batch_download_is_a_zip_with_folder_paths(self):
        import io
        import zipfile
        resp, body = self.get("/download.zip?ids=s0,s1,s0,missing")
        self.assertEqual(resp.status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertEqual(sorted(z.namelist()), ["DSC_0001.JPG", "day 2/DSC_0001.JPG"])
            self.assertEqual(z.read("day 2/DSC_0001.JPG"), b"photo-1" * 1000)
            self.assertIsNone(z.testzip())

    def test_downloads_need_the_key(self):
        self.assertEqual(self.get("/download/s0", signed_in=False)[0].status, 403)
        self.assertEqual(self.get("/download.zip?ids=s0", signed_in=False)[0].status, 403)
        self.assertEqual(self.get("/download.zip?ids=nope")[0].status, 404)


class SkipTest(unittest.TestCase):
    """'Not duplicates' moves a group's shots to Unique, in memory only."""

    def make_app(self):
        from photocull import server
        app = server.App()
        app.status, app.root = "ready", "/a"
        app.rows = [row("/a/1.JPG", A, taken=1), row("/a/2.JPG", A, taken=2),
                    row("/a/3.JPG", A, taken=3), row("/a/9.JPG", B, taken=900, color=200)]
        app._group()
        return app

    def test_skipping_a_group_moves_its_shots_to_unique(self):
        app = self.make_app()
        self.assertEqual(len(app.groups), 1)
        ids = [s["id"] for s in app.groups[0]["shots"]]
        app.skip(ids)
        self.assertEqual(app.groups, [])
        self.assertEqual(len(app.singles), 4)
        self.assertEqual(sum(s["skipped"] for s in app.payload()["singles"]), 3)

    def test_skipping_part_of_a_group_and_restoring(self):
        app = self.make_app()
        ids = [s["id"] for s in app.groups[0]["shots"]]
        app.skip(ids[:1])
        self.assertEqual(len(app.groups[0]["shots"]), 2)
        app.regroup(10, 30)                      # survives a settings change
        self.assertEqual(len(app.groups[0]["shots"]), 2)
        app.skip(None, False)
        self.assertEqual(len(app.groups[0]["shots"]), 3)
        self.assertEqual(app.state()["skipped"], 0)


class DemoScriptTest(unittest.TestCase):
    def test_refuses_to_replace_a_folder_it_did_not_make(self):
        import os
        import subprocess
        import sys
        import tempfile
        script = os.path.join(os.path.dirname(__file__), "..", "demo", "make_demo.py")
        with tempfile.TemporaryDirectory() as d:
            keep = os.path.join(d, "keep.txt")
            open(keep, "w").close()
            proc = subprocess.run([sys.executable, script, d], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(os.path.exists(keep))


if __name__ == "__main__":
    unittest.main()
