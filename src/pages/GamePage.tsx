import { useNavigate } from "react-router-dom";
import ChessGame from "../components/ChessGame";
import AppNav from "../components/AppNav";
import "../App.css";

function GamePage() {
  const navigate = useNavigate();

  return (
    <main className="app-shell game-page">
      <AppNav />

      <div className="constrained-content game-page__intro">
        <section className="game-page__hero">
          <h1>Play a Training Game</h1>
          <p>
            Start a session against Maia, then review your critical positions in
            history.
          </p>
        </section>
      </div>

      <ChessGame
        onOpenHistory={(opts) => navigate("/history", { state: opts })}
      />
    </main>
  );
}

export default GamePage;
