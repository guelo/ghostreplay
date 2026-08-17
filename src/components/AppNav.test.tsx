import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { within } from "@testing-library/react";
import type { AuthContextValue } from "../contexts/authContextShared";
import { fireEvent, render, screen } from "../test/utils";
import AppNav from "./AppNav";
import { openFeedbackWidget } from "../analytics/featurebase";

vi.mock("../analytics/featurebase", () => ({
  openFeedbackWidget: vi.fn(),
}));

const authState = vi.hoisted(() => ({
  current: {
    user: { id: 1, username: "guest", isAnonymous: true },
    token: "token",
    isLoading: false,
    error: null,
    login: vi.fn(),
    logout: vi.fn(),
    claimAccount: vi.fn(),
  } as AuthContextValue,
}));

vi.mock("../contexts/useAuth", () => ({
  useAuth: () => authState.current,
}));

const renderNav = () =>
  render(
    <MemoryRouter>
      <AppNav />
    </MemoryRouter>,
  );

// jsdom loads the component stylesheet but does not evaluate its mobile media
// query, so the menu button keeps its desktop display:none base. Query by the
// explicit label to exercise drawer behavior without treating jsdom's missing
// responsive layout as an accessibility result.
const getMenuButton = () =>
  screen.getByLabelText(/open navigation menu/i) as HTMLButtonElement;

describe("AppNav", () => {
  beforeEach(() => {
    vi.mocked(openFeedbackWidget).mockClear();
    authState.current = {
      user: { id: 1, username: "guest", isAnonymous: true },
      token: "token",
      isLoading: false,
      error: null,
      login: vi.fn(),
      logout: vi.fn(),
      claimAccount: vi.fn(),
    };
  });

  it("opens the mobile drawer with the shared route actions", () => {
    renderNav();

    const menuButton = getMenuButton();
    expect(menuButton).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(menuButton);

    expect(menuButton).toHaveAttribute("aria-expanded", "true");
    const drawer = screen.getByRole("dialog", { name: /navigation menu/i });
    expect(
      within(drawer).getByRole("button", { name: /close navigation menu/i }),
    ).toHaveFocus();
    for (const label of [
      "Home",
      "Play",
      "History",
      "Openings",
      "Stats",
      "Register",
      "Log in",
    ]) {
      expect(drawer).toHaveTextContent(label);
    }
  });

  it("closes the drawer on route, backdrop, and Escape interactions", () => {
    const { container } = renderNav();
    const menuButton = getMenuButton();

    fireEvent.click(menuButton);
    fireEvent.click(
      screen.getByRole("dialog", { name: /navigation menu/i }).querySelector(
        'a[href="/play"]',
      )!,
    );
    expect(screen.queryByRole("dialog", { name: /navigation menu/i })).toBeNull();
    expect(document.activeElement).toBe(menuButton);

    fireEvent.click(menuButton);
    fireEvent.click(container.querySelector(".nav-drawer-backdrop")!);
    expect(screen.queryByRole("dialog", { name: /navigation menu/i })).toBeNull();
    expect(document.activeElement).toBe(menuButton);

    fireEvent.click(menuButton);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: /navigation menu/i })).toBeNull();
    expect(document.activeElement).toBe(menuButton);
  });

  it("logs out from the drawer and closes it", () => {
    authState.current = {
      ...authState.current,
      user: { id: 2, username: "player-one", isAnonymous: false },
      logout: vi.fn(),
    };
    renderNav();

    fireEvent.click(getMenuButton());
    const logoutButtons = screen.getAllByRole("button", { name: /log out/i });
    fireEvent.click(logoutButtons[logoutButtons.length - 1]);

    expect(authState.current.logout).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog", { name: /navigation menu/i })).toBeNull();
  });

  it("keeps the backdrop out of the tab order and traps focus in the drawer", () => {
    const { container } = renderNav();
    fireEvent.click(getMenuButton());

    const drawer = screen.getByRole("dialog", { name: /navigation menu/i });
    const closeButton = within(drawer).getByRole("button", {
      name: /close navigation menu/i,
    });
    const loginLink = within(drawer).getByRole("link", { name: /log in/i });

    expect(container.querySelector(".nav-drawer-backdrop")).toHaveAttribute(
      "tabindex",
      "-1",
    );

    closeButton.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(loginLink).toHaveFocus();

    fireEvent.keyDown(document, { key: "Tab" });
    expect(closeButton).toHaveFocus();
  });

  it("opens the feedback widget from the desktop nav", () => {
    renderNav();

    fireEvent.click(screen.getByRole("button", { name: /feedback/i }));

    expect(vi.mocked(openFeedbackWidget)).toHaveBeenCalledTimes(1);
  });

  it("opens the feedback widget from the mobile drawer and closes it", () => {
    renderNav();

    fireEvent.click(getMenuButton());
    const drawer = screen.getByRole("dialog", { name: /navigation menu/i });
    fireEvent.click(within(drawer).getByRole("button", { name: /feedback/i }));

    expect(vi.mocked(openFeedbackWidget)).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("dialog", { name: /navigation menu/i }),
    ).toBeNull();
  });
});
