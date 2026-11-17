# photocull

Clear out duplicate and look-alike photos from a camera card or photo folder.
photocull groups the shots that show the same thing, scores each one for focus,
tilt and exposure, and lets you move the ones you don't want to the Trash in one
batch.

It runs entirely on your Mac: a small local web page, no account, no upload,
nothing to install. You can also review from your phone over Wi-Fi by scanning
a QR code.

![Review page: groups of similar shots with scores](docs/review.png)

## Requirements

- macOS (uses the system's image tools and Vision framework)
- Xcode command line tools, which provide Python 3 and the Swift compiler:

  ```sh
  xcode-select --install
  ```

There are no Python packages to install.

## Quick start

```sh
git clone https://github.com/rexllz/photocull.git
cd photocull
./photocull.sh
```

Your browser opens the start page. Choose a folder (or paste its path), wait for
the scan, then review. Press `Ctrl+C` in the terminal to quit.

![Start page](docs/welcome.png)

To skip the chooser, pass the folder: `./photocull.sh /path/to/photos`.

### Try it without your own photos

```sh
python3 demo/make_demo.py      # draws 17 synthetic "photos" into ./demo-photos
./photocull.sh demo-photos
```

The demo set contains bursts where one frame is good, one is tilted and one is
out of focus, plus exact copies and a few one-off scenes. The screenshots in
this README were taken from it.

### What to expect on the first run

- The first scan reads every file: allow a few minutes for a few thousand
  photos. Later scans reuse a cache and only read new or changed files.
- The first run also compiles a small Swift helper (about 30 seconds, once).
- macOS may ask whether the terminal can access the folder or removable volume
  you picked. Allow it, or the scan finds nothing.

## Reviewing

Each group shows the shots side by side, best first.

- Click a shot to switch it between **KEEP** and **TRASH**.
- **Keep best only** / **Keep all** work per group, or on every group shown.
- **View** opens a shot full size; `←` `→` flip between the shots of a group.
- **Move to Trash** moves everything marked to the macOS Trash. Nothing is
  deleted permanently until you empty the Trash.

Tabs:

| Tab | Shows |
|---|---|
| **All** | Every group |
| **Identical** | Byte-for-byte copies, e.g. a card imported twice. Extras start out marked for the Trash. |
| **Similar** | Look-alike shots: bursts, retakes, a subject that moved. Nothing is pre-marked. |
| **Unique** | Every shot with no look-alike, so you can see what was left out and trash any of those too. |

![Unique tab](docs/unique.png)

Controls:

- **Strict / Loose**: how alike two shots must look to be grouped.
- **Taken within**: how close in time shots must be to count as one burst.
- **Refresh**: pick up files added to the folder since the scan.

Keys: `J`/`K` next/previous group, `1`–`9` toggle a shot, `B` keep best only,
`A` keep all, `Space` full size, `X` toggle while full size, `Esc` close.

## Review from your phone

<img src="docs/phone.png" alt="Review page on a phone" width="300">

You can review on a phone while the photos stay on the Mac:

1. On the Mac's review page, click **Phone** and confirm. macOS may ask to
   allow incoming connections; choose Allow.
2. Scan the QR code with the phone's camera. The same review page opens in the
   phone's browser, laid out for a small screen.
3. Tap a shot to mark it, tap **View** to see it full size (swipe to flip
   between shots, pinch to zoom), and use **Move to Trash** as on the Mac. Files
   are moved to the Trash on the Mac; nothing on the phone is touched.
4. Click **Phone: on** → **Turn off phone access** when you're done.

Both devices must be on the same Wi-Fi, and the Mac must stay awake. Some
networks (guest, hotel, some shared building Wi-Fi) block devices from reaching
each other; phone access cannot work there.

## Scores and the "Best" shot

Every shot in a group gets a score out of 100, with its three parts shown:

| Check | Weight | How it is measured |
|---|---|---|
| **Focus** | 40% | Edge sharpness (Tenengrad) inside the subject: the faces if there are any, otherwise the region that draws the eye. Shown relative to the sharpest shot in the group. With faces, Apple's face capture quality (blur, eyes, expression) is blended in. |
| **Level** | 35% | The tilt of the longest straight near-level edge (horizon, shoreline, railing), found with a Hough transform. "Tilt n/a" means there is no such line; the check is then left out of the score. |
| **Exposure** | 25% | Share of the frame blown to white or crushed to black, plus overall brightness. |

The best shot is listed first. A clearly over- or under-exposed shot is never
best while another is available; then the highest score wins; ties fall back to
having a RAW file, more pixels, a larger file, and a name without "copy".

The score is a starting suggestion, not a verdict. It cannot judge composition
or expression, and tilt in particular can be fooled by a slope or a receding
shoreline. Compare by eye before trashing similar shots.

## How it works

```mermaid
flowchart LR
    subgraph Browser
        P[Review page on the Mac<br>or a phone via QR code]
    end
    subgraph "Local server (127.0.0.1; Wi-Fi only if you switch phone access on)"
        S[HTTP server<br>session key required]
    end
    subgraph "Logic"
        A[Scan<br>thumbnails + capture time] --> B[Fingerprint<br>shape hash + colour grid]
        A --> Q[Score<br>focus · tilt · exposure]
        B --> G[Group<br>identical · near-identical · same scene]
        Q --> R[Rank<br>pick Best]
        G --> R
    end
    C[(Cache<br>~/Library/Caches/photocull)]
    T[(macOS Trash)]
    P <-->|JSON + images| S
    S --> A
    R --> S
    A <--> C
    S -->|marked shots| T
```

1. **Scan.** Walks the folder and uses `sips` (built into macOS) to make a
   small thumbnail of each photo and read its capture time. Because the system
   does the decoding, RAW formats macOS understands (such as Nikon `.NEF`) are
   read the same way as JPEGs.
2. **Fingerprint.** Each thumbnail is reduced to a 256-bit shape hash (where
   the picture gets brighter or darker) and a 4×4 grid of average colours.
3. **Score.** A Swift helper (`photocull/photoscore.swift`) measures focus,
   tilt and exposure once per photo using the Vision framework.
4. **Group.** Shots are grouped when:
   - their file contents are identical; or
   - shape and colour both match closely, whenever they were taken (resized or
     re-saved copies); or
   - they were taken moments apart and show the same scene: the colours sit in
     the same places even if the subject moved, or the shapes line up across a
     moderate exposure change.

   A shot joins a burst only if it resembles the burst's first shot as well as
   its latest, so a slowly changing sequence cannot chain unrelated pictures
   into one group.
5. **Rank.** Within each group the shots are ordered by score.
6. **Trash.** Marked files are moved to the Trash with the system's own Trash
   function, never deleted outright.

A RAW file and its same-named JPEG in one folder (`DSC_0001.NEF` +
`DSC_0001.JPG`) count as one shot and are kept or trashed together.

Layout:

```
photocull.sh                launcher
photocull/__main__.py       command line entry
photocull/index.py          scan, thumbnails, fingerprints, cache, scoring driver
photocull/photoscore.swift  focus / tilt / exposure measurements
photocull/grouping.py       shots, grouping rules, scores and ranking
photocull/server.py         local web server and actions
photocull/trash.py          move to Trash
photocull/web/index.html    the page
demo/make_demo.py           synthetic demo photos
tests/                      unit tests
```

## Privacy and safety

- **Nothing leaves your network.** There are no calls to the internet,
  analytics or third-party services, and the page loads no external scripts or
  fonts.
- **Local only by default.** The server listens on `127.0.0.1`, so other
  devices on your network cannot reach it.
- **Phone access is opt-in.** Only while you switch it on does the app also
  answer on your Wi-Fi address. The phone gets its own random key (in the QR
  code), which stops working the moment you switch phone access off. From the
  phone you can review and trash within the open folder only: choosing other
  folders stays on the Mac. The connection is plain HTTP inside your network,
  so use it on Wi-Fi you trust, not on public networks.
- **Session key.** The link printed at start-up contains a random key. Every
  request must carry it, so other users logged in to the same Mac cannot view
  your photos or trash anything. Treat the link like a password while the app
  is running; it stops working when you quit.
- **Trash, not delete.** Files go to the macOS Trash and can be restored until
  you empty it. On a camera card or external drive they go to that volume's
  own Trash and keep using its space until you empty the Trash with the volume
  connected.
- **Your photo folder is never written to.** Thumbnails, scores and the index
  live in `~/Library/Caches/photocull/`; delete that folder to wipe them.
- **Photos only.** Videos are not scanned or shown, and the Trash action
  refuses them. (`INCLUDE_VIDEOS` in `photocull/index.py` turns them on; that
  path is less tested.)

## Limitations

- macOS only.
- Developed and checked mostly against camera JPEGs. RAW files rely on macOS
  decoding and have had little testing.
- Files larger than 4 MB are compared by size plus three 1 MB samples (start,
  middle, end) rather than every byte, to keep scanning fast.
- A retake with a very different exposure may not be grouped with its
  siblings, because grouping looks at colour.
- Very plain frames (dark or blank shots) can be grouped as similar.
- The score weights and thresholds are sensible defaults, not tuned on a large
  photo collection. They live at the top of `photocull/grouping.py`.

## Command line options

```
./photocull.sh [folder] [--port N] [--no-open]

--port N     port for the page (default 8777)
--no-open    don't open the browser; use the printed link
```

## Tests

```sh
python3 -m unittest discover -s tests
```

## Licence

MIT. See [LICENSE](LICENSE).
