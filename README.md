# Ghost Replay Client

This repository contains the Ghost Replay web client. It is a React + Vite (TypeScript) workspace that future tasks will build upon for the Milestone 1 experience described in `SPEC.md`.

The starter UI renders a simple landing hero so you can confirm the correct app is running before new features are layered in.

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

The default dev command prints the local URL in the terminal. Open it in a browser and you should see the Ghost Replay placeholder copy along with the feature highlight cards.

## Project structure

- `src/main.tsx` mounts React and wires up the global styles
- `src/App.tsx` renders the placeholder hero, feature cards, and status message
- `src/App.css` + `src/index.css` define the temporary theme and layout
- `public/` holds assets that should be copied verbatim into the build output
- `vite.config.ts`, `tsconfig.*.json`, and `eslint.config.js` configure the tooling

## Next steps

With the scaffold landed you can now:

1. Add routing/state management for the upcoming chessboard views
2. Layer in API calls to the coordinator service once it exists
3. Replace the placeholder hero with real Ghost Replay interaction flows
