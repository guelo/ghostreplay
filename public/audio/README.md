Place blunder audio clips in this folder using these exact names:

- `bestmove2.wav`
- `blunder1.m4a`
- `blunder2.m4a`
- `blunder3.m4a`
- `blunder4.m4a`
- `blunder5.m4a`
- `blunder6.m4a`
- `blunder7.m4a`
- `blunder8.m4a`
- `blunder9.m4a`
- `blunder10.m4a`

Repeat-mistake buzzer (optional):

- `buzzer.mp3` — a short, harsh error buzzer.

Move sounds:

- `move.m4a` — a soft sound for a regular (non-capturing) move.
- `take.m4a` — a distinct sound for a capture.

`ChessGame` picks one clip at random whenever a player blunder is detected.
`AnalysisEffects` plays `bestmove2.wav` whenever a player move resolves as the best move,
and plays `buzzer.mp3` when an SRS review repeats a past mistake (the full-screen
spotlight). If `buzzer.mp3` is missing, playback no-ops harmlessly.

`commitAppliedMove` plays a move sound on every committed move (player, engine,
and ghost), choosing `take.m4a` when `move.captured` is set and `move.m4a`
otherwise.
