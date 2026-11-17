"""photocull: find duplicate and look-alike photos/videos and trash them in batches."""
import argparse
import sys
import webbrowser
from pathlib import Path

from . import grouping, index
from .server import App, serve


def main():
    ap = argparse.ArgumentParser(prog="photocull", description=__doc__)
    ap.add_argument("folder", help="folder of photos/videos (searched recursively)")
    ap.add_argument("--threshold", type=int, default=10,
                    help="how different two shots taken together may look and still be "
                         "grouped, 0-64 (default 10; lower = stricter)")
    ap.add_argument("--window", type=int, default=30,
                    help="seconds between shots to count as 'taken together' (default 30)")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--rescan", action="store_true", help="ignore the cache and re-read every file")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        sys.exit("Not a folder: %s" % root)

    cache = index.cache_dir(root)
    db = index.open_db(cache)

    def progress(done, total):
        sys.stdout.write("\rReading new files: %d / %d" % (done, total))
        sys.stdout.flush()

    print("Scanning %s ..." % root)
    rows = index.scan(root, db, cache, progress, rescan=args.rescan)
    print("\r%d photos and videos indexed.%s" % (len(rows), " " * 20))
    if not rows:
        sys.exit("No photos or videos found.")

    index.fill_qhashes(rows, db)
    unreadable = sum(1 for r in rows if not r["dhash"])
    shots = grouping.build_shots(rows)
    groups = grouping.group_shots(shots, threshold=args.threshold, window=args.window)

    exact = sum(1 for g in groups if g["kind"] == "exact")
    print("%d groups to review: %d of identical copies, %d of similar shots."
          % (len(groups), exact, len(groups) - exact))
    if unreadable:
        print("%d files had no readable preview; they are only matched as identical copies."
              % unreadable)

    app = App(root, groups, db, cache)
    try:
        server = serve(app, args.port)
    except OSError:
        sys.exit("Port %d is in use; pass --port with another number." % args.port)
    url = "http://127.0.0.1:%d/" % args.port
    print("Review page: %s   (Ctrl+C to quit)" % url)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
