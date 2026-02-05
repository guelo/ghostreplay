import ChessGame from "./components/ChessGame";
import "./App.css";

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
