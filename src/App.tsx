import { Link, useNavigate } from "react-router-dom";
import ChessGame from "./components/ChessGame";
import { useAuth } from "./contexts/useAuth";
import "./App.css";

function App() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <main className="app-shell">
      <nav className="nav-bar">
        <span className="nav-bar__brand">Ghost Replay</span>
        <div className="nav-bar__actions">
          <Link to="/history" className="nav-bar__link">History</Link>
          <Link to="/stats" className="nav-bar__link">Stats</Link>
          {user?.isAnonymous ? (
            <>
              <Link to="/register" className="chess-button primary nav-bar__btn">
                Register
              </Link>
              <Link to="/login" className="nav-bar__link">
                Log in
              </Link>
            </>
          ) : (
            <>
              <span className="nav-bar__user">{user?.username}</span>
              <button
                className="nav-bar__link"
                type="button"
                onClick={logout}
              >
                Log out
              </button>
            </>
          )}
        </div>
      </nav>

      <div className="constrained-content">
        <section className="hero">
          <h1>Face the blunder. Fix the player.</h1>
        </section>
      </div>

      <ChessGame
        onOpenHistory={(opts) =>
          navigate("/history", { state: opts })
        }
      />
    </main>
  );
}

export default App;
