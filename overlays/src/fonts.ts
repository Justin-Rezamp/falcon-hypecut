import {continueRender, delayRender, staticFile} from 'remotion';

// Bundled OFL fonts (see public/fonts/OFL-*.txt): no network at render time.
const faces: Array<[string, string, string]> = [
  ['Big Shoulders Display', 'fonts/big-shoulders-display.woff2', '100 900'],
  ['Atkinson Hyperlegible', 'fonts/atkinson-400.woff2', '400'],
  ['Atkinson Hyperlegible', 'fonts/atkinson-700.woff2', '700'],
];

let loaded = false;

export const ensureFonts = () => {
  if (loaded || typeof document === 'undefined') return;
  loaded = true;
  const handle = delayRender('Loading bundled fonts');
  Promise.all(
    faces.map(([family, file, weight]) => {
      const face = new FontFace(family, `url(${staticFile(file)}) format('woff2')`, {weight});
      document.fonts.add(face);
      return face.load();
    }),
  )
    .then(() => continueRender(handle))
    .catch((err) => {
      console.warn('Font load failed, using fallbacks', err);
      continueRender(handle);
    });
};
