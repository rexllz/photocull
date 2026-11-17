"""Move files to the macOS Trash (recoverable), never delete outright."""
import json
import subprocess

# JavaScript for Automation calling NSFileManager directly: lands in the real
# Trash of whichever volume the file is on, without scripting Finder.
_JXA = """
ObjC.import("Foundation");
function run(argv) {
  var fm = $.NSFileManager.defaultManager, failed = [];
  argv.forEach(function (p) {
    var ok = fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), null, null);
    if (!ok) failed.push(p);
  });
  return JSON.stringify(failed);
}
"""


def move_to_trash(paths, chunk=200):
    """Trash the given paths. Returns the list of paths that could not be moved."""
    failed = []
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        proc = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA] + part,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            failed.extend(json.loads(proc.stdout.decode()))
        except ValueError:
            failed.extend(part)
    return failed
