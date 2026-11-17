import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';
import {colors} from './theme';

/** Dark pitch backdrop: soft vignette plus slowly drifting field stripes. */
export const PitchBackground: React.FC<{accent: string}> = ({accent}) => {
  const frame = useCurrentFrame();
  const drift = interpolate(frame, [0, 300], [0, -120]);
  return (
    <AbsoluteFill style={{backgroundColor: colors.ink, overflow: 'hidden'}}>
      <AbsoluteFill
        style={{
          backgroundImage:
            'repeating-linear-gradient(100deg, rgba(255,255,255,0.035) 0 120px, rgba(255,255,255,0) 120px 240px)',
          backgroundPosition: `${drift}px 0`,
        }}
      />
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse at 20% 110%, ${accent}33 0%, transparent 55%)`,
        }}
      />
      <AbsoluteFill
        style={{
          background:
            'radial-gradient(ellipse at 50% 40%, transparent 35%, rgba(0,0,0,0.65) 100%)',
        }}
      />
    </AbsoluteFill>
  );
};
