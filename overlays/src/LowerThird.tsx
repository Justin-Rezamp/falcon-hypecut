import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {clamp, enter} from './anim';
import {ensureFonts} from './fonts';
import type {CardProps} from './props';
import {colors, fitSize, fonts, onAccent, safeAccent, useUnit} from './theme';

ensureFonts();

/** Transparent lower-third: number chip + name/team panel. Slides in and out. */
export const LowerThird: React.FC<CardProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const {u, vertical, width} = useUnit();
  const accent = safeAccent(p.accent);
  const name = (p.playerName || '').toUpperCase();
  const team = p.teamName || '';
  const num = (p.playerNumber || '').replace(/^#/, '');

  const inP = enter(frame, fps, 0, 20);
  const panel = enter(frame, fps, 5, 20);
  const outP = interpolate(frame, [durationInFrames - 14, durationInFrames], [0, 1], clamp);
  const k = vertical ? 1.25 : 1;
  const chip = 150 * u * k;
  const maxName = (vertical ? width * 0.62 : width * 0.36) ;
  const nameSize = fitSize(name, 82 * u * k, maxName, 0.47);
  const left = (vertical ? 60 : 96) * u;
  const bottom = vertical ? 420 * u : 110 * u;
  const shift = (1 - inP) * -(chip + 400 * u) + outP * -(chip + 900 * u * k);

  return (
    <AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          left,
          bottom,
          display: 'flex',
          alignItems: 'stretch',
          height: chip,
          transform: `translateX(${shift}px)`,
          filter: `drop-shadow(0 ${8 * u}px ${24 * u}px rgba(0,0,0,0.45))`,
        }}
      >
        {num ? (
          <div
            style={{
              width: chip,
              backgroundColor: accent,
              color: onAccent(accent),
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontFamily: fonts.display,
              fontWeight: 900,
              fontSize: chip * 0.66,
              borderRadius: `${14 * u}px 0 0 ${14 * u}px`,
            }}
          >
            {num}
          </div>
        ) : null}
        <div
          style={{
            backgroundColor: colors.panel,
            padding: `0 ${44 * u * k}px 0 ${36 * u * k}px`,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            borderRadius: num ? `0 ${14 * u}px ${14 * u}px 0` : 14 * u,
            borderTop: `${3 * u}px solid ${colors.line}`,
            clipPath: `inset(0 ${(1 - panel) * 100}% 0 0)`,
          }}
        >
          <div
            style={{
              fontFamily: fonts.display,
              fontWeight: 800,
              fontSize: nameSize,
              lineHeight: 0.95,
              color: colors.text,
              whiteSpace: 'nowrap',
            }}
          >
            {name || team.toUpperCase()}
          </div>
          {name && team ? (
            <div
              style={{
                fontFamily: fonts.body,
                fontWeight: 700,
                fontSize: 30 * u * k,
                letterSpacing: 2 * u,
                textTransform: 'uppercase',
                color: accent,
                marginTop: 6 * u,
              }}
            >
              {team}
            </div>
          ) : null}
        </div>
      </div>
    </AbsoluteFill>
  );
};
