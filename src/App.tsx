import { Link } from "react-router-dom";
import AppNav from "./components/AppNav";
import "./App.css";

function App() {
  return (
    <main className="app-shell home-page">
      <AppNav showLogo />

      <div className="constrained-content">
        <section className="hero">
          <p className="eyebrow">Ghost Replay</p>
          <h1>Face the Blunder. Fix the Player.</h1>
          <p>
            Train by revisiting your real mistakes. Play a game, trigger your
            ghost lines, and build better habits move by move.
          </p>
          <div className="cta-row">
            <Link to="/game" className="chess-button primary">
              Play a Game
            </Link>
            <Link to="/history" className="chess-button">
              Review History
            </Link>
          </div>
        </section>

        <section className="feature-grid home-page__features">
          <article className="feature-card">
            <h2>Ghost Practice</h2>
            <p>
              The opponent steers into positions where you previously blundered.
            </p>
          </article>
          <article className="feature-card">
            <h2>Live Analysis</h2>
            <p>
              Every player move is analyzed so mistakes are captured with
              context.
            </p>
          </article>
          <article className="feature-card">
            <h2>Session Review</h2>
            <p>
              Jump into history and stats to inspect patterns and track
              progress.
            </p>
          </article>
        </section>
      </div>
    </main>
  );
}

export default App;
