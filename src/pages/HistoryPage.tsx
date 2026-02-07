import { Link, useLocation } from "react-router-dom";
import { useAuth } from "../contexts/useAuth";
import "../App.css";

function HistoryPage() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const state = location.state as {
    select?: "latest";
    source?: string;
  } | null;

  return (
    <main className="app-shell">
      <nav className="nav-bar">
        <Link to="/" className="nav-bar__brand">
          Ghost Replay
        </Link>
        <div className="nav-bar__actions">
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
        <section className="history-shell">
          <h1 className="history-shell__title">Game History</h1>
          {state?.select === "latest" && (
            <p className="history-shell__hint">
              {state.source === "post_game_view_analysis"
                ? "Showing analysis for your latest game."
                : "Browsing your game history."}
            </p>
          )}
          <p className="history-shell__placeholder">
            History and analysis coming soon.
          </p>
          <Link to="/" className="chess-button primary">
            Back to Game
          </Link>
        </section>
      </div>
    </main>
  );
}

export default HistoryPage;
