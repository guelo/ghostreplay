import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { flushSync } from "react-dom";

const mockLogout = vi.fn();
const getOpeningFamilyScoresMock = vi.fn();

vi.mock("../contexts/useAuth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      username: "tester",
      isAnonymous: false,
    },
    logout: mockLogout,
  }),
}));

vi.mock("../utils/api", async () => {
  const actual = await vi.importActual<typeof import("../utils/api")>(
    "../utils/api",
  );

  return {
    ...actual,
    getOpeningFamilyScores: (
      ...args: Parameters<typeof actual.getOpeningFamilyScores>
    ) => getOpeningFamilyScoresMock(...args),
  };
});

import AppRoutes from "../AppRoutes";
import OpeningsPage from "./OpeningsPage";

const whiteFamiliesResponse = {
  player_color: "white",
  total_families: 3,
  computed_at: "2026-03-30T12:00:00Z",
  families: [
    {
      family_name: "Sicilian Defense",
      root_count: 2,
      family_score: 44,
      family_confidence: 0.67,
      family_coverage: 0.36,
      root_sample_size_sum: 12,
      last_practiced_at: "2026-03-29T10:00:00Z",
      weakest_root_name: "Dragon Variation",
      weakest_root_score: 33,
    },
    {
      family_name: "French Defense",
      root_count: 3,
      family_score: 52,
      family_confidence: 0.82,
      family_coverage: 0.58,
      root_sample_size_sum: 26,
      last_practiced_at: "2026-03-27T10:00:00Z",
      weakest_root_name: "Winawer Variation",
      weakest_root_score: 33,
    },
    {
      family_name: "Caro-Kann Defense",
      root_count: 2,
      family_score: 49.2,
      family_confidence: 0.71,
      family_coverage: 0.44,
      root_sample_size_sum: 18,
      last_practiced_at: "2026-03-28T10:00:00Z",
      weakest_root_name: "Advance Variation",
      weakest_root_score: 41,
    },
  ],
} as const;

const blackFamiliesResponse = {
  player_color: "black",
  total_families: 1,
  computed_at: "2026-03-30T12:05:00Z",
  families: [
    {
      family_name: "King's Indian Defense",
      root_count: 2,
      family_score: 61,
      family_confidence: 0.74,
      family_coverage: 0.52,
      root_sample_size_sum: 21,
      last_practiced_at: "2026-03-28T08:00:00Z",
      weakest_root_name: "Classical Variation",
      weakest_root_score: 47,
    },
  ],
} as const;

function renderPage() {
  return render(
    <MemoryRouter>
      <OpeningsPage />
    </MemoryRouter>,
  );
}

function renderRoute(path = "/openings") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;

  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });

  return { promise, resolve, reject };
}

describe("OpeningsPage", () => {
  beforeEach(() => {
    getOpeningFamilyScoresMock.mockReset();
    mockLogout.mockReset();
  });

  it("registers a dedicated /openings route and nav link", async () => {
    getOpeningFamilyScoresMock.mockResolvedValueOnce(whiteFamiliesResponse);

    renderRoute("/openings");

    await waitFor(() => {
      expect(getOpeningFamilyScoresMock).toHaveBeenCalledWith("white");
    });

    expect(
      screen.getByRole("heading", { name: "Opening Families" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Openings" })).toHaveAttribute(
      "href",
      "/openings",
    );
    expect(screen.queryByText("Your Stats")).not.toBeInTheDocument();
  });

  it("shows a loading state before the first response", () => {
    getOpeningFamilyScoresMock.mockImplementation(
      () => new Promise(() => undefined),
    );

    renderPage();

    expect(screen.getByText("Loading opening families...")).toBeInTheDocument();
    expect(getOpeningFamilyScoresMock).toHaveBeenCalledWith("white");
  });

  it("renders populated family cards in backend order", async () => {
    getOpeningFamilyScoresMock.mockResolvedValueOnce(whiteFamiliesResponse);

    renderPage();

    const grid = await screen.findByRole("region", {
      name: "White opening families",
    });
    const headings = within(grid)
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);

    expect(headings).toEqual([
      "Sicilian Defense",
      "French Defense",
      "Caro-Kann Defense",
    ]);

    const firstCard = within(grid)
      .getByRole("heading", { name: "Sicilian Defense" })
      .closest("article");

    expect(firstCard).not.toBeNull();
    expect(within(firstCard!).getByText(/Weakest root:/)).toBeInTheDocument();
    expect(within(firstCard!).getByText("Dragon Variation")).toBeInTheDocument();
    expect(within(firstCard!).getByText("Samples")).toBeInTheDocument();
    expect(within(firstCard!).queryByText("Games")).not.toBeInTheDocument();
  });

  it("ignores a stale response that settles during a color switch", async () => {
    const whiteDeferred = createDeferred<typeof whiteFamiliesResponse>();
    const blackDeferred = createDeferred<typeof blackFamiliesResponse>();

    getOpeningFamilyScoresMock.mockImplementationOnce(() => whiteDeferred.promise);
    getOpeningFamilyScoresMock.mockImplementationOnce(() => blackDeferred.promise);

    renderPage();

    expect(getOpeningFamilyScoresMock).toHaveBeenCalledWith("white");

    flushSync(() => {
      fireEvent.click(screen.getByRole("button", { name: "Black" }));
    });

    expect(screen.getByRole("button", { name: "Black" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    whiteDeferred.resolve(whiteFamiliesResponse);
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.getByText("Loading opening families...")).toBeInTheDocument();
    expect(screen.queryByText("Sicilian Defense")).not.toBeInTheDocument();

    blackDeferred.resolve(blackFamiliesResponse);

    expect(
      await screen.findByRole("region", { name: "Black opening families" }),
    ).toBeInTheDocument();
    expect(screen.getByText("King's Indian Defense")).toBeInTheDocument();
  });

  it("shows the true no-evidence empty state when computed_at is null", async () => {
    getOpeningFamilyScoresMock.mockResolvedValueOnce({
      player_color: "white",
      families: [],
      total_families: 0,
      computed_at: null,
    });

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("No opening evidence for White yet."),
      ).toBeInTheDocument();
    });
  });

  it("shows the computed snapshot empty state when no families are returned", async () => {
    getOpeningFamilyScoresMock.mockResolvedValueOnce({
      player_color: "white",
      families: [],
      total_families: 0,
      computed_at: "2026-03-30T12:00:00Z",
    });

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("No scored opening families are available for White yet."),
      ).toBeInTheDocument();
    });
  });

  it("switches colors and refetches for the selected side", async () => {
    const user = userEvent.setup();
    getOpeningFamilyScoresMock.mockResolvedValueOnce(whiteFamiliesResponse);
    getOpeningFamilyScoresMock.mockResolvedValueOnce(blackFamiliesResponse);

    renderPage();

    await screen.findByRole("region", { name: "White opening families" });

    await user.click(screen.getByRole("button", { name: "Black" }));

    await waitFor(() => {
      expect(getOpeningFamilyScoresMock).toHaveBeenLastCalledWith("black");
    });

    expect(
      await screen.findByRole("region", { name: "Black opening families" }),
    ).toBeInTheDocument();
    expect(screen.getByText("King's Indian Defense")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Black" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows fetch failure and retry preserves the active color", async () => {
    const user = userEvent.setup();
    getOpeningFamilyScoresMock.mockResolvedValueOnce(whiteFamiliesResponse);
    getOpeningFamilyScoresMock.mockRejectedValueOnce(
      new Error("Opening family cache unavailable"),
    );
    getOpeningFamilyScoresMock.mockResolvedValueOnce(blackFamiliesResponse);

    renderPage();

    await screen.findByRole("region", { name: "White opening families" });

    await user.click(screen.getByRole("button", { name: "Black" }));

    await waitFor(() => {
      expect(
        screen.getByText("Opening family cache unavailable"),
      ).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Black" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("region", { name: "Black opening families" }),
    ).toBeInTheDocument();
    expect(getOpeningFamilyScoresMock).toHaveBeenNthCalledWith(1, "white");
    expect(getOpeningFamilyScoresMock).toHaveBeenNthCalledWith(2, "black");
    expect(getOpeningFamilyScoresMock).toHaveBeenNthCalledWith(3, "black");
  });
});
