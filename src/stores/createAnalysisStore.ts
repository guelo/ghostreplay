import { createContext, useContext } from "react";
import { createStore, useStore } from "zustand";
import type { AnalysisResult } from "../hooks/useMoveAnalysis";

type AnalysisStatus = "booting" | "ready" | "error";

export type AnalysisStoreState = {
  analysisMap: Map<number, AnalysisResult>;
  lastAnalysis: AnalysisResult | null;
  streamingEval: { moveIndex: number; cp: number } | null;
  status: AnalysisStatus;
  error: string | null;
  isAnalyzing: boolean;
  analyzingMove: string | null;

  resolveAnalysis: (moveIndex: number, result: AnalysisResult) => void;
  setLastAnalysis: (result: AnalysisResult | null) => void;
  setStreamingEval: (
    value: { moveIndex: number; cp: number } | null,
  ) => void;
  setStatus: (status: AnalysisStatus) => void;
  setError: (error: string | null) => void;
  setIsAnalyzing: (value: boolean) => void;
  setAnalyzingMove: (move: string | null) => void;
  /** Reset worker-lifecycle state while preserving analysisMap. */
  resetTransient: () => void;
  /** Reset everything including analysisMap (for new game). */
  clearAll: () => void;
};

export type AnalysisStore = ReturnType<typeof createAnalysisStore>;

export const createAnalysisStore = () =>
  createStore<AnalysisStoreState>((set) => ({
    analysisMap: new Map(),
    lastAnalysis: null,
    streamingEval: null,
    status: "booting",
    error: null,
    isAnalyzing: false,
    analyzingMove: null,

    resolveAnalysis: (moveIndex, result) =>
      set((s) => {
        const next = new Map(s.analysisMap);
        next.set(moveIndex, result);
        return { analysisMap: next, lastAnalysis: result };
      }),
    setLastAnalysis: (result) => set({ lastAnalysis: result }),
    setStreamingEval: (value) => set({ streamingEval: value }),
    setStatus: (status) => set({ status }),
    setError: (error) => set({ error }),
    setIsAnalyzing: (value) => set({ isAnalyzing: value }),
    setAnalyzingMove: (move) => set({ analyzingMove: move }),
    resetTransient: () =>
      set({
        lastAnalysis: null,
        streamingEval: null,
        status: "booting",
        error: null,
        isAnalyzing: false,
        analyzingMove: null,
      }),
    clearAll: () =>
      set({
        analysisMap: new Map(),
        lastAnalysis: null,
        streamingEval: null,
        isAnalyzing: false,
        analyzingMove: null,
        error: null,
      }),
  }));

/** Singleton analysis store for the main game (ChessGame). */
export const gameAnalysisStore = createAnalysisStore();

const AnalysisStoreContext = createContext<AnalysisStore | null>(null);

export const AnalysisStoreProvider = AnalysisStoreContext.Provider;

export function useAnalysisStoreApi(): AnalysisStore {
  const store = useContext(AnalysisStoreContext);
  if (!store)
    throw new Error(
      "useAnalysisStore used outside AnalysisStoreProvider",
    );
  return store;
}

export function useAnalysisStore<T>(
  selector: (state: AnalysisStoreState) => T,
): T {
  return useStore(useAnalysisStoreApi(), selector);
}
