import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../contexts/useAuth";

type AppNavProps = {
  showLogo?: boolean;
};

const routeActions = [
  { to: "/", label: "Home" },
  { to: "/play", label: "Play" },
  { to: "/history", label: "History" },
  { to: "/blunders", label: "Blunders" },
  { to: "/openings", label: "Openings" },
  { to: "/stats", label: "Stats" },
] as const;

function AppNav({ showLogo = false }: AppNavProps) {
  const { user, logout } = useAuth();
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const drawerRef = useRef<HTMLDivElement | null>(null);
  const drawerCloseButtonRef = useRef<HTMLButtonElement | null>(null);
  const drawerId = "app-nav-mobile-drawer";

  const closeMenu = useCallback(() => {
    setIsMenuOpen(false);
    menuButtonRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!isMenuOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeMenu();
        return;
      }

      if (event.key !== "Tab" || !drawerRef.current) {
        return;
      }

      const focusableElements = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      const firstElement = focusableElements[0];
      const lastElement = focusableElements[focusableElements.length - 1];

      if (!firstElement || !lastElement) {
        return;
      }

      const activeElement = document.activeElement;
      if (
        event.shiftKey &&
        (activeElement === firstElement ||
          !drawerRef.current.contains(activeElement))
      ) {
        event.preventDefault();
        lastElement.focus();
        return;
      }

      if (!event.shiftKey && activeElement === lastElement) {
        event.preventDefault();
        firstElement.focus();
      }
    };

    drawerCloseButtonRef.current?.focus();
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [closeMenu, isMenuOpen]);

  const handleMobileLogout = () => {
    logout();
    closeMenu();
  };

  const renderActions = (variant: "desktop" | "mobile") => {
    const linkClassName =
      variant === "desktop" ? "nav-bar__link" : "nav-drawer__link";
    const buttonClassName =
      variant === "desktop" ? "nav-bar__link" : "nav-drawer__link";
    const registerClassName =
      variant === "desktop"
        ? "chess-button primary nav-bar__btn"
        : "chess-button primary nav-drawer__btn";
    const onNavigate = variant === "mobile" ? closeMenu : undefined;

    return (
      <>
        {routeActions.map((action) => (
          <Link
            key={action.to}
            to={action.to}
            className={linkClassName}
            onClick={onNavigate}
          >
            {action.label}
          </Link>
        ))}
        {user?.isAnonymous ? (
          <>
            <Link
              to="/register"
              className={registerClassName}
              onClick={onNavigate}
            >
              Register
            </Link>
            <Link to="/login" className={linkClassName} onClick={onNavigate}>
              Log in
            </Link>
          </>
        ) : (
          <>
            <span
              className={
                variant === "desktop" ? "nav-bar__user" : "nav-drawer__user"
              }
            >
              {user?.username}
            </span>
            <button
              className={buttonClassName}
              type="button"
              onClick={variant === "mobile" ? handleMobileLogout : logout}
            >
              Log out
            </button>
          </>
        )}
      </>
    );
  };

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
      <button
        ref={menuButtonRef}
        className="nav-bar__menu-button"
        type="button"
        aria-label="Open navigation menu"
        aria-controls={drawerId}
        aria-expanded={isMenuOpen}
        onClick={() => setIsMenuOpen(true)}
      >
        <span aria-hidden="true" />
        <span aria-hidden="true" />
        <span aria-hidden="true" />
      </button>
      <div className="nav-bar__actions">
        {renderActions("desktop")}
      </div>
      {isMenuOpen && (
        <div className="nav-drawer-shell" role="presentation">
          <button
            className="nav-drawer-backdrop"
            type="button"
            aria-label="Close navigation menu"
            tabIndex={-1}
            onClick={closeMenu}
          />
          <div
            ref={drawerRef}
            id={drawerId}
            className="nav-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="Navigation menu"
          >
            <div className="nav-drawer__header">
              <span className="nav-drawer__title">Ghost Replay</span>
              <button
                ref={drawerCloseButtonRef}
                className="nav-drawer__close"
                type="button"
                aria-label="Close navigation menu"
                onClick={closeMenu}
              >
                ×
              </button>
            </div>
            <div className="nav-drawer__actions">{renderActions("mobile")}</div>
          </div>
        </div>
      )}
    </nav>
  );
}

export default AppNav;
