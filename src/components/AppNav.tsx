import { Link } from "react-router-dom";
import { useAuth } from "../contexts/useAuth";

type AppNavProps = {
  showLogo?: boolean;
};

function AppNav({ showLogo = false }: AppNavProps) {
  const { user, logout } = useAuth();

  return (
    <nav className="nav-bar">
      <Link
        to="/"
        className={`nav-bar__brand${showLogo ? " nav-bar__brand--logo" : ""}`}
      >
        {showLogo ? (
          <>
            <img
              className="nav-bar__logo"
              src="/branding/ghost-logo-option-4-scholar.svg"
              alt="Ghost Replay logo"
            />
            <span>Ghost Replay</span>
          </>
        ) : (
          "Ghost Replay"
        )}
      </Link>
      <div className="nav-bar__actions">
        <Link to="/" className="nav-bar__link">
          Home
        </Link>
        <Link to="/game" className="nav-bar__link">
          Game
        </Link>
        <Link to="/history" className="nav-bar__link">
          History
        </Link>
        <Link to="/stats" className="nav-bar__link">
          Stats
        </Link>
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
            <button className="nav-bar__link" type="button" onClick={logout}>
              Log out
            </button>
          </>
        )}
      </div>
    </nav>
  );
}

export default AppNav;
