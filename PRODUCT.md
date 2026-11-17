# Product

<!-- impeccable:product-schema 1 -->

> Inferred from the owner's written brief (Falcon Highlights spec and the Nov 16 UI request);
> the init interview could not run live. Inferred facts are marked *(inferred)*.

## Platform

web

## Users

A soccer parent who films youth games with an XbotGo Falcon auto-tracking camera and taps the
Falcon app's mark button when something happens. They open this tool after the game, often on a
phone at home or in the car, sometimes on a laptop, and want a highlight reel of their kid to keep
and share with family. Not a video editor; not a coach running analytics *(inferred)*.

## Product Purpose

Turn a Falcon game recording plus its `.mark` file into named clips (`clip01_mark_9-09.mp4`) and one
`highlights.mp4`, with the parent reviewing which clips go in before anything is cut. Success: the
reel contains the moments they tapped, nothing they dropped, and they never had to touch a timeline.

## Positioning

Local-first and tap-driven: the parent's own taps are the source of truth, the footage never leaves
their computer, and every automatic guess is labelled as a guess.

## Operating Context

- Footage is large (a 58-minute 4K game is ~21.7 GB, H.264). Games often already sit on the laptop
  in a `games/<date>/` folder, copied from Google Drive or the camera's SD card.
- `.mark` files live in the SD card's hidden `.data` folder and are usually named differently from
  the video (`VID_20260425_133314_01_01.mark` vs `Your_Video_File.mp4`).
- Rules from the spec: only `type: click` marks; 10 s pad before and after each tap; windows that
  overlap or sit within 2 s merge; clips are referred to by ID; cutting needs the parent's go-ahead.
- Server binds to 127.0.0.1; the parent opens it in a browser on the same computer.

## Capabilities and Constraints

- Modes: marks (taps only), hybrid (taps with detector-shifted edges), detect (no taps).
- Exports: original-size reel, 1080p phone copy, 9:16 vertical, loudness normalisation.
- Planning is fast and cuts nothing; rendering a 4K game takes minutes.
- Terminology: a **tap** (mark) means "something happened near here", never "goal". Hybrid shifts
  and detector-only clips are inferences and must say so.

## Evidence on Hand

- Real game 2 `.mark` (41 taps) and its 58:08 video on the owner's laptop. No real thumbnails or
  player identities may be invented; the UI shows frames from the actual video only.

## Product Principles

1. The parent's taps are facts; anything the software adds is labelled as a guess.
2. Nothing is cut until the parent has seen the clip list and pressed the build button.
3. Never make the parent move a 20 GB file twice.
4. Phone-first: one obvious next step on a small screen.

## Accessibility & Inclusion

WCAG 2.2 AA contrast, visible keyboard focus, 44 px tap targets, readable in bright daylight
on a phone *(inferred)*.
