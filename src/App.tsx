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
          Haunting in progress
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
          <span className="home-demo__callout-icon">💀</span>
          <span>
            <strong>Blunder spotted</strong>
            Your knight wandered off.
          </span>
        </div>

        <div className="home-demo__callout home-demo__callout--review">
          <span className="home-demo__callout-icon">👻</span>
          <span>
            <strong>Ghost queued</strong>
            This position will return to haunt you.
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
          <div className="home-hero__drift" aria-hidden="true">
            <span>♞</span>
            <span>♟</span>
            <span>♝</span>
            <span>♜</span>
            <span>♛</span>
            <span>♚</span>
            <span className="ecto-particle" />
            <span className="ecto-particle" />
            <span className="ecto-particle" />
            <span className="ecto-particle" />
            <span className="ecto-particle" />
            <span className="ecto-particle" />
          </div>
          <div className="home-hero__copy">
            <p className="home-kicker">
              <span aria-hidden="true">👻</span> Your blunders never really die{" "}
              <span aria-hidden="true">— they haunt.</span>
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
              <li>Haunted by your own blunders</li>
              <li>Stockfish-powered exorcism</li>
              <li>Spaced repetition magic</li>
            </ul>
          </div>

          <TrainingBoard />
        </section>

        <section
          className="home-drill-spotlight"
          aria-labelledby="home-drill-title"
        >
          <div className="home-drill-spotlight__ghosts" aria-hidden="true">
            <span>♟</span>
            <span>♞</span>
            <span>♛</span>
            <span>♝</span>
            <span>♚</span>
          </div>

          <div className="home-drill-spotlight__copy">
            <p className="home-kicker">
              <span aria-hidden="true">⚡</span> The marquee feature{" "}
              <span aria-hidden="true">— it's electric (but also haunted)</span>
            </p>
            <h2 id="home-drill-title">
              How long can you play <em>perfect</em> chess?
            </h2>
            <p className="home-drill-spotlight__lede">
              Opening Drills replay your real games move by move. Stay on the
              best line and your streak grows. Slip once and the ghost pounces —
              then files that exact position for a rematch.
            </p>

            <div className="home-drill-spotlight__stats" aria-hidden="true">
              <span className="home-drill-stat">
                <strong>17</strong> move streak
              </span>
              <span className="home-drill-stat">
                <strong>98%</strong> book accuracy
              </span>
              <span className="home-drill-stat home-drill-stat--ghost">
                <strong>👻 1</strong> ghost lurking
              </span>
            </div>

            <Link
              to="/openings"
              className="chess-button primary home-button home-drill-spotlight__cta"
            >
              Start an opening drill <span aria-hidden="true">→</span>
            </Link>
          </div>

          <div className="home-drill-spotlight__panel" aria-hidden="true">
            <div className="home-drill-spotlight__panel-head">
              <span>Drilling: Sicilian Defense</span>
              <span className="home-drill-spotlight__perfect">HAUNTED</span>
            </div>
            <ol className="home-drill-moves">
              <li className="home-drill-move home-drill-move--good">
                <span>1. e4 c5</span>
                <span aria-hidden="true">✓</span>
              </li>
              <li className="home-drill-move home-drill-move--good">
                <span>2. Nf3 d6</span>
                <span aria-hidden="true">✓</span>
              </li>
              <li className="home-drill-move home-drill-move--good">
                <span>3. d4 cxd4</span>
                <span aria-hidden="true">✓</span>
              </li>
              <li className="home-drill-move home-drill-move--live">
                <span>4. Nxd4 ...</span>
                <span className="home-drill-move__cursor">♟</span>
              </li>
            </ol>
            <div className="home-drill-spotlight__streak">
              <span className="home-drill-spotlight__flame" aria-hidden="true">
                🔥
              </span>
              Streak alive — your move
            </div>
          </div>
        </section>

        <section className="home-features" aria-labelledby="home-features-title">
          <div className="home-section-heading">
            <p className="home-kicker">One loop. More ways to improve.</p>
            <h2 id="home-features-title">
              Your chess history becomes a haunted training ground.
            </h2>
            <p>
              Practice the whole game or zoom in on one stubborn ghost. Every
              mode feeds the same goal: make the better move feel like muscle memory.
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
                <span className="home-feature__tag">Opening crypt</span>
              </div>
              <h3>Drill the lines that keep haunting you.</h3>
              <p>
                See which families cost you games, explore every branch, and
                launch a drill from the exact line that needs exorcism.
              </p>
              <div className="home-feature__pills" aria-hidden="true">
                <span>Repertoire map</span>
                <span>Branch stats</span>
                <span>Perfect-line drills</span>
              </div>
              <span className="home-feature__link">Enter the opening crypt →</span>
              <div className="home-feature__orbit" aria-hidden="true">
                <span>♙</span>
                <span>♟</span>
                <span>♘</span>
              </div>
            </Link>

            <Link to="/play" className="home-feature home-feature--play">
              <div className="home-feature__topline">
                <span className="home-feature__icon" aria-hidden="true">
                  👻
                </span>
                <span className="home-feature__tag">Ghost games</span>
              </div>
              <h3>Play against your past.</h3>
              <p>
                The ghost opponent steers toward positions from your own games, so
                practice feels like chess instead of a worksheet.
              </p>
              <span className="home-feature__link">Summon a ghost game →</span>
            </Link>

            <Link
              to="/blunders"
              className="home-feature home-feature--blunders"
            >
              <div className="home-feature__topline">
                <span className="home-feature__icon" aria-hidden="true">
                  💀
                </span>
                <span className="home-feature__tag">Blunder graveyard</span>
              </div>
              <h3>Review the right mistake at the right time.</h3>
              <p>
                A due queue uses spaced repetition to keep shaky decisions from
                rising from the dead.
              </p>
              <span className="home-feature__link">Visit the graveyard →</span>
            </Link>

            <Link
              to="/history"
              className="home-feature home-feature--history"
            >
              <span className="home-feature__icon" aria-hidden="true">
                📜
              </span>
              <h3>Game film</h3>
              <p>Reopen any session and walk through the turning points — the ghost remembers everything.</p>
              <span className="home-feature__link">Browse history →</span>
            </Link>

            <Link
              to="/history"
              className="home-feature home-feature--analysis"
            >
              <span className="home-feature__icon" aria-hidden="true">
                🔍
              </span>
              <h3>Move-by-move forensics</h3>
              <p>
                Accuracy, evaluations, best moves, and classifications stay
                attached to the game — like a haunting you can learn from.
              </p>
              <span className="home-feature__link">Review a game →</span>
            </Link>

            <Link to="/stats" className="home-feature home-feature--stats">
              <span className="home-feature__icon" aria-hidden="true">
                📈
              </span>
              <h3>Ghosts be gone</h3>
              <p>
                Track rating, accuracy, and ACPL trends as your ghosts get exorcised one by one.
              </p>
              <span className="home-feature__link">See your stats →</span>
            </Link>
          </div>
        </section>

        <section className="home-loop" aria-labelledby="home-loop-title">
          <div className="home-loop__intro">
            <p className="home-kicker">
              <span aria-hidden="true">🔄</span> The haunting loop
            </p>
            <h2 id="home-loop-title">Play. Get haunted. Exorcise.</h2>
            <p>
              No generic puzzle pile. Ghost Replay turns your own decisions into
              a practice plan that keeps evolving — and the ghost always remembers.
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
                <p>Finish a game while the ghost watches from the shadows.</p>
              </div>
            </li>
            <li>
              <span className="home-loop__number">02</span>
              <span className="home-loop__glyph" aria-hidden="true">
                👻
              </span>
              <div>
                <h3>The ghost takes notes</h3>
                <p>
                  Your blunders are saved — position, opening, best response. They don't forget.
                </p>
              </div>
            </li>
            <li>
              <span className="home-loop__number">03</span>
              <span className="home-loop__glyph" aria-hidden="true">
                ⚔️
              </span>
              <div>
                <h3>Face your ghost</h3>
                <p>
                  Replay the moment in a ghost game. Get it right and the haunting fades. Get it wrong and it returns stronger.
                </p>
              </div>
            </li>
          </ol>
        </section>

        <section className="home-final-cta">
          <div className="home-final-cta__mascot" aria-hidden="true">
            <span className="home-final-cta__boo">Boo!</span>
            <img src="/branding/ghost-logo-option-3-wink.svg" alt="" />
          </div>
          <div>
            <p className="home-kicker">
              <span aria-hidden="true">👻</span> Your ghost is waiting
            </p>
            <h2>Ready to haunt your bad habits?</h2>
            <p>
              Start a game. We'll remember the useful parts.{" "}
              <em>The ghost always does.</em>
            </p>
          </div>
          <Link to="/play" className="chess-button primary home-button">
            Summon a ghost game <span aria-hidden="true">→</span>
          </Link>
        </section>
      </div>
    </main>
  );
}

export default App;
