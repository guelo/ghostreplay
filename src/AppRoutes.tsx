import { Route, Routes } from "react-router-dom";
import App from "./App";
import AuthForm from "./components/AuthForm";
import BlundersPage from "./pages/BlundersPage";
import HistoryPage from "./pages/HistoryPage";
import GamePage from "./pages/GamePage";
import OpeningsPage from "./pages/OpeningsPage";
import StatsPage from "./pages/StatsPage";

function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<App />} />
      <Route path="/game" element={<GamePage />} />
      <Route path="/login" element={<AuthForm mode="login" />} />
      <Route path="/register" element={<AuthForm mode="register" />} />
      <Route path="/history" element={<HistoryPage />} />
      <Route path="/blunders" element={<BlundersPage />} />
      <Route path="/openings" element={<OpeningsPage />} />
      <Route path="/stats" element={<StatsPage />} />
    </Routes>
  );
}

export default AppRoutes;
