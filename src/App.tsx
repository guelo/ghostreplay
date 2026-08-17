import { Link } from "react-router-dom";
import AppNav from "./components/AppNav";
import "./pages/HomePage.css";

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

const trainingSteps = [
  {
    move: "1. PLAY",
    icon: "♟",
    title: "Play a real game",
    description:
      "Make your moves normally. Stockfish watches quietly from the attic.",
  },
  {
    move: "2. ??",
    icon: "👻",
    title: "A ghost is born",
    description:
      "A costly decision becomes a personal case file, complete with the better move.",
  },
  {
    move: "3. !!",
    icon: "⚔",
    title: "Face it again",
    description:
      "Future games and reviews lead you back. Get it right until the haunting fades.",
  },
] as const;

const hauntRooms = [
  {
    to: "/blunders",
    eyebrow: "Due from beyond",
    icon: "☠",
    title: "Review the ghost that needs you now.",
    description:
      "Spaced repetition brings shaky positions back before they disappear from memory.",
    cta: "Review due blunders",
    className: "home-room--blunders",
    detail: (
      <div className="home-room__due" aria-hidden="true">
        <strong>3</strong>
        <span>spirits due today</span>
      </div>
    ),
  },
  {
    to: "/history",
    eyebrow: "Evidence room",
    icon: "⌕",
    title: "Revisit every turning point.",
    description:
      "Open past games with evaluations, best lines, and the moment the board changed.",
    cta: "Browse the evidence",
    className: "home-room--history",
  },
  {
    to: "/stats",
    eyebrow: "Paranormal activity",
    icon: "↗",
    title: "Watch your ghosts lose their grip.",
    description:
      "Track rating, accuracy, and cleaner decisions as old mistakes stop returning.",
    cta: "See the readings",
    className: "home-room--stats",
  },
] as const;

function TrainingBoard() {
  return (
    <div className="home-demo" aria-hidden="true">
      <div className="home-demo__topbar">
        <span className="home-demo__live">
          <span />
          Case file open
        </span>
        <span>GR-024 · Sicilian</span>
      </div>

      <div className="home-demo__stage">
        <div className="home-demo__moon" />
        <span className="home-demo__boo">pssst...</span>
        <img
          className="home-demo__mascot"
          src="/branding/ghost-logo-option-3-wink-glasses.svg"
          alt=""
        />

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
                {index === 12 && <span className="home-demo__spectre">♞</span>}
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

        <div className="home-demo__callout home-demo__callout--blunder">
          <span className="home-demo__callout-icon">??</span>
          <span>
            <strong>Past you played Nf3</strong>
            The knight never came home.
          </span>
        </div>

        <div className="home-demo__callout home-demo__callout--review">
          <span className="home-demo__callout-icon">!!</span>
          <span>
            <strong>Your move, again</strong>
            This time the ghost brought a hint.
          </span>
        </div>
      </div>

      <div className="home-demo__footer">
        <span>Memory recovered</span>
        <strong>Better move: d4</strong>
      </div>
    </div>
  );
}

function DrillConsole() {
  return (
    <div className="home-drill-console" aria-hidden="true">
      <div className="home-drill-console__topbar">
        <span>
          <i />
          Drilling: Sicilian Defense
        </span>
        <strong>HAUNTED LINE</strong>
      </div>

      <div className="home-drill-console__score">
        <div>
          <span>Perfect streak</span>
          <strong>17</strong>
          <small>moves and counting</small>
        </div>
        <div className="home-drill-console__spirit">
          <img
            src="/branding/ghost-logo-option-3-wink-glasses.svg"
            alt=""
          />
          <span>still watching</span>
        </div>
      </div>

      <ol className="home-drill-console__moves">
        <li>
          <span>1. e4</span>
          <span>c5</span>
          <strong>Perfect</strong>
        </li>
        <li>
          <span>2. Nf3</span>
          <span>d6</span>
          <strong>Perfect</strong>
        </li>
        <li className="home-drill-console__move--active">
          <span>3. d4</span>
          <span>...</span>
          <strong>Your move</strong>
        </li>
      </ol>

      <div className="home-drill-console__footer">
        <span>
          <strong>98%</strong> book accuracy
        </span>
        <span>
          <strong>👻 1</strong> ghost lurking
        </span>
      </div>
    </div>
  );
}

function App() {
  return (
    <main className="app-shell home-page">
      <AppNav showLogo />

      <div className="constrained-content home-page__content">
        <section className="home-hero home-hero--twin">
          <div className="home-hero__atmosphere" aria-hidden="true">
            <span>♞</span>
            <span>✦</span>
            <span>♟</span>
            <span>✧</span>
          </div>

          <div className="home-hero__intro">
            <p className="home-kicker">
              <span aria-hidden="true">👻</span> Personal chess training with
              unfinished business
            </p>
            <h1>
              Two ways to outplay{" "}
              <span className="home-hero__accent">past you.</span>
            </h1>
            <p className="home-hero__lede">
              Ghost Replay turns your own games into training: it haunts you with
              the blunders you keep making, and drills the openings you want to
              own — until both become second nature.
            </p>
          </div>

          <div className="home-twin">
            <article className="home-feature home-feature--ghost">
              <div className="home-feature__head">
                <span className="home-feature__badge" aria-hidden="true">
                  👻
                </span>
                <div>
                  <p className="home-feature__eyebrow">Ghost Replay</p>
                  <h2>Your blunders come back to haunt you.</h2>
                </div>
              </div>
              <p className="home-feature__lede">
                Every costly move becomes a personal case file. We sneak the
                position back into future games until the better move sticks.
              </p>

              <div className="home-feature__visual">
                <span className="home-hero__scribble" aria-hidden="true">
                  it remembers →
                </span>
                <TrainingBoard />
              </div>

              <ul className="home-feature__proof" aria-label="Ghost Replay highlights">
                <li>Built from your games</li>
                <li>Engine checked</li>
                <li>Spaced just right</li>
              </ul>

              <Link to="/play" className="chess-button primary home-button">
                Summon a ghost game <span aria-hidden="true">→</span>
              </Link>
            </article>

            <article className="home-feature home-feature--drill">
              <div className="home-feature__head">
                <span className="home-feature__badge" aria-hidden="true">
                  ⚡
                </span>
                <div>
                  <p className="home-feature__eyebrow">Opening Drills</p>
                  <h2>The best way to learn an opening.</h2>
                </div>
              </div>
              <p className="home-feature__lede">
                Replay your repertoire move by move. Every best move grows the
                streak; one slip wakes the ghost. Lines turn into instinct.
              </p>

              <div className="home-feature__visual">
                <span className="home-hero__scribble" aria-hidden="true">
                  keep the streak →
                </span>
                <DrillConsole />
              </div>

              <ul className="home-feature__proof" aria-label="Opening Drills highlights">
                <li>Drill any branch</li>
                <li>Instant feedback</li>
                <li>Beat your best streak</li>
              </ul>

              <Link
                to="/openings"
                className="chess-button home-button home-feature__cta-drill"
              >
                Start an opening drill <span aria-hidden="true">→</span>
              </Link>
            </article>
          </div>
        </section>

        <section className="home-process" aria-labelledby="home-process-title">
          <div className="home-section-heading home-section-heading--compact">
            <p className="home-kicker">
              <span aria-hidden="true">🔮</span> The whole haunting
            </p>
            <h2 id="home-process-title">One loop. Three moves.</h2>
            <p>
              No generic puzzle pile. Your own decisions become the training
              plan.
            </p>
          </div>

          <ol className="home-process__steps">
            {trainingSteps.map((step) => (
              <li key={step.move}>
                <span className="home-process__move">{step.move}</span>
                <span className="home-process__icon" aria-hidden="true">
                  {step.icon}
                </span>
                <h3>{step.title}</h3>
                <p>{step.description}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="home-rooms" aria-labelledby="home-rooms-title">
          <div className="home-section-heading">
            <p className="home-kicker">
              <span aria-hidden="true">🕯</span> Pick a room
            </p>
            <h2 id="home-rooms-title">Keep digging through the evidence.</h2>
            <p>
              Review what is due, reopen a full game, or watch your long-term
              progress.
            </p>
          </div>

          <div className="home-rooms__grid">
            {hauntRooms.map((room) => (
              <Link
                to={room.to}
                className={`home-room ${room.className}`}
                key={room.to}
              >
                <div className="home-room__topline">
                  <span className="home-room__icon" aria-hidden="true">
                    {room.icon}
                  </span>
                  <span>{room.eyebrow}</span>
                </div>
                <h3>{room.title}</h3>
                <p>{room.description}</p>
                {"detail" in room ? room.detail : null}
                <span className="home-room__link">
                  {room.cta} <span aria-hidden="true">→</span>
                </span>
              </Link>
            ))}
          </div>
        </section>

        <section className="home-final-cta">
          <div className="home-final-cta__mascot" aria-hidden="true">
            <span className="home-final-cta__boo">Your move.</span>
            <img src="/branding/ghost-logo-option-3-wink.svg" alt="" />
          </div>
          <div>
            <p className="home-kicker">The board has a long memory</p>
            <h2>Give your ghosts a rematch.</h2>
            <p>Play one game. We’ll handle the unfinished business.</p>
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
