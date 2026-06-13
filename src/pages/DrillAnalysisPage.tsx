import { Navigate, useNavigate } from "react-router-dom";
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
  const navigate = useNavigate();

  if (!snapshot) {
    return <Navigate to="/play" replace />;
  }

  // Explicit return to the drill. Carries the snapshot's source session identity
  // so /play can prove the retained game store describes this same drill and
  // restore the "Again" presentation. Never navigate(-1): browser history may
  // not point at the drill, and the destination needs the identity marker.
  const handleReturnToDrill = () => {
    navigate("/play", {
      state: { returnFromDrillAnalysis: { sourceSessionId: snapshot.sourceSessionId } },
    });
  };

  return (
    <main className="app-shell history-page drill-analysis-page">
      <AppNav />

      <div className="constrained-content">
        <section className="history-shell">
          <button
            type="button"
            className="drill-analysis-return"
            onClick={handleReturnToDrill}
            aria-label="Return to drill"
            title="Return to drill"
          >
            <span aria-hidden="true">←</span> Back to drill
          </button>
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
