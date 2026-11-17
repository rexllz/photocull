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


if __name__ == "__main__":
    unittest.main()
