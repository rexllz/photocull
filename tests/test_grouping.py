import unittest

from photocull.grouping import build_shots, group_shots
from photocull.index import dhash_rows


def row(path, dhash, taken=0.0, size=1000, qhash=None, w=6000, h=4000, kind="image", color=100):
    return {"path": path, "size": size, "mtime": taken, "taken": taken, "w": w, "h": h,
            "dhash": "%064x" % dhash, "qhash": qhash, "thumb": None, "kind": kind,
            "color": "%02x" % color * 48}


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

    def test_photo_and_video_never_group(self):
        shots = build_shots([row("/a/1.JPG", A, taken=1), row("/a/2.MOV", A, taken=2, kind="video")])
        self.assertEqual(group_shots(shots), [])


if __name__ == "__main__":
    unittest.main()
