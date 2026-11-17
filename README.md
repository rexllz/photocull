# photocull

Find duplicate and look-alike photos and videos from a Nikon camera, review them
side by side, and move the ones you don't want to the Trash in one batch.

Runs locally on macOS with nothing to install: Python 3 from the Xcode command
line tools, plus the system's own image tools (which read `.NEF` RAW files).

## Use

```sh
./photocull.sh /path/to/photos
```

It scans the folder (and subfolders), then opens a review page in your browser.
Press `Ctrl+C` in the terminal to quit.

The first scan reads every file, so allow a few minutes for thousands of RAW
files. Later runs reuse a cache and only read new or changed files.

## What gets grouped

| Group | Meaning | Starts out marked for Trash? |
|---|---|---|
| **Identical copies** | Same file content, e.g. the same card imported twice | Yes, all but the best copy |
| **Similar shots** | Look alike and were taken within 30 seconds (bursts, retakes), or are near-identical at any time (resized or re-saved copies) | No, you choose |

A RAW file and its same-named JPEG in the same folder (`DSC_0001.NEF` +
`DSC_0001.JPG`) count as one shot and are kept or trashed together.

In each group the "Best" shot is listed first: it prefers shots that have a RAW
file, then more pixels, then the larger file, then names without "copy".

## Reviewing

- Click a shot to switch it between **KEEP** and **TRASH**.
- **Keep best only** / **Keep all** per group, or for every group shown.
- **View** opens full size; `←` `→` flip between the shots of a group to compare.
- **Move to Trash** moves everything marked to the macOS Trash. Nothing is
  deleted permanently until you empty the Trash.

Keys: `J`/`K` next/previous group, `1`–`9` toggle a shot, `B` keep best only,
`A` keep all, `Space` full size, `X` toggle while full size, `Esc` close.

## Options

```
--threshold N   how different shots taken together may look and still be grouped,
                0-64 (default 10; lower is stricter)
--window N      seconds between shots to count as taken together (default 30)
--rescan        ignore the cache and re-read every file
--port N        port for the review page (default 8777)
--no-open       don't open the browser
```

## Notes

- Files larger than 4 MB are compared by size plus three 1 MB samples (start,
  middle, end) rather than every byte, to keep scanning large videos fast.
- Videos are matched as identical copies, or as similar by their preview frame.
- The cache lives in `~/Library/Caches/photocull/`; the photo folder is never
  written to.
- The review page is only reachable from this Mac (`127.0.0.1`).

## Tests

```sh
python3 -m unittest discover -s tests
```
