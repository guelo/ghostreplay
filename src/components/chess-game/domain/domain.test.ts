import { describe, expect, it } from "vitest";
import {
  deriveAnnotatedMoves,
  deriveBlunderArrows,
  deriveLastMoveSquares,
  type BlunderAlert,
  type MoveRecord,
  type ReviewFailInfo,
} from "./movePresentation";
import { deriveDisplayedOpening } from "./opening";
import { buildSessionMoveUploads, parseUciToSan } from "./sessionUpload";
import { deriveGameStatusBadge, deriveStatusText } from "./status";

describe("chess-game domain helpers", () => {
  it("derives status text from chess state", () => {
    expect(
      deriveStatusText({
        isCheckmate: () => true,
        isDraw: () => false,
        isGameOver: () => true,
        inCheck: () => false,
        turn: () => "w",
      }),
    ).toBe("Black wins by checkmate");

    expect(
      deriveStatusText({
        isCheckmate: () => false,
        isDraw: () => false,
        isGameOver: () => false,
        inCheck: () => true,
        turn: () => "b",
      }),
    ).toBe("Black to move (check)");
  });

  it("derives game status badge for active and completed games", () => {
    expect(deriveGameStatusBadge(true, null)).toEqual({
      label: "Live",
      className: "game-status-badge--live",
    });

    expect(
      deriveGameStatusBadge(false, {
        type: "resign",
        message: "You resigned.",
      }),
    ).toEqual({
      label: "Resigned",
      className: "game-status-badge--other",
    });
  });

  it("derives move highlights and annotations", () => {
    const moveHistory: MoveRecord[] = [
      {
        san: "e4",
        fen: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        uci: "e2e4",
      },
      {
        san: "d5",
        fen: "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2",
        uci: "d7d5",
      },
    ];

    const analysisMap = new Map([
      [
        0,
        {
          id: "a0",
          move: "e2e4",
          bestMove: "e2e4",
          bestEval: 34,
          playedEval: 34,
          currentPositionEval: 34,
          moveIndex: 0,
          delta: 0,
          blunder: false,
          recordable: false,
        },
      ],
      [
        1,
        {
          id: "a1",
          move: "d7d5",
          bestMove: "d7d5",
          bestEval: 20,
          playedEval: 12,
          currentPositionEval: 12,
          moveIndex: 1,
          delta: 8,
          blunder: false,
          recordable: false,
        },
      ],
    ]);

    expect(Object.keys(deriveLastMoveSquares(moveHistory, null)).sort()).toEqual([
      "d5",
      "d7",
    ]);
    expect(Object.keys(deriveLastMoveSquares(moveHistory, 0)).sort()).toEqual([
      "e2",
      "e4",
    ]);

    const annotatedMoves = deriveAnnotatedMoves(moveHistory, analysisMap);
    expect(annotatedMoves).toEqual([
      { san: "e4", classification: "best", eval: 34 },
      { san: "d5", classification: "excellent", eval: -12 },
    ]);
  });

  it("derives blunder arrows preferring review-fail arrows over toast arrows", () => {
    const reviewFail: ReviewFailInfo = {
      userMoveSan: "Nf3",
      bestMoveSan: "Nc3",
      userMoveUci: "g1f3",
      bestMoveUci: "b1c3",
      evalLoss: 50,
      moveIndex: 2,
    };
    const blunderAlert: BlunderAlert = {
      moveSan: "e4",
      moveUci: "e2e4",
      bestMoveUci: "d2d4",
      bestMoveSan: "d4",
      delta: 120,
    };

    expect(deriveBlunderArrows(reviewFail, blunderAlert)).toEqual([
      {
        startSquare: "g1",
        endSquare: "f3",
        color: "rgba(248, 113, 113, 0.8)",
      },
      {
        startSquare: "b1",
        endSquare: "c3",
        color: "rgba(52, 211, 153, 0.8)",
      },
    ]);

    expect(deriveBlunderArrows(null, blunderAlert)).toEqual([
      {
        startSquare: "e2",
        endSquare: "e4",
        color: "rgba(248, 113, 113, 0.8)",
      },
      {
        startSquare: "d2",
        endSquare: "d4",
        color: "rgba(52, 211, 153, 0.8)",
      },
    ]);
  });

  it("derives displayed opening from navigation index with fallback", () => {
    const c20 = { eco: "C20", name: "King's Pawn Game", source: "eco" } as const;
    const c50 = { eco: "C50", name: "Italian Game", source: "eco" } as const;
    const history = [c20, null, c50];

    expect(deriveDisplayedOpening(history, null)).toEqual(c50);
    expect(deriveDisplayedOpening(history, -1)).toEqual(c20);
    expect(deriveDisplayedOpening(history, 0)).toEqual(c20);
    expect(deriveDisplayedOpening([], null)).toBeNull();
  });

  it("parses UCI to SAN and builds session upload payload", () => {
    const startingFen =
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
    const moveHistory: MoveRecord[] = [
      {
        san: "e4",
        fen: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        uci: "e2e4",
      },
      {
        san: "d5",
        fen: "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2",
        uci: "d7d5",
      },
    ];
    const analyses = new Map([
      [
        0,
        {
          id: "a0",
          move: "e2e4",
          bestMove: "e2e4",
          bestEval: 25,
          playedEval: 25,
          currentPositionEval: 25,
          moveIndex: 0,
          delta: 0,
          blunder: false,
          recordable: false,
        },
      ],
      [
        1,
        {
          id: "a1",
          move: "d7d5",
          bestMove: "d7d5",
          bestEval: 16,
          playedEval: 8,
          currentPositionEval: 8,
          moveIndex: 1,
          delta: 8,
          blunder: false,
          recordable: false,
        },
      ],
    ]);

    expect(parseUciToSan(startingFen, "e2e4")).toBe("e4");
    expect(parseUciToSan(startingFen, "bad")).toBeNull();

    expect(buildSessionMoveUploads(moveHistory, analyses, startingFen)).toEqual([
      {
        move_number: 1,
        color: "white",
        move_san: "e4",
        fen_after:
          "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        eval_cp: 25,
        eval_mate: null,
        best_move_san: "e4",
        best_move_eval_cp: 25,
        eval_delta: 0,
        classification: "best",
        fen_before: startingFen,
        move_uci: "e2e4",
        best_move_uci: "e2e4",
      },
      {
        move_number: 1,
        color: "black",
        move_san: "d5",
        fen_after:
          "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2",
        eval_cp: 8,
        eval_mate: null,
        best_move_san: "d5",
        best_move_eval_cp: 16,
        eval_delta: 8,
        classification: "excellent",
        fen_before:
          "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        move_uci: "d7d5",
        best_move_uci: "d7d5",
      },
    ]);
  });
});
