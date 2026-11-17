import {interpolate, spring} from 'remotion';

export const enter = (frame: number, fps: number, delay = 0, damping = 18) =>
  spring({frame: frame - delay, fps, config: {damping, mass: 0.8}});

/** 1 -> 0 over the last `frames` frames of the composition. */
export const exitFade = (frame: number, total: number, frames = 12) =>
  interpolate(frame, [total - frames, total], [1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

export const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;
