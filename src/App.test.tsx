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
        name: /your blunders are coming back\. good\./i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: /how long can you keep the perfect line alive/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", { name: /start an opening drill/i }),
    ).toHaveLength(2);

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
