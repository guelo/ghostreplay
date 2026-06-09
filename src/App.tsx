import { Link } from "react-router-dom";
import AppNav from "./components/AppNav";
import "./App.css";

function App() {
  return (
    <main className="app-shell home-page">
      <AppNav showLogo />

      <div className="constrained-content">
        <section className="hero hero--ghost">
          <div className="hero__ghost" aria-hidden="true">
            <div className="ghost-sprite">
              <div className="ghost-sprite__eye ghost-sprite__eye--left" />
              <div className="ghost-sprite__eye ghost-sprite__eye--right" />
              <div className="ghost-sprite__mouth" />
            </div>
            <div className="ghost-sprite__shadow" />
          </div>

          <p className="eyebrow">👻 Ghost Replay</p>
          <h1>
            Face the Blunder.
            <br />
            <span className="hero__accent">Fix the Player.</span>
          </h1>
          <p className="hero__lede">
            Your past mistakes don't disappear — they come back as ghosts. Drill
            the exact openings where you go wrong, replay your real blunders, and
            exorcise them one move at a time.
          </p>
          <div className="cta-row">
            <Link to="/openings" className="chess-button primary hero__cta-drill">
              🎯 Start an Opening Drill
            </Link>
            <Link to="/play" className="chess-button">
              Play a Game
            </Link>
          </div>
        </section>

        <section className="feature-spotlight">
          <div className="feature-spotlight__badge">Featured Mode</div>
          <div className="feature-spotlight__body">
            <h2>Opening Drill Mode 👻♟️</h2>
            <p>
              Pick any opening from your repertoire and the ghost steers every
              game straight into it. Repeat the lines you keep flubbing until the
              right move is muscle memory — no more wandering into the same trap.
            </p>
            <Link to="/openings" className="chess-button primary">
              Browse Openings &amp; Drill →
            </Link>
          </div>
          <div className="feature-spotlight__art" aria-hidden="true">
            <span className="floating-piece floating-piece--1">♟</span>
            <span className="floating-piece floating-piece--2">♞</span>
            <span className="floating-piece floating-piece--3">♝</span>
          </div>
        </section>

        <section className="feature-grid home-page__features">
          <Link to="/play" className="feature-card feature-card--link">
            <span className="feature-card__emoji" aria-hidden="true">
              🕹️
            </span>
            <h2>Ghost Practice</h2>
            <p>
              The opponent possesses your old games and steers into positions
              where you previously blundered.
            </p>
            <span className="feature-card__cta">Play now →</span>
          </Link>

          <Link to="/blunders" className="feature-card feature-card--link">
            <span className="feature-card__emoji" aria-hidden="true">
              💀
            </span>
            <h2>Blunder Graveyard</h2>
            <p>
              Every shaky move is analyzed and buried here with full context —
              revisit them and lay them to rest.
            </p>
            <span className="feature-card__cta">View blunders →</span>
          </Link>

          <Link to="/stats" className="feature-card feature-card--link">
            <span className="feature-card__emoji" aria-hidden="true">
              📈
            </span>
            <h2>Track the Haunting</h2>
            <p>
              Accuracy, ACPL, and rating trends so you can watch your ghosts
              fade as your play sharpens.
            </p>
            <span className="feature-card__cta">See stats →</span>
          </Link>

          <Link to="/history" className="feature-card feature-card--link">
            <span className="feature-card__emoji" aria-hidden="true">
              📜
            </span>
            <h2>Replay History</h2>
            <p>
              Step back through every session move by move and inspect the
              patterns behind your mistakes.
            </p>
            <span className="feature-card__cta">Open history →</span>
          </Link>
        </section>

        <section className="how-it-works">
          <h2 className="how-it-works__title">How the haunting works</h2>
          <ol className="how-it-works__steps">
            <li>
              <span className="how-step__num">1</span>
              <div>
                <strong>Play</strong>
                <p>Every move you make gets analyzed in the background.</p>
              </div>
            </li>
            <li>
              <span className="how-step__num">2</span>
              <div>
                <strong>Blunder</strong>
                <p>Your mistakes are captured and turned into ghost lines.</p>
              </div>
            </li>
            <li>
              <span className="how-step__num">3</span>
              <div>
                <strong>Drill</strong>
                <p>Face those exact positions again until you nail them.</p>
              </div>
            </li>
          </ol>
        </section>
      </div>
    </main>
  );
}

export default App;
