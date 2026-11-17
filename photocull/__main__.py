"""photocull: find duplicate and look-alike photos/videos and trash them in batches.

Starts a local web page where you pick a folder, review the groups and move the
shots you don't want to the Trash.
"""
import argparse
import sys
import webbrowser

from .server import App, serve


def main():
    ap = argparse.ArgumentParser(prog="photocull", description=__doc__)
    ap.add_argument("folder", nargs="?", help="optional: folder to open straight away")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    app = App()
    if args.folder:
        error = app.open(args.folder)
        if error:
            sys.exit("%s (%s)" % (error, args.folder))
    try:
        server = serve(app, args.port)
    except OSError:
        sys.exit("Port %d is in use; pass --port with another number." % args.port)
    # The key in the link signs this browser in; without it the page is refused.
    url = "http://127.0.0.1:%d/?k=%s" % (args.port, app.token)
    print("photocull is running. Open this link (Ctrl+C to quit):\n  %s" % url, flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
