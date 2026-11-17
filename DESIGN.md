# Design

<!-- Falcon Highlights web UI (src/hypecut/web/static/index.html). Made with the
     Impeccable skill (pbakaus/impeccable): new-work, Operate mode. -->

## World: kit room

Washed kit cotton by day, the team bus at night. The world lends only type,
palette, density and one signature move; layout and controls are standard web
controls (radios, buttons, switches, a stepper).

- **Signature move:** every clip card carries its ID as a jersey number
  (Big Shoulders Display 900 over the frame). A dropped clip's number is struck through.
- **Type:** Big Shoulders Display (headings, numerals, tallies) and Atkinson
  Hyperlegible (everything else, chosen for daylight phone legibility). Both
  are self-hosted under `static/fonts/` (SIL OFL 1.1; licence files beside them).
- **Palette (light / dark):** ground `#f3efe6` / `#0e1524`, surface `#fffdf8` /
  `#162036`, ink `#14213d` / `#eef0f4`, secondary ink `#4a5470` / `#aeb7cc`.
  Navy header `#14213d`. One action colour, training-cone orange `#b8410c`
  (white text) / `#ff8a4c` (near-black text), used only on the primary action.
  Kept `#1d6b46` / `#5fd39a`. Inferred/guess `#8a5300` on `#f7e7c6`.
- **Theme:** follows the OS, with a header toggle saved in localStorage.

## Rules

- One primary action per step; on phones it lives in a bottom dock.
- Steps: Choose game → Your taps → Review clips → Build reel. Numbers in the
  stepper carry sequence, so they stay.
- A tap is a tap: copy never says "goal". Hybrid edge shifts and detector-only
  clips carry a *guess* badge and an explanation.
- Tap targets ≥44 px, visible focus ring (`--focus`), text ≥4.5:1.
- Numbers that change (tallies, times) use tabular numerals.
