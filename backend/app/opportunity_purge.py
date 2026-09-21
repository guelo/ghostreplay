"""Administrative whole-user SRS training purge (g-srs-retention-state).

This is the ONLY code that sets the transaction-local markers the freeze guards
in migration 20260919_05 accept. It exists because a user must remain able to
delete their own training history even after that history has been folded and
frozen — freezing protects evidence from silent rewriting, not from a deliberate,
scoped, whole-user deletion.

What this is NOT:

* Not an end-user API. No route is introduced here.
* Not authorization. The markers are transaction-local settings, so they are an
  accidental-misuse guard against the wrong code path reaching a frozen row.
  A privileged administrator with arbitrary SQL can set them anyway; what stops
  that is database access control, not this module.
* Not an FK bypass. ``session_replication_role`` is never touched. Rows are
  deleted in existing FK-valid order, and every pre-existing restriction still
  applies.

Lock order is fixed and stated once: the user row, then that user's retention
state, then the parent rows. Every trigger involved deliberately takes no user
lock at all, so no trigger can invert this order behind a parent-row lock.

Folded evidence is included. The raw rows of a folded batch are already deleted,
so what this has to remove instead is the manifest that describes them — and the
export file it points at. That file outlives this transaction whatever happens,
so the manifest is expired in place rather than deleted: it stays as a tombstone
naming the file, and the next expiry sweep takes the row and the file together.
A purge that left them behind would keep seven days of exactly the history it was
asked to delete.

This purge is not evidence-only. It deletes the user's sessions, so it also
deletes the ``rating_history`` rows earned in them: their Elo and games-played
chain resets. That follows from what a whole-training-history deletion means,
and it is stated here rather than discovered from a foreign key error.

A single-blunder deletion does NOT belong here. It cascades to its own summary
and event rows through existing foreign keys, and the event guard recognizes
that cascade by the parent blunder already being gone. Routing it through a
whole-user purge would hand it a whole-user escape it has no business holding.
"""

from __future__ import annotations

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    BlunderReview,
    GameSession,
    OpponentDecision,
    OpponentTargetFact,
    OpportunityFoldBatch,
    RatingHistory,
    SessionMove,
    User,
    UserOpportunityRetentionState,
)
from app.opportunity_retention import database_clock
from app.opportunity_store import ensure_retention_state
from app.row_locks import for_no_key_update

PURGE_MODE_SETTING = "ghostreplay.srs_purge_mode"
PURGE_USER_SETTING = "ghostreplay.srs_purge_user_id"
PURGE_MODE = "user_training"


class PurgeScopeError(RuntimeError):
    """The purge was asked to run against an owner it could not validate."""


def _mark_transaction(db: Session, *, user_id: int) -> None:
    """Arm the freeze escape for THIS transaction and this owner only.

    ``is_local => true`` is what bounds it: the settings expire at COMMIT or
    ROLLBACK, so a pooled connection handed to the next request carries nothing,
    and a savepoint rollback inside the purge cannot leave a half-armed state
    behind a still-open transaction.
    """
    db.execute(
        text("SELECT set_config(:name, :value, true)").bindparams(
            name=PURGE_MODE_SETTING, value=PURGE_MODE
        )
    )
    db.execute(
        text("SELECT set_config(:name, :value, true)").bindparams(
            name=PURGE_USER_SETTING, value=str(user_id)
        )
    )


def purge_user_training_history(db: Session, *, user_id: int) -> dict[str, int]:
    """Delete one user's sessions, blunders and all their opportunity state.

    The caller owns the COMMIT. That is not an oversight: the deferred purge
    assertion only runs at COMMIT, so committing here would hide the one check
    that proves the purge was complete from a caller that wanted to inspect it.

    Returns per-table counts, which are diagnostics only — the assertion, not
    these numbers, is what establishes that nothing was left behind.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        raise PurgeScopeError("purge requires a positive integer user id")

    # Lock the user first, then its retention state, then touch parents. Taking
    # the state row AFTER a parent row would give a second purge the opposite
    # order and deadlock the two.
    owner = for_no_key_update(db.query(User).filter(User.id == user_id)).first()
    if owner is None:
        raise PurgeScopeError(f"purge target user {user_id} does not exist")
    # The deferred completeness assertion is an AFTER DELETE trigger on the
    # retention state, so a user with no such row would be purged with nothing
    # checking that the purge was complete. Creating the row here — after the
    # owner is validated and before it is locked — is what makes the assertion
    # unconditional instead of dependent on whether this user happened to
    # predate the migration. Nothing else writes this row on the request path.
    ensure_retention_state(db, user_id)
    for_no_key_update(
        db.query(UserOpportunityRetentionState).filter(
            UserOpportunityRetentionState.user_id == user_id
        )
    ).first()

    _mark_transaction(db, user_id=user_id)

    blunder_ids = list(
        db.execute(select(Blunder.id).where(Blunder.user_id == user_id)).scalars()
    )
    session_ids = list(
        db.execute(select(GameSession.id).where(GameSession.user_id == user_id)).scalars()
    )

    counts: dict[str, int] = {}
    # FK-valid order, and the order is the whole point. Three targeting tables
    # reference blunders.id with NO ondelete — session_moves.target_blunder_id,
    # opponent_decisions.target_blunder_id and opponent_target_facts.blunder_id —
    # so deleting blunders first fails for any user who was ever ghost-targeted.
    # They are scoped by BLUNDER, not by session, so that a reference parked in
    # some other session still clears; in practice only this user's sessions
    # target this user's blunders, but the purge must not depend on that.
    if blunder_ids:
        counts["events"] = db.execute(
            delete(BlunderOpportunityEvent).where(
                BlunderOpportunityEvent.blunder_id.in_(blunder_ids)
            )
        ).rowcount
        counts["summaries"] = db.execute(
            delete(BlunderOpportunitySummary).where(
                BlunderOpportunitySummary.blunder_id.in_(blunder_ids)
            )
        ).rowcount
        counts["reviews"] = db.execute(
            delete(BlunderReview).where(BlunderReview.blunder_id.in_(blunder_ids))
        ).rowcount
        # Nullable echoes of which blunder was being steered toward. The move
        # and the served decision themselves are history of the GAME, and die
        # with their session below; only the pointer has to go first.
        db.execute(
            SessionMove.__table__.update()
            .where(SessionMove.target_blunder_id.in_(blunder_ids))
            .values(target_blunder_id=None)
        )
        db.execute(
            OpponentDecision.__table__.update()
            .where(OpponentDecision.target_blunder_id.in_(blunder_ids))
            .values(target_blunder_id=None)
        )
        # blunder_id is half this table's primary key, so there is no pointer to
        # null: the fact only means anything alongside the blunder it names.
        counts["target_facts"] = db.execute(
            delete(OpponentTargetFact).where(
                OpponentTargetFact.blunder_id.in_(blunder_ids)
            )
        ).rowcount
    if session_ids:
        # blunders.source_session_id and game_sessions.recorded_blunder_id point
        # at each other, so both references are cleared before either table is
        # deleted rather than relying on a delete order that cannot exist.
        db.execute(
            GameSession.__table__.update()
            .where(GameSession.user_id == user_id)
            .values(recorded_blunder_id=None)
        )
        db.execute(
            Blunder.__table__.update()
            .where(Blunder.user_id == user_id)
            .values(source_session_id=None)
        )
    if session_ids:
        # rating_history.game_session_id is NOT NULL with no cascade, so a user
        # who ever finished a rated game cannot have their sessions deleted
        # while their rating rows survive. There is no pointer to null and no
        # meaning left in the row: the purge removes the very games the rating
        # was earned in. The consequence is stated rather than hidden — the
        # user's Elo and games-played chain reset, which is what deleting your
        # whole training history means.
        counts["rating_history"] = db.execute(
            delete(RatingHistory).where(RatingHistory.user_id == user_id)
        ).rowcount
        # blunder_reviews.session_id also has no cascade. The blunder-scoped
        # delete above covers every review of THIS user's blunders; this is the
        # session-scoped half, so a review parked in one of these sessions
        # cannot block the delete even if it somehow names another blunder.
        counts["session_reviews"] = db.execute(
            delete(BlunderReview).where(BlunderReview.session_id.in_(session_ids))
        ).rowcount
    # Sessions before blunders: everything still pointing at a blunder from here
    # is session-owned and cascades away with its session.
    counts["sessions"] = db.execute(
        delete(GameSession).where(GameSession.user_id == user_id)
    ).rowcount
    counts["blunders"] = db.execute(
        delete(Blunder).where(Blunder.user_id == user_id)
    ).rowcount
    # Fold manifests. Deleting the ACCOUNT cascades these away; deleting only the
    # training history under it does not, and leaving them alone would keep a
    # record of which blunders this user had evidence for, plus a restore that
    # would try to put the deleted rows back.
    #
    # Expired in place rather than deleted, because the artifact is a FILE and no
    # transaction can unlink one: something has to outlive this commit still
    # naming it. A deleted manifest names nothing, and the sweep cannot infer the
    # deletion from the missing retention-state row below — a user who keeps
    # playing gets that row back at their next served target
    # (:mod:`app.srs_target_admission`), and their export would then age out over
    # seven days like any orphan. So the row stays, saying three things:
    #
    # * ``expires_at`` now, which is what hands the row and its file to the next
    #   expiry sweep (:mod:`app.opportunity_fold_recovery`), whatever else has
    #   happened to this user by then;
    # * ``restored_at`` now, because what that column asserts is true here — the
    #   rows it describes are no longer deleted-but-recoverable — and leaving it
    #   NULL would hold the recovery anchor open and refuse a schema downgrade
    #   over rows nothing may ever put back;
    # * no contributions, because that ledger is per-blunder and is precisely the
    #   record of which blunders this user had evidence for.
    now = db.execute(select(database_clock(db))).scalar_one()
    counts["fold_batches"] = db.execute(
        update(OpportunityFoldBatch)
        .where(OpportunityFoldBatch.user_id == user_id)
        .values(expires_at=now, restored_at=now, contributions=[])
        .execution_options(synchronize_session=False)
    ).rowcount
    counts["retention_state"] = db.execute(
        delete(UserOpportunityRetentionState).where(
            UserOpportunityRetentionState.user_id == user_id
        )
    ).rowcount
    return counts
