import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
const App = lazy(() => import("./App"));
const AuthForm = lazy(() => import("./components/AuthForm"));
const BlundersPage = lazy(() => import("./pages/BlundersPage"));
const GameAnalysisPage = lazy(() => import("./pages/GameAnalysisPage"));
const DrillAnalysisPage = lazy(() => import("./pages/DrillAnalysisPage"));
const HistoryPage = lazy(() => import("./pages/HistoryPage"));
const GamePage = lazy(() => import("./pages/GamePage"));
const OpeningsPage = lazy(() => import("./pages/OpeningsPage"));
const StatsPage = lazy(() => import("./pages/StatsPage"));

function AppRoutes() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route path="/" element={<App />} />
        <Route path="/play" element={<GamePage />} />
        <Route path="/game" element={<GameAnalysisPage />} />
        <Route path="/drill-analysis" element={<DrillAnalysisPage />} />
        <Route path="/login" element={<AuthForm mode="login" />} />
        <Route path="/register" element={<AuthForm mode="register" />} />
        <Route path="/history" element={<HistoryPage />} />
        <Route path="/blunders" element={<BlundersPage />} />
        <Route path="/openings" element={<OpeningsPage />} />
        <Route path="/stats" element={<StatsPage />} />
      </Routes>
    </Suspense>
  );
}

export default AppRoutes;
