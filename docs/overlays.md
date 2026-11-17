# Overlay cards (Remotion)

Optional intro/outro cards, a player lower-third, title cards and a score bar
for Falcon highlight reels. Off by default: nothing changes unless you ask.

The cards are a small [Remotion](https://www.remotion.dev) project in
[`overlays/`](../overlays). Python renders them with the Remotion CLI and puts
them on a finished reel in **one ffmpeg pass**, writing
`highlights_overlays.mp4` next to `highlights.mp4`. The original reel is never
overwritten.

| Card | Composition (16:9 / 9:16) | Output | Background |
|---|---|---|---|
| Intro: player, number, team, date, opponent (optional) | `Intro` / `Intro-Vertical` | `intro.mp4`, 5 s | opaque, prepended |
| Outro: "Thanks for watching", player, team | `Outro` / `Outro-Vertical` | `outro.mp4`, 4 s | opaque, appended |
| Lower-third: number chip + name + team | `LowerThird` / `LowerThird-Vertical` | `lower_third.mov`, 5 s | transparent (ProRes 4444) |
| Title card you assign ("Goal!", "Save", "Assist") | `Title` / `Title-Vertical` | `title_NN_<text>.mov`, 3 s | transparent |
| Score bar (whole reel) | `ScoreBar` / `ScoreBar-Vertical` | `score_bar.mov`, holds last frame | transparent |

The 9:16 variants are picked automatically when the reel is taller than wide.
Cards render at the reel's native size (`--scale 2` for a 4K reel), with the
bundled OFL fonts (Big Shoulders Display, Atkinson Hyperlegible), so rendering
needs no network.

## Titles are yours, never automatic

A Falcon mark is a parent tap, not proof of a goal. Title cards appear **only**
on clips you name (`--title clip05=Goal!`). By default the title shows 0.5 s
before the clip's tap; add `@SECONDS` to time it yourself
(`--title clip05=Goal!@6`). A title or lower-third on a dropped or unknown clip
stops the run before anything is cut.

## Use it

With `hypecut falcon` (all flags optional):

```bash
hypecut falcon --from-plan games/20260425/plan_marks/falcon_plan.json \
  --intro --outro --player "Test Player" --number 10 --team "Test FC" \
  --game-date "Apr 25, 2026" --opponent "Sample United" \
  --lower-third clip01 --title clip05=Goal! --score 2-1 --score-labels TFC-SMP
```

Or from a config file ([example](../overlays/overlays.example.json)):

```bash
hypecut overlays render  --config overlays.json -o cards --stills   # just the cards + PNG previews
hypecut overlays compose highlights.mp4 --config overlays.json \
  --plan falcon_plan.json --clips-dir clips                          # cards on an existing reel
```

`overlays.json` keys: `team`, `player`, `number`, `date`, `opponent`, `accent`
(hex), `format` (`"16:9"` or `"9:16"`), `intro`, `outro`, `outroLine`,
`lowerThird` (list of clip IDs, or `"all"`), `titles` (`{"clip05": "Goal!"}`),
`titleAt` (`{"clip05": 6}`), `score` (`{"home": 2, "away": 1, "homeLabel":
"TFC", "awayLabel": "SMP"}` or `"2-1"`), `crf`.

## Install

Node 18+ and npm, inside `overlays/` only (nothing global):

```bash
hypecut overlays setup        # = npm ci in overlays/ (+ the Windows ARM64 step below)
```

Top-level packages: `remotion`, `@remotion/cli`, `@remotion/renderer`, `react`,
`react-dom` (dev: `typescript`, `@types/react`). On first render Remotion
downloads its pinned Chrome Headless Shell into `overlays/node_modules/.remotion/`.

### Windows on ARM (e.g. Snapdragon laptops)

Remotion ships neither Chrome Headless Shell nor its compositor for
`win32-arm64`. `hypecut overlays setup` handles both:

- **Browser:** the installed Chrome (or Edge) is passed with
  `--browser-executable`, Remotion's documented option for unsupported
  platforms. Override with `--browser PATH` or `HYPECUT_REMOTION_BROWSER`.
- **Compositor:** the x64 build (`@remotion/compositor-win32-x64-msvc`, same
  version as `remotion`) is fetched with `npm pack` into
  `overlays/.remotion-bin/win32-x64/` and passed with `--binaries-directory`.
  It runs under Windows' built-in x64 emulation.

Remotion warns that a full Chrome can render slightly differently from its
pinned headless shell; the cards use plain CSS, so this has not mattered.

## Licence

Remotion is **not** open source under an OSI licence. Its own licence
(`overlays/node_modules/remotion/LICENSE.md`, <https://www.remotion.dev/docs/license>):

- **Free**, including commercial use, for individuals, for-profit companies
  with up to 3 employees, non-profits, and for evaluation.
- Larger for-profit companies need a paid **Company License**.
- You may not resell or relicense Remotion itself or a derivative of it.
- Remotion 5.0 will change the terms slightly (remotion-dev/remotion#3750).
- Telemetry: CLI renders only report usage when a license key is set. We set
  none, so no render events are sent.

Remotion is installed by npm from the registry and is not redistributed in
this repository. Using it for a family's own highlight reels falls under the
free licence; anyone hosting this for others as a business should check the
terms above.
