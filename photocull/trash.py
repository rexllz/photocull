"""Move files to the macOS Trash (recoverable), never delete outright, and put
the most recent batch back on request."""
import json
import os
import subprocess

# JavaScript for Automation calling NSFileManager directly: lands in the real
# Trash of whichever volume the file is on, without scripting Finder.
_JXA = """
ObjC.import("Foundation");
function run(argv) {
  var fm = $.NSFileManager.defaultManager, moved = {};
  argv.forEach(function (p) {
    var landed = $();   // receives where the file ended up, for undo
    var ok = fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), landed, null);
    moved[p] = ok ? landed.path.js : null;
  });
  return JSON.stringify(moved);
}
"""


def put_back(moved):
    """Move trashed files back. `moved` maps each original path to where macOS put it
    in the Trash (it may have been renamed there). Returns the paths restored."""
    restored = []
    for original, trashed in moved.items():
        if not trashed or os.path.exists(original) or not os.path.exists(trashed):
            continue
        try:
            os.rename(trashed, original)
            restored.append(original)
        except OSError:
            pass
    return restored


def move_to_trash(paths, chunk=200):
    """Trash the given paths. Returns (paths that could not be moved,
    {path: where it landed in the Trash})."""
    failed, moved = [], {}
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        proc = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA] + part,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            result = json.loads(proc.stdout.decode())
        except ValueError:
            result = {}
        for p in part:
            if result.get(p):
                moved[p] = result[p]
            elif os.path.exists(p):
                failed.append(p)
            else:
                moved[p] = None   # gone, but macOS did not say where
    return failed, moved
