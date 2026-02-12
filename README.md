# Ghost Replay Client

This repository contains the Ghost Replay web client. It is a React + Vite (TypeScript) workspace that future tasks will build upon for the Milestone 1 experience described in `SPEC.md`.

The app now separates a landing experience (`/`) from the playable game page (`/game`).

## Prerequisites

- Node.js 20+
- npm 10+

## Getting started

```bash
npm install         # install dependencies and create node_modules
npm run dev         # start the Vite dev server on http://localhost:5173
npm run lint        # run the ESLint flat config
npm run build       # compile TypeScript and create a production build
npm run preview     # preview the production build locally
```

The default dev command prints the local URL in the terminal. Open it in a browser and you should see the home page at `/`. Use the **Play a Game** CTA (or navigate to `/game`) to launch gameplay.

## Verifying the chess game

The client now bundles [`react-chessboard`](https://www.npmjs.com/package/react-chessboard) and [`chess.js`](https://www.npmjs.com/package/chess.js) so you can rehearse both sides of the board locally.

1. Run `npm run dev` and open the printed URL.
2. Go to `/game` (or click **Play a Game** from home).
3. Make a few legal moves; the turn indicator should update after every drop.
4. Try an illegal move (for example, move a knight like a bishop). The piece should snap back immediately.
5. Click **Flip board** to play from the opposite side or toggle **Auto-rotate** to have the view swap each move.
6. Use **Reset game** after a full play-through to return to the initial position.

## Project structure

- `src/main.tsx` mounts React and wires up the top-level providers/router
- `src/AppRoutes.tsx` defines route mappings (`/`, `/game`, `/history`, auth pages)
- `src/App.tsx` renders the home/landing page
- `src/pages/GamePage.tsx` hosts the live gameplay view
- `src/components/ChessGame.tsx` powers the local chess sandbox with react-chessboard + chess.js
- `src/openings/openingBook.ts` loads the vendored ECO opening dataset + precomputed position index with caching
- `scripts/build-opening-position-index.mjs` regenerates the precomputed opening position index
- `src/App.css` + `src/index.css` define the temporary theme and layout
- `public/` holds assets that should be copied verbatim into the build output
- `public/data/openings/eco.json` is the full local ECO opening book (no runtime network dependency)
- `public/data/openings/eco.byPosition.json` is the precomputed normalized-position lookup index
- `vite.config.ts`, `tsconfig.*.json`, and `eslint.config.js` configure the tooling

## Next steps

With the scaffold landed you can now:

1. Continue improving route-level UX between home, game, and review pages
2. Layer in API calls to the coordinator service once it exists
3. Keep iterating on the home/game visual split based on design feedback
