import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {PitchBackground} from './Background';
import {clamp, enter, exitFade} from './anim';
import {ensureFonts} from './fonts';
import {metaLine} from './Intro';
import type {CardProps} from './props';
import {colors, fitSize, fonts, safeAccent, useUnit} from './theme';

ensureFonts();

export const Outro: React.FC<CardProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const {u, vertical, width} = useUnit();
  const accent = safeAccent(p.accent);
  const line = (p.outroLine || 'Thanks for watching').toUpperCase();
  const who = [p.playerName, p.playerNumber ? `#${p.playerNumber.replace(/^#/, '')}` : '']
    .filter(Boolean)
    .join(' ');
  const team = p.teamName || '';
  const meta = metaLine(p);
  const a1 = enter(frame, fps, 2);
  const a2 = enter(frame, fps, 10);
  const ring = interpolate(frame, [0, 30], [0, 1], clamp);
  const out = exitFade(frame, durationInFrames, 14);
  const maxW = width - (vertical ? 140 : 300) * u;

  return (
    <AbsoluteFill style={{opacity: out}}>
      <PitchBackground accent={accent} />
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
        <div
          style={{
            position: 'absolute',
            width: 760 * u,
            height: 760 * u,
            borderRadius: '50%',
            border: `${4 * u}px solid ${accent}`,
            opacity: 0.25 * ring,
            transform: `scale(${0.7 + 0.3 * ring})`,
          }}
        />
        <div style={{textAlign: 'center', display: 'flex', flexDirection: 'column', gap: 20 * u}}>
          <div
            style={{
              fontFamily: fonts.display,
              fontWeight: 900,
              fontSize: fitSize(line, 150 * u, maxW, 0.46),
              lineHeight: 0.95,
              color: colors.text,
              opacity: a1,
              transform: `translateY(${(1 - a1) * 40 * u}px)`,
            }}
          >
            {line}
          </div>
          {who || team ? (
            <div
              style={{
                fontFamily: fonts.body,
                fontWeight: 700,
                fontSize: 44 * u,
                color: accent,
                opacity: a2,
              }}
            >
              {[who, team].filter(Boolean).join('  ·  ')}
            </div>
          ) : null}
          {meta ? (
            <div
              style={{
                fontFamily: fonts.body,
                fontSize: 32 * u,
                letterSpacing: 3 * u,
                textTransform: 'uppercase',
                color: colors.muted,
                opacity: a2,
              }}
            >
              {meta}
            </div>
          ) : null}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
