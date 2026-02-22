import { useNavigate } from "react-router-dom";
import ChessGame from "../components/ChessGame";
import AppNav from "../components/AppNav";
import "../App.css";

function GamePage() {
  const navigate = useNavigate();

  return (
    <main className="app-shell game-page">
      <AppNav />

      <ChessGame
        onOpenHistory={(opts) => navigate("/history", { state: opts })}
      />
    </main>
  );
}

export default GamePage;
