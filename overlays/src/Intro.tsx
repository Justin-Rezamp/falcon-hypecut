import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {PitchBackground} from './Background';
import {clamp, enter, exitFade} from './anim';
import {ensureFonts} from './fonts';
import type {CardProps} from './props';
import {colors, fitSize, fonts, onAccent, safeAccent, useUnit} from './theme';

ensureFonts();

export const metaLine = (p: CardProps) =>
  [p.gameDate, p.opponent ? `vs ${p.opponent}` : ''].filter(Boolean).join('  ·  ');

export const Intro: React.FC<CardProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const {u, vertical, width} = useUnit();
  const accent = safeAccent(p.accent);
  const name = (p.playerName || 'Highlights').toUpperCase();
  const team = (p.teamName || '').toUpperCase();
  const num = (p.playerNumber || '').replace(/^#/, '');
  const meta = metaLine(p);

  const a1 = enter(frame, fps, 4);
  const a2 = enter(frame, fps, 10);
  const a3 = enter(frame, fps, 16);
  const bar = interpolate(frame, [8, 26], [0, 1], clamp);
  const numIn = enter(frame, fps, 0, 26);
  const out = exitFade(frame, durationInFrames, 10);

  const margin = (vertical ? 80 : 120) * u;
  const textWidth = vertical ? width - margin * 2 : width * 0.62;
  const nameSize = fitSize(name, (vertical ? 190 : 210) * u, textWidth, 0.47);

  return (
    <AbsoluteFill style={{opacity: out}}>
      <PitchBackground accent={accent} />
      {num ? (
        <div
          style={{
            position: 'absolute',
            right: vertical ? undefined : 90 * u,
            left: vertical ? margin - 20 * u : undefined,
            top: vertical ? 230 * u : undefined,
            bottom: vertical ? undefined : -90 * u,
            fontFamily: fonts.display,
            fontWeight: 900,
            fontSize: (vertical ? 620 : 760) * u,
            lineHeight: 1,
            color: 'transparent',
            WebkitTextStroke: `${6 * u}px ${accent}`,
            opacity: 0.9 * numIn,
            transform: `translateY(${(1 - numIn) * 80 * u}px)`,
          }}
        >
          {num}
        </div>
      ) : null}
      <div
        style={{
          position: 'absolute',
          left: margin,
          right: margin,
          bottom: vertical ? 360 * u : 150 * u,
          display: 'flex',
          flexDirection: 'column',
          gap: 18 * u,
        }}
      >
        {meta ? (
          <div
            style={{
              fontFamily: fonts.body,
              fontWeight: 700,
              fontSize: 34 * u,
              letterSpacing: 4 * u,
              textTransform: 'uppercase',
              color: colors.muted,
              opacity: a1,
              transform: `translateX(${(1 - a1) * -40 * u}px)`,
            }}
          >
            {meta}
          </div>
        ) : null}
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 900,
            fontSize: nameSize,
            lineHeight: 0.9,
            color: colors.text,
            opacity: a2,
            transform: `translateX(${(1 - a2) * -60 * u}px)`,
            whiteSpace: 'nowrap',
          }}
        >
          {name}
        </div>
        <div style={{display: 'flex', alignItems: 'center', gap: 24 * u}}>
          <div
            style={{
              height: 12 * u,
              width: 160 * u * bar,
              backgroundColor: accent,
              borderRadius: 6 * u,
            }}
          />
          {team ? (
            <div
              style={{
                fontFamily: fonts.display,
                fontWeight: 700,
                fontSize: 64 * u,
                letterSpacing: 2 * u,
                color: colors.text,
                opacity: a3,
              }}
            >
              {team}
            </div>
          ) : null}
          {num ? (
            <div
              style={{
                fontFamily: fonts.display,
                fontWeight: 800,
                fontSize: 48 * u,
                padding: `${4 * u}px ${18 * u}px`,
                borderRadius: 10 * u,
                backgroundColor: accent,
                color: onAccent(accent),
                opacity: a3,
              }}
            >
              #{num}
            </div>
          ) : null}
        </div>
      </div>
    </AbsoluteFill>
  );
};
