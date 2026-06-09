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

const boardFiles = ["a", "b", "c", "d", "e"];
const boardRanks = [5, 4, 3, 2, 1];

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
        <div className="home-demo__board-frame">
          <div className="home-demo__ranks">
            {boardRanks.map((rank) => (
              <span key={rank}>{rank}</span>
            ))}
          </div>
          <div className="home-demo__board">
            {Array.from({ length: 25 }, (_, index) => (
              <span
                className={`home-demo__square${
                  index === 12 ? " home-demo__square--target" : ""
                }`}
                key={index}
              >
                {boardPieces[index]}
                {index === 12 && (
                  <span className="home-demo__spectre">♞</span>
                )}
              </span>
            ))}
            <span className="home-demo__arrow">↗</span>
          </div>
          <div className="home-demo__files">
            {boardFiles.map((file) => (
              <span key={file}>{file}</span>
            ))}
          </div>
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
      <span className="home-wanderer" aria-hidden="true">
        👻
      </span>
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
          <div className="home-hero__intro">
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
            <ul className="home-hero__proof" aria-label="Training highlights">
              <li>Haunted by your own blunders</li>
              <li>Stockfish-powered exorcism</li>
              <li>Spaced repetition magic</li>
            </ul>
          </div>

          <div className="home-portals">
            <Link to="/play" className="home-portal home-portal--ghost">
              <div className="home-portal__head">
                <span className="home-portal__tag">
                  <span aria-hidden="true">👻</span> Ghost games
                </span>
                <h2>Play against your past.</h2>
                <p>
                  The ghost steers every game toward positions from your own
                  history — your second chance, disguised as a fresh game.
                </p>
              </div>
              <TrainingBoard />
              <span className="home-portal__cta chess-button primary home-button">
                Summon a ghost game <span aria-hidden="true">→</span>
              </span>
            </Link>

            <Link to="/openings" className="home-portal home-portal--drill">
              <div className="home-portal__ghosts" aria-hidden="true">
                <span>♛</span>
                <span>♞</span>
              </div>
              <div className="home-portal__head">
                <span className="home-portal__tag home-portal__tag--electric">
                  <span aria-hidden="true">⚡</span> The challenge
                </span>
                <h2>
                  How long can you play <em>perfect</em> chess?
                </h2>
                <p>
                  Opening drills replay your real games move by move. Stay on
                  the best line and the streak grows. Slip once — the ghost
                  pounces.
                </p>
              </div>

              <div className="home-portal__panel" aria-hidden="true">
                <div className="home-portal__panel-head">
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
                  <li className="home-drill-move home-drill-move--live">
                    <span>3. d4 ...</span>
                    <span className="home-drill-move__cursor">♟</span>
                  </li>
                </ol>
                <div className="home-portal__streak">
                  <span className="home-drill-spotlight__flame" aria-hidden="true">
                    🔥
                  </span>
                  <strong>17-move streak</strong> alive — your move
                </div>
              </div>

              <div className="home-portal__stats" aria-hidden="true">
                <span className="home-drill-stat">
                  <strong>98%</strong> book accuracy
                </span>
                <span className="home-drill-stat home-drill-stat--ghost">
                  <strong>👻 1</strong> ghost lurking
                </span>
              </div>

              <span className="home-portal__cta chess-button home-button">
                Start an opening drill <span aria-hidden="true">→</span>
              </span>
            </Link>
          </div>
        </section>

        <section className="home-ticker" aria-label="How Ghost Replay trains you">
          <div className="home-ticker__track">
            {[false, true].map((isClone) => (
              <ul
                className="home-ticker__group"
                aria-hidden={isClone || undefined}
                key={isClone ? "clone" : "original"}
              >
                <li>
                  <span aria-hidden="true">👻</span> Every blunder is saved
                  with its best reply
                </li>
                <li>
                  <span aria-hidden="true">♞</span> Ghost games steer into
                  positions from your real games
                </li>
                <li>
                  <span aria-hidden="true">🔮</span> Spaced repetition decides
                  when a ghost returns
                </li>
                <li>
                  <span aria-hidden="true">♛</span> Opening drills replay your
                  own games move by move
                </li>
                <li>
                  <span aria-hidden="true">⚙️</span> Stockfish grades every
                  move you make
                </li>
                <li>
                  <span aria-hidden="true">💀</span> ?? is chess notation for a
                  blunder — we collect those
                </li>
              </ul>
            ))}
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
            <p className="home-loop__legend">
              In chess notation <strong>??</strong> marks a blunder and{" "}
              <strong>!!</strong> a brilliancy. This loop walks you from one to
              the other.
            </p>
          </div>

          <ol className="home-loop__steps">
            <li>
              <span className="home-loop__number">1. e4</span>
              <span className="home-loop__glyph" aria-hidden="true">
                ♟
              </span>
              <div>
                <h3>Play naturally</h3>
                <p>Finish a game while the ghost watches from the shadows.</p>
              </div>
            </li>
            <li>
              <span className="home-loop__number home-loop__number--blunder">
                2. ??
              </span>
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
              <span className="home-loop__number home-loop__number--brilliant">
                3. !!
              </span>
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

        <section className="home-faq" aria-labelledby="home-faq-title">
          <div className="home-faq__intro">
            <p className="home-kicker">
              <span aria-hidden="true">🕯️</span> Frequently asked hauntings
            </p>
            <h2 id="home-faq-title">Questions from beyond the board.</h2>
            <p>
              Everything you need to know before your first séance. The ghost
              answered these personally.
            </p>
            <img
              className="home-faq__mascot"
              src="/branding/ghost-logo-option-4-scholar.svg"
              alt=""
            />
          </div>

          <div className="home-faq__list">
            <details className="home-faq__item" open>
              <summary>
                <span className="home-faq__move" aria-hidden="true">
                  1.
                </span>
                What exactly is a ghost game?
                <span className="home-faq__ghost" aria-hidden="true">
                  👻
                </span>
              </summary>
              <p>
                A full game against an opponent built from your own history. The
                ghost steers play toward positions where you went wrong before,
                so you get a natural second chance at the exact decisions that
                cost you — no flashcards, just chess.
              </p>
            </details>

            <details className="home-faq__item">
              <summary>
                <span className="home-faq__move" aria-hidden="true">
                  2.
                </span>
                Where do my blunders come from?
                <span className="home-faq__ghost" aria-hidden="true">
                  👻
                </span>
              </summary>
              <p>
                Every game you play here is analyzed by Stockfish. When a move
                loses serious ground, the position is filed away with the
                opening it came from and the move you should have played. That
                file is your personal haunting list.
              </p>
            </details>

            <details className="home-faq__item">
              <summary>
                <span className="home-faq__move" aria-hidden="true">
                  3.
                </span>
                How do opening drills work?
                <span className="home-faq__ghost" aria-hidden="true">
                  👻
                </span>
              </summary>
              <p>
                Pick an opening family from your repertoire map and replay your
                own games move by move. Match the best line and your streak
                grows; deviate and the drill stops you on the spot, shows the
                better move, and queues the position for review.
              </p>
            </details>

            <details className="home-faq__item">
              <summary>
                <span className="home-faq__move" aria-hidden="true">
                  4.
                </span>
                When do ghosts come back to haunt me?
                <span className="home-faq__ghost" aria-hidden="true">
                  👻
                </span>
              </summary>
              <p>
                On a spaced-repetition schedule. Positions you fumble return
                quickly; positions you nail come back later and later, until the
                ghost finally gives up and moves on. That's the exorcism.
              </p>
            </details>

            <details className="home-faq__item">
              <summary>
                <span className="home-faq__move" aria-hidden="true">
                  5.
                </span>
                Do I need an account to get haunted?
                <span className="home-faq__ghost" aria-hidden="true">
                  👻
                </span>
              </summary>
              <p>
                You can start playing as a guest right away — the ghost starts
                taking notes immediately. Register whenever you want to keep
                your hauntings, stats, and streaks safe across sessions.
              </p>
            </details>
          </div>
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
