#!/usr/bin/env python3
"""Generate a small folder of synthetic "photos" to try photocull without your own.

Each scene is drawn from simple maths (no real pictures), then saved several times
the way a real burst goes wrong: one good frame, one tilted, one out of focus, and
sometimes one overexposed. A few one-off scenes and an exact copy are added so every
part of the app has something to show.

    python3 demo/make_demo.py            # writes ./demo-photos
    ./photocull.sh demo-photos

Needs only macOS (it uses the built-in `sips` to write JPEGs). A short demo video is
added too when the Swift compiler is available (Xcode command line tools).
"""
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

W, H = 960, 640
START = 1717236000  # fixed capture times, so results are the same on every run


def noise(x, y, seed=0):
    """Repeatable pseudo-random value 0..1 for a pixel: gives surfaces fine texture."""
    n = (x * 374761393 + y * 668265263 + seed * 982451653) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((n >> 16) & 0xFF) / 255.0


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def coast(u, v):
    if v < 0.55:
        c = mix((60, 130, 220), (175, 212, 245), v / 0.55)
        if (u - 0.76) ** 2 + ((v - 0.2) * 0.67) ** 2 < 0.0016:
            return (255, 250, 225)
        return mix(c, (255, 255, 255), 0.5 * max(0, math.sin(u * 9 + 1) * math.sin(v * 31)) ** 3)
    wave = math.sin(v * 420 + math.sin(u * 22) * 2.5)
    return mix((24, 92, 150), (10, 50, 96), (v - 0.55) / 0.45 + 0.12 * wave)


def sunset(u, v):
    if (u - 0.5) ** 2 + ((v - 0.58) * 0.67) ** 2 < 0.0045 and v < 0.6:
        return (255, 244, 200)
    if v < 0.6:
        return mix((92, 38, 78), (252, 150, 44), v / 0.6)
    glow = max(0.0, 1 - abs(u - 0.5) * 7) * (0.5 + 0.5 * math.sin(v * 380))
    return mix((30, 20, 44), (250, 150, 60), 0.75 * glow)


def mountains(u, v):
    ridge = 0.44 + 0.12 * math.sin(u * 7.0) + 0.05 * math.sin(u * 23 + 1) + 0.02 * math.sin(u * 61)
    if v < ridge:
        return mix((70, 130, 215), (190, 220, 245), v / 0.5)
    if v > 0.72:
        return mix((40, 110, 160), (20, 70, 120), 0.5 + 0.5 * math.sin(v * 360 + u * 6))
    if v < ridge + 0.07:
        return (238, 242, 248)
    if v < 0.6:
        return mix((95, 100, 112), (70, 76, 88), 0.5 + 0.5 * math.sin(u * 140 + v * 90))
    return mix((60, 128, 62), (42, 96, 48), 0.5 + 0.5 * math.sin(u * 230) * math.sin(v * 170))


def city(u, v):
    if v > 0.8:
        return mix((30, 30, 36), (16, 16, 20), 0.5 + 0.5 * math.sin(u * 300))
    k = int(math.floor(u * 14))
    top = 0.8 - (0.18 + 0.42 * noise(k, 7, 3))
    if v > top and (u * 14 - k) > 0.08:
        col, row = int((u * 14 - k) * 6), int((v - top) * 40)
        if noise(k * 31 + col, row, 9) > 0.55 and (v - top) * 40 - row > 0.3:
            return (255, 214, 120)
        return (34, 38, 58)
    return mix((8, 12, 40), (40, 30, 70), v / 0.8)


def forest(u, v):
    if v > 0.76:
        return mix((70, 96, 44), (50, 66, 34), 0.5 + 0.5 * math.sin(u * 260) * math.sin(v * 200))
    k = int(math.floor(u * 10))
    centre = (k + 0.5) / 10
    tip = 0.2 + 0.2 * noise(k, 3, 5)
    if v > tip and abs(u - centre) < (v - tip) * 0.085:
        return mix((22, 84, 40), (12, 52, 26), 0.5 + 0.5 * math.sin(v * 150 + u * 80))
    return mix((150, 190, 225), (228, 236, 230), v / 0.76)


def desert(u, v):
    if v < 0.5:
        return mix((236, 196, 150), (250, 228, 196), v / 0.5)
    dune = 0.5 + 0.5 * math.sin(u * 9 + v * 26 + math.sin(u * 3) * 2)
    return mix((214, 150, 84), (150, 96, 52), 0.25 + 0.6 * dune * (v - 0.4))


def lake(u, v):
    if v < 0.42:
        return mix((120, 170, 220), (215, 228, 240), v / 0.42)
    if v < 0.5:
        return mix((46, 92, 60), (30, 66, 44), 0.5 + 0.5 * math.sin(u * 190))
    return mix((70, 130, 170), (36, 84, 124), (v - 0.5) * 2 + 0.1 * math.sin(v * 400 + u * 5))


def render(scene, tilt=0.0, gain=1.0, seed=0):
    """Draw a scene as BMP bytes, with the camera rolled by `tilt` degrees."""
    cos, sin = math.cos(math.radians(tilt)), math.sin(math.radians(tilt))
    rows = []
    for py in range(H - 1, -1, -1):  # BMP rows run bottom to top
        row = bytearray()
        y = py / H - 0.5
        for px in range(W):
            x = px / H - 0.75
            u, v = (x * cos + y * sin + 0.75) / 1.5, -x * sin + y * cos + 0.5
            r, g, b = scene(u, v)
            grain = (noise(px, py, seed) - 0.5) * 14
            row += bytes((max(0, min(255, int((b + grain) * gain))),
                          max(0, min(255, int((g + grain) * gain))),
                          max(0, min(255, int((r + grain) * gain)))))
        rows.append(bytes(row))
    data = b"".join(rows)
    header = struct.pack("<2sIHHIIiiHHIIiiII", b"BM", 54 + len(data), 0, 0, 54, 40, W, H, 1, 24,
                         0, len(data), 2835, 2835, 0, 0)
    return header + data


def sips(*args):
    subprocess.run(["sips"] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def save(bmp, dest, taken, blur=False):
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "frame.bmp")
        with open(src, "wb") as f:
            f.write(bmp)
        if blur:  # shrink and enlarge: the cheap way to lose fine detail
            small = os.path.join(tmp, "small.bmp")
            sips("-Z", "200", src, "--out", small)
            sips("-z", str(H), str(W), small, "--out", src)
        sips("-s", "format", "jpeg", "-s", "formatOptions", "90", src, "--out", dest)
    os.utime(dest, (taken, taken))


# (scene, [variants]); each variant is (label, tilt degrees, brightness gain, blurred?)
BURSTS = [
    (coast, [("good", 0.2, 1.0, False), ("tilted", 3.6, 1.0, False), ("soft", 0.4, 1.0, True)]),
    (sunset, [("tilted", -2.8, 1.0, False), ("good", 0.0, 1.0, False), ("soft", 0.3, 1.0, True)]),
    (mountains, [("soft", 0.5, 1.0, True), ("good", -0.3, 1.0, False), ("tilted", 4.4, 1.0, False),
                 ("bright", 0.2, 1.4, False)]),
    (city, [("good", 0.1, 1.0, False), ("tilted", -3.2, 1.0, False)]),
]
SINGLES = [forest, desert, lake]


def make_video(dest, taken):
    """A 3-second clip of the sun setting, written with AVFoundation via a small Swift helper."""
    if not shutil.which("swift"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for i in range(12):
            def scene(u, v, lift=i * 0.012):
                return sunset(u, v - lift)
            bmp = os.path.join(tmp, "f%02d.bmp" % i)
            jpg = os.path.join(tmp, "f%02d.jpg" % i)
            with open(bmp, "wb") as f:
                f.write(render(scene, 0.0, 1.0, seed=i))
            sips("-s", "format", "jpeg", bmp, "--out", jpg)
            frames.append(jpg)
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "make_video.swift")
        ok = subprocess.run(["swift", helper, dest, "4"] + frames, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
    if ok:
        os.utime(dest, (taken, taken))
    return ok


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "demo-photos"
    marker = os.path.join(out, ".photocull-demo")
    if os.path.exists(out):
        # Only ever replace a folder this script made: never delete someone's photos.
        if not os.path.exists(marker):
            sys.exit("%s already exists and was not made by this script. Choose another name." % out)
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "second-import"))
    open(marker, "w").close()
    number = 0
    first = None
    for s, (scene, variants) in enumerate(BURSTS):
        for k, (label, tilt, gain, blur) in enumerate(variants):
            number += 1
            dest = os.path.join(out, "DSC_%04d.JPG" % number)
            save(render(scene, tilt, gain, seed=number), dest, START + s * 900 + k * 3, blur)
            first = first or dest
            print("%s  %-9s %s" % (os.path.basename(dest), scene.__name__, label))
    for s, scene in enumerate(SINGLES):
        number += 1
        dest = os.path.join(out, "DSC_%04d.JPG" % number)
        save(render(scene, 0.0, 1.0, seed=number), dest, START + 7200 + s * 900)
        print("%s  %-9s one-off" % (os.path.basename(dest), scene.__name__))
    # The same files imported twice: they show up as identical copies.
    for original in (first, dest):
        shutil.copy2(original, os.path.join(out, "second-import", os.path.basename(original)))
        print("second-import/%s  exact copy" % os.path.basename(original))
    written = number + 2
    if make_video(os.path.join(out, "DSC_0100.MOV"), START + 10800):
        written += 1
        print("DSC_0100.MOV  sunset    video")
    print("\nWrote %d files to %s. Now run:  ./photocull.sh %s" % (written, out, out))


if __name__ == "__main__":
    main()
