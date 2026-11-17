import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {enter} from './anim';
import {ensureFonts} from './fonts';
import type {CardProps} from './props';
import {colors, fonts, onAccent, safeAccent, useUnit} from './theme';

ensureFonts();

/** Transparent top-left score bug. Short slide-in; the compose step holds the last frame. */
export const ScoreBar: React.FC<CardProps> = (p) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const {u, vertical} = useUnit();
  const accent = safeAccent(p.accent);
  const inP = enter(frame, fps, 0, 20);
  const k = vertical ? 1.15 : 1;
  const h = 76 * u * k;
  const label = (t: string | undefined, fallback: string) => (t || fallback).toUpperCase().slice(0, 4);
  const cell = (text: string, bg: string, color: string, w: number) => (
    <div
      style={{
        minWidth: w,
        padding: `0 ${18 * u}px`,
        height: h,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: bg,
        color,
        fontFamily: fonts.display,
        fontWeight: 800,
        fontSize: h * 0.62,
        letterSpacing: 1 * u,
      }}
    >
      {text}
    </div>
  );
  return (
    <AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          top: (vertical ? 120 : 56) * u,
          left: (vertical ? 60 : 72) * u,
          display: 'flex',
          borderRadius: 12 * u,
          overflow: 'hidden',
          boxShadow: `0 ${6 * u}px ${20 * u}px rgba(0,0,0,0.4)`,
          opacity: inP,
          transform: `translateY(${(1 - inP) * -30 * u}px)`,
        }}
      >
        {cell(label(p.homeLabel, 'HOME'), accent, onAccent(accent), 110 * u * k)}
        {cell(`${p.homeScore ?? 0}`, colors.panelSolid, colors.text, 70 * u * k)}
        <div style={{width: 2 * u, backgroundColor: colors.line}} />
        {cell(`${p.awayScore ?? 0}`, colors.panelSolid, colors.text, 70 * u * k)}
        {cell(label(p.awayLabel, 'AWAY'), 'rgba(255,255,255,0.14)', colors.text, 110 * u * k)}
      </div>
    </AbsoluteFill>
  );
};
