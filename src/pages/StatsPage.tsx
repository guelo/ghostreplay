import { Link } from "react-router-dom";
import { useAuth } from "../contexts/useAuth";
import "../App.css";

function StatsPage() {
  const { user, logout } = useAuth();

  return (
    <main className="app-shell">
      <nav className="nav-bar">
        <Link to="/" className="nav-bar__brand">
          Ghost Replay
        </Link>
        <div className="nav-bar__actions">
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
        <section className="stats-shell">
          <h1 className="stats-shell__title">Your Stats</h1>
          <p className="stats-shell__placeholder">
            Stats and graphs coming soon.
          </p>
          <Link to="/" className="chess-button primary">
            Back to Game
          </Link>
        </section>
      </div>
    </main>
  );
}

export default StatsPage;
