# photocull

Find duplicate and look-alike photos and videos from a Nikon camera, review them
side by side, and move the ones you don't want to the Trash in one batch.

Runs locally on macOS with nothing to install: Python 3 from the Xcode command
line tools, plus the system's own image tools (which read `.NEF` RAW files).

## Use

```sh
./photocull.sh
```

This starts a local web page and opens it in your browser. Everything happens
there: choose a folder, watch the scan, review the groups, move shots to the
Trash. Press `Ctrl+C` in the terminal to quit.

You can also pass a folder to skip the chooser: `./photocull.sh /path/to/photos`.

The first scan of a folder reads every file, so allow a few minutes for
thousands of RAW files. Later scans reuse a cache and only read new or changed
files.

## What gets grouped

| Group | Meaning | Starts out marked for Trash? |
|---|---|---|
| **Identical copies** | Same file content, e.g. the same card imported twice | Yes, all but the best copy |
| **Similar shots** | Look alike in both shape and colour and were taken within 30 seconds (bursts, retakes), or are near-identical at any time (resized or re-saved copies) | No, you choose |

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

## Tuning what counts as similar

The toolbar has two controls, applied instantly without rescanning:

- **Strict / Loose**: how alike two shots must look to be grouped.
- **Taken within**: how close in time shots must be to count as one burst.

**Refresh** picks up files added to the folder since the scan.

Command line options: `--port N` (default 8777) and `--no-open` (don't open the
browser).

## How similarity is judged

Each shot gets two fingerprints from a tiny version of the picture: a 256-bit
shape hash (where it gets brighter or darker) and a 4x4 grid of average colours.
Two shots count as similar only when both agree. A shot joins a burst only if it
resembles the burst's first shot as well as its latest, so a slowly changing
sequence can't chain unrelated pictures into one group.

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
