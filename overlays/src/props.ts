// JSON props shared by every card. All fields optional so a partial
// overlays.json still renders; the Python side validates before calling us.
export type CardProps = {
  teamName?: string;
  playerName?: string;
  playerNumber?: string;
  gameDate?: string;
  opponent?: string;
  accent?: string;
  /** Title card text, ONLY ever set from the user's own instruction. */
  title?: string;
  subtitle?: string;
  homeLabel?: string;
  awayLabel?: string;
  homeScore?: number | string;
  awayScore?: number | string;
  outroLine?: string;
  durationInSeconds?: number;
};

export const sampleProps: CardProps = {
  teamName: 'Test FC',
  playerName: 'Test Player',
  playerNumber: '10',
  gameDate: 'Apr 25, 2026',
  opponent: 'Sample United',
  accent: '#FFC41F',
  title: 'Goal!',
  subtitle: 'Test Player #10',
  homeLabel: 'TFC',
  awayLabel: 'SMP',
  homeScore: 2,
  awayScore: 1,
  outroLine: 'Thanks for watching',
};
