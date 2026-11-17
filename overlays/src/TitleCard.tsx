import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {clamp, enter} from './anim';
import {ensureFonts} from './fonts';
import type {CardProps} from './props';
import {colors, fitSize, fonts, onAccent, safeAccent, useUnit} from './theme';

ensureFonts();

/**
 * Transparent title stinger ("Goal!", "Save", "Assist" ...).
 * The text always comes from the user's own edit instruction: a Falcon mark is
 * only a parent tap, never proof of what happened.
 */
export const TitleCard: React.FC<CardProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const {u, vertical, width} = useUnit();
  const accent = safeAccent(p.accent);
  const title = (p.title || '').toUpperCase();
  const sub = p.subtitle || '';

  const pop = enter(frame, fps, 0, 11);
  const slab = interpolate(frame, [0, 9], [0, 1], clamp);
  const shine = interpolate(frame, [8, 30], [-30, 130], clamp);
  const out = interpolate(frame, [durationInFrames - 10, durationInFrames], [1, 0], clamp);
  const outScale = interpolate(frame, [durationInFrames - 10, durationInFrames], [1, 1.08], clamp);
  const size = fitSize(title, (vertical ? 230 : 250) * u, width * (vertical ? 0.8 : 0.7), 0.5);
  const ink = onAccent(accent);

  if (!title) return <AbsoluteFill />;

  return (
    <AbsoluteFill
      style={{
        alignItems: 'center',
        justifyContent: 'center',
        opacity: out,
        transform: `scale(${outScale})`,
      }}
    >
      <div style={{position: 'relative', transform: `scale(${0.6 + 0.4 * pop}) rotate(-4deg)`}}>
        <div
          style={{
            position: 'absolute',
            inset: `${-10 * u}px ${-56 * u}px`,
            backgroundColor: accent,
            transform: `skewX(-12deg) scaleX(${slab})`,
            borderRadius: 12 * u,
            boxShadow: `0 ${16 * u}px ${48 * u}px rgba(0,0,0,0.45)`,
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              position: 'absolute',
              top: 0,
              bottom: 0,
              left: `${shine}%`,
              width: '18%',
              background:
                'linear-gradient(90deg, transparent, rgba(255,255,255,0.55), transparent)',
            }}
          />
        </div>
        <div
          style={{
            position: 'relative',
            fontFamily: fonts.display,
            fontWeight: 900,
            fontSize: size,
            lineHeight: 1,
            color: ink,
            padding: `${6 * u}px ${12 * u}px`,
            whiteSpace: 'nowrap',
          }}
        >
          {title}
        </div>
      </div>
      {sub ? (
        <div
          style={{
            marginTop: 54 * u,
            padding: `${10 * u}px ${28 * u}px`,
            backgroundColor: colors.panel,
            borderRadius: 10 * u,
            fontFamily: fonts.body,
            fontWeight: 700,
            fontSize: 40 * u,
            color: colors.text,
            opacity: interpolate(frame, [10, 20], [0, 1], clamp),
          }}
        >
          {sub}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
