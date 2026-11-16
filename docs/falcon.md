# HypeCut for XbotGo Falcon

This fork adds a `hypecut falcon` command for youth-sports games recorded on an
**XbotGo Falcon** (or any auto-tracking camera). Give it the game MP4 and the
`.mark` file the XbotGo app saves with it, and you get:

```
game_highlights/
  highlights.mp4              the reel, every clip in order
  clips/clip01_mark_9-09.mp4  one file per clip, named after the tap that made it
  clips/clip02_mark_14-31_merged.mp4   two taps close together became one clip
  falcon_plan.json            what was cut and why (edit it and re-render)
  highlights_phone.mp4        optional, 1080p for sharing (--phone)
  highlights_vertical.mp4     optional, 9:16 that follows the play (--vertical)
```

Everything runs on your own computer. Nothing is uploaded anywhere.

---

## Parent quickstart (Windows)

You need **Python 3.10+**, **FFmpeg** and **Git**. In PowerShell:

```powershell
winget install Gyan.FFmpeg      # skip any you already have
winget install Git.Git
winget install Python.Python.3.12
```

Close PowerShell and open a new one (so it sees the new programs), then:

```powershell
cd $HOME\Documents
git clone -b falcon-mvp https://github.com/Justin-Rezamp/falcon-hypecut.git
cd falcon-hypecut
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -e ".[web]"
```

Each time you come back, open PowerShell and run
`cd $HOME\Documents\falcon-hypecut; .\.venv\Scripts\Activate.ps1` first.

### Make a reel

Put the MP4 and its `.mark` file in the same folder with the same name
(`game.mp4` + `game.mark`). Then:

```powershell
# 1. Look before you cut: prints the clip table, writes falcon_plan.json, cuts nothing
hypecut falcon "$HOME\Videos\Falcon\game.mp4" --plan-only

# 2. Cut the clips and join the reel
hypecut falcon "$HOME\Videos\Falcon\game.mp4"
```

The output folder is `game_highlights` next to the video (change it with
`-o`). Running it again **replaces** `highlights.mp4` and says so.

Common variations:

```powershell
hypecut falcon game.mp4 --pad 15                 # 15 s before and after every tap
hypecut falcon game.mp4 --before 12 --after 6    # more build-up, less aftermath
hypecut falcon game.mp4 --phone --loudnorm       # + a 1080p copy at a steady volume
hypecut falcon game.mp4 --vertical               # + a 9:16 copy for Reels/TikTok/Shorts
hypecut falcon game.mp4 --mode hybrid            # let the detectors fine-tune each window
hypecut falcon game.mp4 --sport basketball       # basketball tuning for hybrid/detect
```

### Edit, then re-render

`falcon_plan.json` lists every clip as `clip01`, `clip02`, ... with `start`,
`end` (seconds) and `keep`. Set `"keep": false` to drop a clip, or change the
times to recut it, then:

```powershell
hypecut falcon --from-plan "$HOME\Videos\Falcon\game_highlights\falcon_plan.json"
```

### Or use the web page

```powershell
hypecut serve      # then open http://127.0.0.1:8000
```

Drop the MP4, click **Add .mark file**, pick **Just my taps** or
**Taps + smart edges**, and press the button. Each clip is downloadable by name.

---

## Getting the MP4 and .mark off the Falcon

Per XbotGo's help centre and the community [Falcon Player](https://www.tekscene.com/tools/falcon-player) notes on the SD-card layout:

- **SD card (best for the full-quality game):** power the Falcon off, take out
  the microSD card and put it in a card reader. The MP4s are on the card; the
  `.mark` files (with `.vdata`, `.cover`, `.track`) are in a hidden **`.data`**
  folder beside them, with the same file names. In Windows Explorer turn on
  *View → Show → Hidden items* to see it. Copy the MP4 and its matching `.mark`.
  You can point `hypecut falcon` straight at the card or at a copy: it looks
  for `game.mark` next to the video first and then in `.data\`.
- **XbotGo app:** *Gallery → Falcon → Select → Download* copies videos into the
  app (and *Save to Gallery* onto the phone). That path gives you the MP4; check
  whether a `.mark` came with it — if not, use the SD card, or pass
  `--mode detect` to let the detectors choose.

If the taps were edited in the app (*Edit → Edit Marker in Video*), export the
original video with updated markers so the `.mark` matches.

**H.264 vs HEVC.** Falcon firmware 5.0.1 and later record **HEVC (H.265)** to
save space (per the Falcon Player notes); the "Compatible" (H.264) setting plays everywhere. Both work here:
HEVC (or anything above 1080p) is analysed on a small temporary proxy and the
clips are re-encoded to H.264 so the reel plays on any phone or PC. H.264/AAC
sources are stream-copied with no quality loss (fast, but each clip may begin
up to a keyframe — a second or two — early).

---

## Modes

| Mode | When | What it does |
|---|---|---|
| `marks` *(default with a .mark)* | you tapped during the game | Exactly the Falcon Highlights runner rules: 10 s before and after each tap, windows that overlap or are within 2 s merge, `clip01_mark_9-09.mp4` names (`_merged` when a clip holds several taps). No video analysis — fast. |
| `hybrid` | you tapped, but late or early | Every tap still makes a clip with the same name. The detectors look for a clear action peak up to 6 s before (`--max-shift`) or 3 s after the tap and slide the window to centre on it — never so far that the tap falls outside its clip. `--extras N` adds up to N clips nobody tapped, named `clipNN_auto_M-SS.mp4`. |
| `detect` *(default without a .mark)* | no taps | HypeCut's detectors with the `falcon-<sport>` profile. Clips are named `_auto_`. |

The plan always separates **facts** (what the `.mark` file contains, what
ffprobe reports) from **notes and inferences** (a detector thinks the action
peaked 4 s before your tap; a tap was after the end of the video and was
skipped). A tap is never treated as proof of a goal, and mark times are never
invented.

### Using taps in the general HypeCut pipeline

`hypecut cut` and `hypecut analyze` accept `--marks [file]` (bare `--marks`
finds the sibling `.mark`). That adds the `falcon_marks` signal — a bump around
each tap that leans *before* it, because people tap after they see something —
fused with weight 2 alongside the other detectors. It raises tapped moments a
lot but, unlike `hypecut falcon`, does not guarantee a clip per tap.

---

## Why a Falcon needs its own profile: the moving camera

Every motion-based detector in HypeCut assumes the camera is still or cuts
between fixed angles. A Falcon does neither: it pans, tilts and zooms the
whole game to follow the ball. To a frame-difference detector that looks like
non-stop action, and every fast swing looks like a "cut".

`hypecut.camera` fixes that the way video stabilisers do. For every pair of
analysis frames it estimates the camera's own motion — a zoom plus a shift,
from phase correlation, checked against several candidates (a bright player can
steal the strongest correlation peak), a smoothed temporal prior and a
Lucas-Kanade refinement — warps the previous frame onto the current one, and
measures what is left. A pure pan leaves almost nothing; players and the ball
are what remains.

`configs/falcon-soccer.yaml` / `falcon-basketball.yaml` turn that on:

- `motion` with `compensate_camera: true` — residual motion, not camera motion
- `camera_pan` — the camera's own speed, a weak cue (the Falcon swings hard on a breakaway)
- `segments.snap_compensate_camera: true` — boundaries the camera explains are not cuts
- a 5 Hz analysis grid, so a 90-minute game analyses in a few minutes on a laptop

All of it is opt-in. Every other profile behaves exactly as before.

---

## Limitations (this pass)

- **No player tracking.** It cannot follow *your* kid, read jersey numbers, find
  the ball or detect goals. Detectors find "busy and loud", which is often but
  not always the moment that matters. Review `_auto_` clips before sharing.
- **Taps drive the reel.** In `marks` mode a mistaken tap becomes a clip; drop it
  in `falcon_plan.json` and re-render.
- **Stream copy is keyframe-accurate, not frame-accurate.** Use `--cut encode`
  for frame-exact edges (slower).
- **The camera-motion model is pan/tilt/zoom.** Rolling-shutter wobble, heavy
  wind shake or a camera knocked over are not modelled, and a low-detail frame
  (a wide shot of flat grass in fog) can defeat the estimate — it then falls
  back to the plain difference rather than inventing motion.
- **The `.track` telemetry file is not used yet** — see next steps.
- **`hypecut falcon` needs a checkout** (it reads `configs/falcon-*.yaml` from
  the repo or the current folder), or pass `--profile`.

## Next steps

1. **Click-your-kid tracking:** YOLO person detection + ByteTrack, pick the
   player on one frame, keep only clips where they are on screen, and steer the
   9:16 crop to them.
2. **Jersey number OCR** to find a player without clicking.
3. **Ball detection** for possession-aware edges and better hybrid peaks.
4. **Goal detection** (ball crossing the goal line, or the celebration pattern),
   so a reel can be "goals only".
5. **Use the Falcon `.track` telemetry** for exact camera motion instead of
   estimating it, when the file is present.
6. Calibrate `hybrid` shift limits and profile weights against a few real,
   hand-labelled games (`hypecut label` + `hypecut eval`).
