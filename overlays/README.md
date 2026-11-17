# Falcon overlay cards

Remotion compositions for Falcon highlight reels: `Intro`, `Outro`,
`LowerThird`, `Title`, `ScoreBar`, each in 16:9 and `-Vertical` (9:16). Props
are JSON (see `src/props.ts`).

```bash
npm ci                                   # or: hypecut overlays setup
npx remotion studio src/index.ts         # live preview in the browser
npx remotion still src/index.ts Intro intro.png --frame=100
```

Normally you don't call these directly: `hypecut overlays ...` and the
`hypecut falcon --intro/--title ...` flags render and compose for you. See
[docs/overlays.md](../docs/overlays.md), including the Remotion licence terms
and the Windows ARM64 notes.

Fonts: Big Shoulders Display and Atkinson Hyperlegible, SIL Open Font License
(`public/fonts/OFL-*.txt`).
