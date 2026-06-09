import { Link } from "react-router-dom";
import AppNav from "./components/AppNav";
import "./App.css";

const boardPieces: Record<number, string> = {
  1: "♜",
  3: "♚",
  5: "♟",
  7: "♞",
  9: "♟",
  16: "♙",
  18: "♘",
  20: "♙",
  21: "♖",
  23: "♔",
};

function TrainingBoard() {
  return (
    <div className="home-demo" aria-hidden="true">
      <div className="home-demo__topbar">
        <span className="home-demo__live">
          <span />
          Live training
        </span>
        <span>Ghost line #24</span>
      </div>

      <div className="home-demo__stage">
        <div className="home-demo__board">
          {Array.from({ length: 25 }, (_, index) => (
            <span
              className={`home-demo__square${
                index === 12 ? " home-demo__square--target" : ""
              }`}
              key={index}
            >
              {boardPieces[index]}
            </span>
          ))}
          <span className="home-demo__arrow">↗</span>
        </div>

        <img
          className="home-demo__mascot"
          src="/branding/ghost-logo-option-3-wink-glasses.svg"
          alt=""
        />

        <div className="home-demo__callout home-demo__callout--blunder">
          <span className="home-demo__callout-icon">!</span>
          <span>
            <strong>Blunder spotted</strong>
            Your knight wandered off.
          </span>
        </div>

        <div className="home-demo__callout home-demo__callout--review">
          <span className="home-demo__callout-icon">✓</span>
          <span>
            <strong>Review queued</strong>
            This position will return.
          </span>
        </div>
      </div>
    </div>
  );
}

function App() {
  return (
    <main className="app-shell home-page">
      <AppNav showLogo />

      <div className="constrained-content home-page__content">
        <section className="home-hero">
          <div className="home-hero__copy">
            <p className="home-kicker">
              <span aria-hidden="true">✦</span> Your mistakes want a rematch
            </p>
            <h1>
              Turn every blunder into your{" "}
              <span className="home-hero__accent">next best move.</span>
            </h1>
            <p className="home-hero__lede">
              Ghost Replay remembers where your games went sideways, then brings
              those moments back as personalized games, drills, and spaced
              reviews.
            </p>

            <div className="home-hero__actions">
              <Link to="/play" className="chess-button primary home-button">
                Play a ghost game <span aria-hidden="true">→</span>
              </Link>
              <Link to="/openings" className="chess-button home-button">
                Explore opening drills
              </Link>
            </div>

            <ul className="home-hero__proof" aria-label="Training highlights">
              <li>Built from your games</li>
              <li>Stockfish-powered analysis</li>
              <li>Spaced repetition</li>
            </ul>
          </div>

          <TrainingBoard />
        </section>

        <section className="home-features" aria-labelledby="home-features-title">
          <div className="home-section-heading">
            <p className="home-kicker">One loop. More ways to improve.</p>
            <h2 id="home-features-title">
              Your chess history becomes a training ground.
            </h2>
            <p>
              Practice the whole game or zoom in on one stubborn pattern. Every
              mode feeds the same goal: make the better move feel familiar.
            </p>
          </div>

          <div className="home-bento">
            <Link
              to="/openings"
              className="home-feature home-feature--openings"
            >
              <div className="home-feature__topline">
                <span className="home-feature__icon" aria-hidden="true">
                  ♞
                </span>
                <span className="home-feature__tag">Opening lab</span>
              </div>
              <h3>Build a repertoire that survives contact.</h3>
              <p>
                See which families cost you games, explore every branch, and
                launch a drill from the exact line that needs work.
              </p>
              <div className="home-feature__pills" aria-hidden="true">
                <span>Repertoire map</span>
                <span>Branch stats</span>
                <span>Targeted drills</span>
              </div>
              <span className="home-feature__link">Enter the opening lab →</span>
              <div className="home-feature__orbit" aria-hidden="true">
                <span>♙</span>
                <span>♟</span>
                <span>♘</span>
              </div>
            </Link>

            <Link to="/play" className="home-feature home-feature--play">
              <div className="home-feature__topline">
                <span className="home-feature__icon" aria-hidden="true">
                  ◉
                </span>
                <span className="home-feature__tag">Ghost games</span>
              </div>
              <h3>Play against your past.</h3>
              <p>
                The opponent steers toward positions from your own games, so
                practice feels like chess instead of a worksheet.
              </p>
              <span className="home-feature__link">Start a game →</span>
            </Link>

            <Link
              to="/blunders"
              className="home-feature home-feature--blunders"
            >
              <div className="home-feature__topline">
                <span className="home-feature__icon" aria-hidden="true">
                  ✦
                </span>
                <span className="home-feature__tag">Blunder inbox</span>
              </div>
              <h3>Review the right mistake at the right time.</h3>
              <p>
                A due queue uses spaced repetition to keep shaky decisions from
                fading back into bad habits.
              </p>
              <span className="home-feature__link">Review due positions →</span>
            </Link>

            <Link
              to="/history"
              className="home-feature home-feature--history"
            >
              <span className="home-feature__icon" aria-hidden="true">
                ↺
              </span>
              <h3>Game film</h3>
              <p>Reopen any session and walk through the turning points.</p>
              <span className="home-feature__link">Browse history →</span>
            </Link>

            <Link
              to="/history"
              className="home-feature home-feature--analysis"
            >
              <span className="home-feature__icon" aria-hidden="true">
                ∿
              </span>
              <h3>Move-by-move analysis</h3>
              <p>
                Accuracy, evaluations, best moves, and classifications stay
                attached to the game that produced them.
              </p>
              <span className="home-feature__link">Review a game →</span>
            </Link>

            <Link to="/stats" className="home-feature home-feature--stats">
              <span className="home-feature__icon" aria-hidden="true">
                ↗
              </span>
              <h3>Progress you can see</h3>
              <p>
                Track rating, accuracy, and ACPL trends as your repeat mistakes
                start disappearing.
              </p>
              <span className="home-feature__link">See your stats →</span>
            </Link>
          </div>
        </section>

        <section className="home-loop" aria-labelledby="home-loop-title">
          <div className="home-loop__intro">
            <p className="home-kicker">The training loop</p>
            <h2 id="home-loop-title">Play. Catch it. Replay it.</h2>
            <p>
              No generic puzzle pile. Ghost Replay turns your own decisions into
              a practice plan that keeps evolving with you.
            </p>
          </div>

          <ol className="home-loop__steps">
            <li>
              <span className="home-loop__number">01</span>
              <span className="home-loop__glyph" aria-hidden="true">
                ♟
              </span>
              <div>
                <h3>Play naturally</h3>
                <p>Finish a game while analysis runs quietly in the background.</p>
              </div>
            </li>
            <li>
              <span className="home-loop__number">02</span>
              <span className="home-loop__glyph" aria-hidden="true">
                !
              </span>
              <div>
                <h3>Catch the pattern</h3>
                <p>
                  Blunders are saved with their position, opening, and best
                  response.
                </p>
              </div>
            </li>
            <li>
              <span className="home-loop__number">03</span>
              <span className="home-loop__glyph" aria-hidden="true">
                ↺
              </span>
              <div>
                <h3>Meet it again</h3>
                <p>
                  Replay the moment in a ghost game or clear it from your review
                  queue.
                </p>
              </div>
            </li>
          </ol>
        </section>

        <section className="home-final-cta">
          <img
            src="/branding/ghost-logo-option-4-scholar.svg"
            alt=""
            aria-hidden="true"
          />
          <div>
            <p className="home-kicker">Your next move is waiting</p>
            <h2>Ready to haunt your bad habits?</h2>
            <p>Start a game. We’ll remember the useful parts.</p>
          </div>
          <Link to="/play" className="chess-button primary home-button">
            Start playing <span aria-hidden="true">→</span>
          </Link>
        </section>
      </div>
    </main>
  );
}

export default App;
