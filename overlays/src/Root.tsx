import React from 'react';
import {CalculateMetadataFunction, Composition} from 'remotion';
import {Intro} from './Intro';
import {LowerThird} from './LowerThird';
import {Outro} from './Outro';
import {ScoreBar} from './ScoreBar';
import {TitleCard} from './TitleCard';
import {CardProps, sampleProps} from './props';
import {FPS} from './theme';

const formats = [
  {suffix: '', width: 1920, height: 1080},
  {suffix: '-Vertical', width: 1080, height: 1920},
];

const cards: Array<{id: string; component: React.FC<CardProps>; seconds: number}> = [
  {id: 'Intro', component: Intro, seconds: 5},
  {id: 'Outro', component: Outro, seconds: 4},
  {id: 'LowerThird', component: LowerThird, seconds: 5},
  {id: 'Title', component: TitleCard, seconds: 3},
  {id: 'ScoreBar', component: ScoreBar, seconds: 1},
];

// `durationInSeconds` in the props overrides the default length (1-20 s).
const withDuration =
  (fallback: number): CalculateMetadataFunction<CardProps> =>
  ({props}) => {
    const s = Number(props.durationInSeconds);
    const secs = Number.isFinite(s) && s >= 1 && s <= 20 ? s : fallback;
    return {durationInFrames: Math.round(secs * FPS)};
  };

export const RemotionRoot: React.FC = () => (
  <>
    {cards.flatMap((card) =>
      formats.map((f) => (
        <Composition
          key={card.id + f.suffix}
          id={card.id + f.suffix}
          component={card.component}
          durationInFrames={card.seconds * FPS}
          fps={FPS}
          width={f.width}
          height={f.height}
          defaultProps={sampleProps}
          calculateMetadata={withDuration(card.seconds)}
        />
      )),
    )}
  </>
);
