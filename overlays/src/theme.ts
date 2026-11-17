import {useVideoConfig} from 'remotion';

export const FPS = 30;

export const colors = {
  ink: '#0A0F1C',
  panel: 'rgba(10, 15, 28, 0.92)',
  panelSolid: '#0E1526',
  line: 'rgba(255, 255, 255, 0.10)',
  text: '#F7F8FA',
  muted: 'rgba(247, 248, 250, 0.72)',
  defaultAccent: '#FFC41F',
};

export const fonts = {
  display: '"Big Shoulders Display", "Arial Narrow", sans-serif',
  body: '"Atkinson Hyperlegible", "Segoe UI", Arial, sans-serif',
};

/** 1 unit = 1px on a 1080p short side, so cards scale with the output. */
export const useUnit = () => {
  const {width, height} = useVideoConfig();
  return {u: Math.min(width, height) / 1080, vertical: height > width, width, height};
};

/** Accept #RGB / #RRGGBB only; anything else falls back to the default accent. */
export const safeAccent = (value?: string): string =>
  value && /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(value) ? value : colors.defaultAccent;

/** Readable text colour on top of the accent (black on light accents, white on dark). */
export const onAccent = (hex: string): string => {
  let h = hex.slice(1);
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return lum > 0.55 ? colors.ink : colors.text;
};

/** Shrink a condensed display font until `text` fits `maxWidth`. */
export const fitSize = (text: string, base: number, maxWidth: number, perChar = 0.5): number => {
  const est = Math.max(1, text.length) * perChar;
  return Math.min(base, maxWidth / est);
};
