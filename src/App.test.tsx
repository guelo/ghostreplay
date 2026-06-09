import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AuthContextValue } from "./contexts/authContextShared";
import { render, screen } from "./test/utils";
import App from "./App";

const authState: AuthContextValue = {
  user: { id: 1, username: "guest", isAnonymous: true },
  token: "token",
  isLoading: false,
  error: null,
  login: vi.fn(),
  logout: vi.fn(),
  claimAccount: vi.fn(),
};

vi.mock("./contexts/useAuth", () => ({
  useAuth: () => authState,
}));

describe("App landing page", () => {
  it("exposes the complete training workflow", () => {
    const { container } = render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", {
        name: /turn every blunder into your next best move/i,
      }),
    ).toBeInTheDocument();

    const destinations = Array.from(
      container.querySelectorAll<HTMLAnchorElement>("a[href]"),
      (link) => link.getAttribute("href"),
    );

    expect(destinations).toEqual(
      expect.arrayContaining([
        "/play",
        "/openings",
        "/blunders",
        "/history",
        "/stats",
      ]),
    );
  });
});
