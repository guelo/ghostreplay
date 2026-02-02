import ChessGame from "./components/ChessGame";
import "./App.css";

const featureHighlights = [
  {
    title: "Ghost opponent",
    description:
      "Relive critical mistakes on demand. The engine forces the same positions so habits get rebuilt under pressure.",
  },
  {
    title: "Spaced repetition",
    description:
      "Mistakes cool down only after you prove you can avoid them. The tougher the blunder, the sooner it returns.",
  },
  {
    title: "Live feedback",
    description:
      "Stockfish watches every move so you know immediately whether the leak is fixed or needs more reps.",
  },
];

function App() {
  return (
    <main className="app-shell">
      <div className="constrained-content">
        <section className="hero">
          <p className="eyebrow">Ghost Replay</p>
          <h1>Face the blunder. Fix the player.</h1>
        </section>
      </div>

      <ChessGame />
    </main>
  );
}

export default App;
