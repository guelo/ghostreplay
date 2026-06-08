import { Navigate } from "react-router-dom";
import AnalysisBoard from "../components/AnalysisBoard";
import AppNav from "../components/AppNav";
import { useDrillAnalysisStore } from "../stores/drillAnalysisStore";
import "../App.css";

/**
 * Ephemeral, in-memory review of a just-played drill (g-a406). Reads the
 * transient snapshot from drillAnalysisStore; refreshing or direct navigation
 * finds no snapshot and redirects to /play. No GameReviewStats footer —
 * accuracy is not available for a transient snapshot.
 */
function DrillAnalysisPage() {
  const snapshot = useDrillAnalysisStore((s) => s.snapshot);

  if (!snapshot) {
    return <Navigate to="/play" replace />;
  }

  return (
    <main className="app-shell history-page">
      <AppNav />

      <div className="constrained-content">
        <section className="history-shell">
          {snapshot.warning && (
            <p className="history-shell__error" role="status">
              {snapshot.warning}
            </p>
          )}
          <div className="analysis-pane">
            <div className="analysis-pane__shell">
              <AnalysisBoard
                moves={snapshot.moves}
                boardOrientation={snapshot.playerColor}
                initialMoveIndex={
                  snapshot.moves.length > 0 ? snapshot.initialMoveIndex : undefined
                }
                positionAnalysis={snapshot.positionAnalysis}
                footer={
                  <p className="drill-analysis-footer">Drill review — not saved</p>
                }
              />
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}

export default DrillAnalysisPage;
