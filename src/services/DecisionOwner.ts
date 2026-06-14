/**
 * DecisionOwner — coordinator-lifetime reducer + retry outbox (g-4dqj).
 *
 * Owns the durable recording/SRS decision pipeline that used to live in
 * component refs inside AnalysisEffects. It consumes the typed `AnalysisOutcome`
 * channel and a synchronous `getGameState()` snapshot (both injected) and drives
 * the UI through a small callback surface, so it builds and unit-tests in full
 * isolation from React. The React layer (g-2m0p) wires this onto the
 * coordinator's outcome/reset listeners and supplies the callbacks.
 *
 * Three concerns, one owner:
 *  - recording frontier (per-moveIndex slots, committed-boundary guard)
 *  - SRS review FIFO outbox (per blunderId, registration order)
 *  - blunder-alert microtask coalescing (latest-only, epoch-leased)
 *
 * Every backend POST goes through the outbox, which retries network/5xx/429 with
 * exponential backoff (honoring Retry-After) within the tab lifetime and sends a
 * stable idempotency key on every attempt so retries dedupe server-side.
 */

import {
  ApiError,
  errorCodeOf,
  recordBlunder,
  reviewSrsBlunder,
  type RecordBlunderRequest,
  type TargetBlunderSrs,
} from '../utils/api'
import { evaluateBlunderCandidate } from '../utils/blunder'
import { evalLoss, gradeRecordableMove } from '../workers/analysisUtils'
import {
  buildBlunderAlert,
  fenBeforeMove,
  sanForUciMove,
  type BlunderAlert,
  type MoveRecord,
} from '../components/chess-game/domain/movePresentation'
import type { MoveMessage, SrsFailDetail } from '../components/MoveList'
import type { ResolvedReview } from '../components/chess-game/types'
import type {
  AnalysisOutcome,
  AnalysisResetInfo,
} from './GameAnalysisCoordinator'

/** 30s ceiling on how long a provisional (failed/skipped) analysis may wait for
 *  a reschedule before the frontier slot / SRS slot is abandoned. */
const MAX_AWAITING_RETRY_MS = 30_000
/** HTTP backoff cap: min(2^attempts, 60)s. */
const MAX_HTTP_BACKOFF_S = 60

// ---------------------------------------------------------------------------
// Injected dependencies
// ---------------------------------------------------------------------------

/** Synchronous game-state snapshot the owner reads at decision time. Mirrors the
 *  fields AnalysisEffects pulled from `useGameStore.getState()`. */
export interface DecisionOwnerGameState {
  sessionId: string | null
  isGameActive: boolean
  isPracticeContinuation: boolean
  playerColor: 'white' | 'black'
  moveHistory: MoveRecord[]
}

/** UI surface the owner drives. The React layer passes its setState/append/audio
 *  functions; tests pass spies. `setResolvedReview` takes a React-style updater so
 *  the owner can guard against a stale analysisId without owning React state. */
export interface DecisionOwnerCallbacks {
  appendMoveMessage: (moveIndex: number, msg: MoveMessage) => void
  setBlunderAlert: (alert: BlunderAlert) => void
  setShowFlash: (show: boolean) => void
  setResolvedReview: (
    update: (prev: ResolvedReview | null) => ResolvedReview | null,
  ) => void
  onSrsFail: (detail: SrsFailDetail, moveIndex: number) => void
  playBuzzer: () => void
  playBlunderAudio: () => void
}

export interface DecisionOwnerDeps {
  getGameState: () => DecisionOwnerGameState
}

/** Per-moveIndex SRS registration payload (the controller mints `srsDecisionId`
 *  once at arm time). Stored with a monotonic `registrationSeq` for FIFO drain. */
export interface PendingSrsReview {
  sessionId: string
  blunderId: number
  moveIndex: number
  userMoveSan: string
  srs: TargetBlunderSrs | null
  srsDecisionId: string
}

/** Recording-frontier context for a player move, keyed by request id. */
export interface PendingAnalysisContext {
  fen: string
  pgn: string
  moveSan: string
  moveUci: string
  moveIndex: number
}

// ---------------------------------------------------------------------------
// Frontier
// ---------------------------------------------------------------------------

type FrontierStatus =
  | 'pending_analysis' // scheduled: awaiting resolved/failed/skipped — BLOCKS
  | 'failed_provisional' // analysis failed, reschedule may follow — BLOCKS
  | 'skipped_provisional' // analysis skipped, reschedule may follow — BLOCKS
  | 'resolved' // resolved; blunderCandidate computed — TERMINAL
  | 'abandoned' // provisional + 30s retry timeout — TERMINAL

interface FrontierSlot {
  moveIndex: number
  requestId: string
  status: FrontierStatus
  blunderCandidate: RecordBlunderRequest | null
  retryTimeoutId?: ReturnType<typeof setTimeout>
}

// ---------------------------------------------------------------------------
// Outbox
// ---------------------------------------------------------------------------

type SrsOutboxStatus =
  | 'awaiting_analysis'
  | 'awaiting_retry'
  | 'pending'
  | 'awaiting_http_retry'
  | 'in_flight'
  | 'succeeded'
  | 'terminal_error'

type BlunderOutboxStatus =
  | 'pending'
  | 'awaiting_http_retry'
  | 'in_flight'
  | 'succeeded'
  | 'terminal_error'

/** Exact `POST /api/srs/review` body. (Mirror of the unexported api.ts shape;
 *  duplicated here to avoid widening the api surface.) */
interface SrsReviewPayload {
  session_id: string
  blunder_id: number
  passed: boolean
  user_move: string
  eval_delta: number
  idempotency_key: string
}

interface SrsOutboxEntry {
  kind: 'srs'
  blunderId: number
  /** STABLE identity — minted once at registration, survives reschedule. */
  srsDecisionId: string
  /** CURRENT request id — updated on every reschedule. */
  requestId: string
  registrationSeq: number
  moveIndex: number
  payload: SrsReviewPayload | null
  status: SrsOutboxStatus
  terminalError?: string
  analysisRetryTimeoutId?: ReturnType<typeof setTimeout>
  httpRetryTimeoutId?: ReturnType<typeof setTimeout>
  attempts: number
}

interface BlunderOutboxEntry {
  kind: 'blunder'
  moveIndex: number
  payload: RecordBlunderRequest
  status: BlunderOutboxStatus
  terminalError?: string
  httpRetryTimeoutId?: ReturnType<typeof setTimeout>
  attempts: number
}

type OutboxEntry = SrsOutboxEntry | BlunderOutboxEntry

// ---------------------------------------------------------------------------
// HTTP retry classification
// ---------------------------------------------------------------------------

/** Network error (no status) and retryable ApiErrors (429 + 5xx per api.ts:100)
 *  retry; everything else is terminal. Do NOT hand-roll `status >= 500` — that
 *  would wrongly make 429 terminal. */
const isRetryable = (err: unknown): boolean => {
  if (err instanceof TypeError) return true // fetch network error, no status
  if (err instanceof ApiError) return err.retryable
  return false
}

/** A 409 the backend tags LEGACY_AMBIGUOUS is treated as success: the row already
 *  exists from a pre-idempotency write, so the POST has effectively landed. */
const isLegacyAmbiguous = (err: unknown): boolean =>
  err instanceof ApiError &&
  err.status === 409 &&
  errorCodeOf(err) === 'LEGACY_AMBIGUOUS'

const cancelTimer = (id: ReturnType<typeof setTimeout> | undefined): void => {
  if (id !== undefined) clearTimeout(id)
}

// ---------------------------------------------------------------------------
// DecisionOwner
// ---------------------------------------------------------------------------

export class DecisionOwner {
  private readonly getGameState: () => DecisionOwnerGameState
  /** The single active UI lease (or null while AnalysisEffects is unmounted).
   *  Durable work runs regardless; transient UI calls are gated on this. */
  private uiLease: { leaseId: number; callbacks: DecisionOwnerCallbacks } | null = null
  private uiLeaseSeq = 0

  // Recording frontier / decision boundary
  private nextDecisionIndex = 0
  private committedDecisionIndex = 0
  private currentGeneration = 0
  private uiEpoch = 0

  private contextMap = new Map<string, PendingAnalysisContext>()
  private pendingSrsMap = new Map<
    string,
    PendingSrsReview & { registrationSeq: number }
  >()
  private blunderReserved = false

  // Alert (microtask-coalesced, latest-only, epoch-leased)
  private alertBuffer: Array<{ moveIndex: number; result: NonNullable<AnalysisOutcome['result']> }> = []
  private alertEpoch = 0
  private alertScheduledEpoch: number | null = null

  /** Monotonic; NEVER resets across generations — globally unique for tab life. */
  private registrationSeqCounter = 0

  private terminatedRequests = new Map<
    string,
    { status: 'skipped' | 'failed'; moveIndex: number }
  >()
  /** requestId → moveIndex. Suppresses a late reschedule/resolved and carries the
   *  moveIndex for partial-reset pruning. */
  private abandonedRequests = new Map<string, number>()
  /** dedup key "generation:requestId" → moveIndex (moveIndex enables prune). */
  private processedOutcomes = new Map<string, number>()
  private frontier = new Map<number, FrontierSlot>()
  private outbox: OutboxEntry[] = []
  /** Set once by dispose(). Async continuations (POST settle, leaked timers)
   *  that fire afterward must be inert — they can no longer be cancelled. */
  private disposed = false

  constructor(deps: DecisionOwnerDeps) {
    this.getGameState = deps.getGameState
  }

  /** Seed the generation from the coordinator's epoch so the very first outcome
   *  validates by generation without waiting for a reset. */
  seedGeneration(generation: number): void {
    this.currentGeneration = generation
  }

  /** Lease the single active UI surface. The durable recording/SRS/outbox path
   *  always runs; transient UI calls (alert flash/audio, resolved-review overlay,
   *  move messages, onSrsFail) fire only while a lease is held. Acquiring or
   *  releasing a lease bumps the UI epoch so an alert scheduled under one lease is
   *  dropped if the lease changed before its microtask flush (unmount suppression).
   *  Returns a cleanup that releases this exact lease (no-op if superseded). */
  registerUICallbacks(callbacks: DecisionOwnerCallbacks): () => void {
    const leaseId = ++this.uiLeaseSeq
    this.uiLease = { leaseId, callbacks }
    this.bumpUiEpoch()
    return () => {
      if (this.uiLease?.leaseId !== leaseId) return
      this.uiLease = null
      this.bumpUiEpoch()
    }
  }

  /** Bump the UI epoch and clear any buffered/scheduled alert. A lease change or
   *  reset between an alert's schedule and its flush makes the flush a no-op. */
  private bumpUiEpoch(): void {
    this.uiEpoch += 1
    this.alertEpoch += 1
    this.alertBuffer = []
    this.alertScheduledEpoch = null
  }

  // --- Registration (called by the controller before/around a move) ---

  registerBlunderContext(requestId: string, context: PendingAnalysisContext): void {
    this.contextMap.set(requestId, context)
  }

  /** True while an armed SRS review for `requestId` is still unresolved (its
   *  analysis has not graded it yet). A leased UI uses this to decide whether a
   *  surviving `pending` resolved-review overlay is still live (keep it — the
   *  owner will transition it on resolution) or stale (the decision already
   *  resolved durably while the UI was unmounted — clear it). */
  hasPendingReview(requestId: string): boolean {
    return this.pendingSrsMap.has(requestId)
  }

  /** Register a pending SRS review. Consults terminatedRequests so a review armed
   *  AFTER its analysis already failed/skipped opens directly in `awaiting_retry`
   *  rather than waiting forever for a `resolved` that already fired. */
  registerSrsReview(requestId: string, review: PendingSrsReview): void {
    const seq = this.registrationSeqCounter++
    this.pendingSrsMap.set(requestId, { ...review, registrationSeq: seq })

    const terminated = this.terminatedRequests.get(requestId)
    const status: SrsOutboxStatus =
      terminated?.status === 'skipped' || terminated?.status === 'failed'
        ? 'awaiting_retry'
        : 'awaiting_analysis'

    const slot: SrsOutboxEntry = {
      kind: 'srs',
      blunderId: review.blunderId,
      srsDecisionId: review.srsDecisionId,
      requestId,
      registrationSeq: seq,
      moveIndex: review.moveIndex,
      payload: null,
      status,
      attempts: 0,
    }
    this.outbox.push(slot)
    if (status === 'awaiting_retry') this.scheduleAnalysisRetryTimeout(slot)
  }

  // --- Outcome channel ---

  handleOutcome(outcome: AnalysisOutcome): void {
    if (outcome.generation !== this.currentGeneration) return // stale guard

    switch (outcome.status) {
      case 'scheduled':
        this.onScheduled(outcome)
        return
      case 'failed':
      case 'skipped':
        this.onProvisional(outcome)
        return
      case 'resolved':
        this.onResolved(outcome)
        return
    }
  }

  private onScheduled(outcome: AnalysisOutcome): void {
    const result = this.migrateOnReschedule(outcome)
    if (result === 'ignored') {
      // The predecessor was tombstoned; suppress the whole reopened lineage so a
      // later `resolved` for this request can never record.
      this.abandonedRequests.set(outcome.requestId, outcome.moveIndex)
      return
    }
    this.frontier.set(outcome.moveIndex, {
      moveIndex: outcome.moveIndex,
      requestId: outcome.requestId,
      status: 'pending_analysis',
      blunderCandidate: null,
    })
    if (this.nextDecisionIndex > outcome.moveIndex) {
      this.nextDecisionIndex = outcome.moveIndex
    }
    this.advanceFrontier()
  }

  private onProvisional(outcome: AnalysisOutcome): void {
    const provisionalStatus: FrontierStatus =
      outcome.status === 'failed' ? 'failed_provisional' : 'skipped_provisional'
    this.terminatedRequests.set(outcome.requestId, {
      status: outcome.status as 'failed' | 'skipped',
      moveIndex: outcome.moveIndex,
    })

    const slot: FrontierSlot = {
      moveIndex: outcome.moveIndex,
      requestId: outcome.requestId,
      status: provisionalStatus,
      blunderCandidate: null,
    }
    const gen = this.currentGeneration
    slot.retryTimeoutId = setTimeout(() => {
      if (this.disposed) return
      if (this.currentGeneration !== gen) return
      // The slot may have been replaced (reschedule/late provisional) or pruned
      // (partial reset, same generation) after this callback was queued. Only the
      // object still live at this index may abandon — otherwise we would
      // reintroduce a tombstone the prune just removed.
      if (this.frontier.get(slot.moveIndex) !== slot) return
      if (
        slot.status !== 'failed_provisional' &&
        slot.status !== 'skipped_provisional'
      ) {
        return
      }
      slot.status = 'abandoned'
      slot.retryTimeoutId = undefined
      // Block a late reschedule and carry moveIndex for partial-reset pruning.
      this.abandonedRequests.set(slot.requestId, slot.moveIndex)
      this.advanceFrontier()
    }, MAX_AWAITING_RETRY_MS)

    this.frontier.set(outcome.moveIndex, slot)
    this.transitionSrsSlotOnTermination(outcome)
    this.advanceFrontier()
  }

  private onResolved(outcome: AnalysisOutcome): void {
    if (this.abandonedRequests.has(outcome.requestId)) return
    const dedupKey = `${this.currentGeneration}:${outcome.requestId}`
    if (this.processedOutcomes.has(dedupKey)) return
    this.processedOutcomes.set(dedupKey, outcome.moveIndex)

    // SRS exactly once + alert (frontier-independent).
    this.handleSrsUiImmediate(outcome)

    const ctx = this.contextMap.get(outcome.requestId) ?? null
    const gs = this.getGameState()
    const blunderCandidate =
      ctx && outcome.result
        ? evaluateBlunderCandidate({
            analysis: outcome.result,
            context: ctx,
            sessionId: gs.sessionId,
            // Snapshot semantics: a practice continuation is not "game active".
            isGameActive: gs.isGameActive && !gs.isPracticeContinuation,
          })
        : null
    // Context is consumed once snapshotted into the candidate.
    this.contextMap.delete(outcome.requestId)

    this.frontier.set(outcome.moveIndex, {
      moveIndex: outcome.moveIndex,
      requestId: outcome.requestId,
      status: 'resolved',
      blunderCandidate,
    })
    this.advanceFrontier()
  }

  /**
   * Reschedule (`scheduled` with previousRequestId): migrate BOTH contextMap and
   * pendingSrsMap from the old request id to the new one, re-point the SRS slot's
   * live request id, and reopen any provisional/awaiting_retry timers. Returns
   * 'ignored' when the predecessor is tombstoned — the caller must suppress the
   * entire new request.
   */
  private migrateOnReschedule(outcome: AnalysisOutcome): 'accepted' | 'ignored' {
    const prev = outcome.previousRequestId
    if (!prev) return 'accepted'
    if (this.abandonedRequests.has(prev)) return 'ignored'

    const ctx = this.contextMap.get(prev)
    if (ctx !== undefined) {
      this.contextMap.set(outcome.requestId, ctx)
      this.contextMap.delete(prev)
    }

    const srs = this.pendingSrsMap.get(prev)
    if (srs !== undefined) {
      this.pendingSrsMap.set(outcome.requestId, srs) // keep same registrationSeq
      this.pendingSrsMap.delete(prev)
      const slot = this.findSrsSlotBySrsDecisionId(srs.srsDecisionId)
      if (slot) {
        slot.requestId = outcome.requestId // re-point identity to the live request
        if (slot.status === 'awaiting_retry') {
          cancelTimer(slot.analysisRetryTimeoutId)
          slot.analysisRetryTimeoutId = undefined
          slot.status = 'awaiting_analysis'
        }
      }
      // Keep the resolved-review overlay pointed at the live request id.
      this.uiLease?.callbacks.setResolvedReview((p) =>
        p && p.analysisId === prev
          ? { analysisId: outcome.requestId, moveIndex: p.moveIndex, result: p.result }
          : p,
      )
    }

    const fslot = this.frontier.get(outcome.moveIndex)
    if (
      fslot &&
      (fslot.status === 'failed_provisional' ||
        fslot.status === 'skipped_provisional')
    ) {
      cancelTimer(fslot.retryTimeoutId) // onScheduled overwrites the slot anyway
      fslot.retryTimeoutId = undefined
    }
    this.terminatedRequests.delete(prev)
    return 'accepted'
  }

  // --- Recording frontier ---

  /** Advance through consecutive TERMINAL slots (resolved/abandoned); the
   *  provisional/pending statuses BLOCK. Recording (processSlot) runs only for
   *  resolved slots; the 30s provisional timer guarantees eventual progress. */
  private advanceFrontier(): void {
    for (;;) {
      const idx = this.nextDecisionIndex
      const slot = this.frontier.get(idx)
      if (!slot) break
      if (
        slot.status === 'pending_analysis' ||
        slot.status === 'failed_provisional' ||
        slot.status === 'skipped_provisional'
      ) {
        break
      }
      if (slot.status === 'resolved') this.processSlot(slot)
      this.committedDecisionIndex = Math.max(this.committedDecisionIndex, idx + 1)
      this.nextDecisionIndex = idx + 1
    }
  }

  /** Blunder recording only — committed-boundary guard, one blunder per session. */
  private processSlot(slot: FrontierSlot): void {
    if (slot.status !== 'resolved') return
    if (slot.moveIndex < this.committedDecisionIndex) return // monotonic boundary
    if (slot.blunderCandidate !== null && !this.blunderReserved) {
      this.blunderReserved = true
      const decisionId = crypto.randomUUID()
      this.outbox.push({
        kind: 'blunder',
        moveIndex: slot.moveIndex,
        payload: { ...slot.blunderCandidate, idempotency_key: decisionId },
        status: 'pending',
        attempts: 0,
      })
      this.drainOutbox()
    }
  }

  // --- SRS immediate UI + alert (Finding 4) ---

  private handleSrsUiImmediate(outcome: AnalysisOutcome): void {
    // Always evaluated: a non-SRS blunder still buffers an alert here.
    this.maybeBufferAlert(outcome)
    const srsEntry = this.pendingSrsMap.get(outcome.requestId)
    if (!srsEntry) return
    this.fillSrsSlotByRequestId(outcome.requestId, outcome)
  }

  private maybeBufferAlert(outcome: AnalysisOutcome): void {
    if (!this.isBlunderAlert(outcome)) return
    const result = outcome.result!
    this.alertBuffer.push({ moveIndex: outcome.moveIndex, result })
    this.maybeScheduleAlertFlush()
  }

  private isBlunderAlert(outcome: AnalysisOutcome): boolean {
    const r = outcome.result
    if (!r) return false
    return (
      r.blunder &&
      r.delta !== null &&
      r.moveIndex !== null &&
      this.isPlayerMoveIndex(r.moveIndex)
    )
  }

  private isPlayerMoveIndex(index: number): boolean {
    if (index < 0) return false
    const playerColor = this.getGameState().playerColor
    const isWhiteMove = index % 2 === 0
    return playerColor === 'white' ? isWhiteMove : !isWhiteMove
  }

  private maybeScheduleAlertFlush(): void {
    if (this.alertScheduledEpoch !== null) return
    const epochAtSchedule = this.alertEpoch
    this.alertScheduledEpoch = epochAtSchedule
    queueMicrotask(() => this.flushAlert(epochAtSchedule))
  }

  private flushAlert(epochAtSchedule: number): void {
    this.alertScheduledEpoch = null
    const buffer = this.alertBuffer
    this.alertBuffer = []
    // A reset/prune/dispose/lease-change between scheduling and flush bumps the
    // epoch — drop the stale alert + audio.
    if (epochAtSchedule !== this.alertEpoch) return
    if (buffer.length === 0) return
    // Transient-only: no UI lease (AnalysisEffects unmounted) → suppress.
    const ui = this.uiLease?.callbacks
    if (!ui) return

    // Latest-only: highest-moveIndex buffered player blunder.
    const top = buffer.reduce((a, b) => (b.moveIndex > a.moveIndex ? b : a))
    const result = top.result
    if (result.moveIndex === null || result.delta === null) return

    const moveHistory = this.getGameState().moveHistory
    const moveSan = moveHistory[result.moveIndex]?.san ?? result.move
    ui.setBlunderAlert(
      buildBlunderAlert({
        moveHistory,
        moveIndex: result.moveIndex,
        moveSan,
        moveUci: result.move,
        bestMoveUci: result.bestMove,
        delta: result.delta,
        shouldRewind: true,
      }),
    )
    ui.setShowFlash(true)
    ui.playBlunderAudio()
  }

  // --- SRS slot fill + grading ---

  findSrsSlotBySrsDecisionId(srsDecisionId: string): SrsOutboxEntry | undefined {
    for (const entry of this.outbox) {
      if (entry.kind === 'srs' && entry.srsDecisionId === srsDecisionId) {
        return entry
      }
    }
    return undefined
  }

  /** Grade the resolved move, fire the resolved-review UI exactly once, and fill
   *  the SRS outbox slot's payload (or terminate it when the eval is
   *  unavailable). Then drain so the FIFO can advance. */
  private fillSrsSlotByRequestId(requestId: string, outcome: AnalysisOutcome): void {
    const pending = this.pendingSrsMap.get(requestId)
    if (!pending) return
    const slot = this.findSrsSlotBySrsDecisionId(pending.srsDecisionId)
    if (!slot) return
    const result = outcome.result
    if (!result || result.moveIndex !== outcome.moveIndex) return

    const grade = gradeRecordableMove(result.delta)
    if (grade === 'unavailable') {
      // Neither pass nor fail — no review can be posted. Terminate so the FIFO
      // is not stranded behind a slot that will never get a payload.
      this.pendingSrsMap.delete(requestId)
      this.markTerminal(slot, 'eval_unavailable')
      return
    }

    this.pendingSrsMap.delete(requestId)
    const passed = grade === 'pass'
    const evalLossCp = evalLoss(result.delta) ?? 0

    // Durable side FIRST: build the payload, arm the slot, cancel the analysis
    // lease, and drain. A throwing React setState in the transient block below
    // then cannot starve the durable POST (review finding 4).
    slot.payload = {
      session_id: pending.sessionId,
      blunder_id: pending.blunderId,
      passed,
      user_move: pending.userMoveSan,
      eval_delta: evalLossCp,
      idempotency_key: pending.srsDecisionId,
    }
    slot.status = 'pending'
    cancelTimer(slot.analysisRetryTimeoutId)
    slot.analysisRetryTimeoutId = undefined
    this.drainOutbox()

    // Transient UI — only while a lease is held.
    const ui = this.uiLease?.callbacks
    ui?.setResolvedReview((prev) =>
      prev?.analysisId === requestId
        ? { analysisId: requestId, moveIndex: outcome.moveIndex, result: passed ? 'pass' : 'fail' }
        : prev,
    )

    if (passed) {
      const srs = pending.srs
      ui?.appendMoveMessage(outcome.moveIndex, {
        key: `srs-${requestId}`,
        text: 'Correct! You avoided your past mistake.',
        variant: 'srs-pass',
        srsStats: srs
          ? { passCount: srs.pass_count + 1, failCount: srs.fail_count, streak: srs.pass_streak + 1 }
          : undefined,
      })
    } else if (ui) {
      const sourceFen = fenBeforeMove(
        this.getGameState().moveHistory,
        outcome.moveIndex,
      )
      const bestMoveSan = sanForUciMove(sourceFen, result.bestMove)
      const srs = pending.srs
      const srsFailDetail: SrsFailDetail = {
        userMoveSan: pending.userMoveSan,
        bestMoveSan,
        userMoveUci: result.move,
        bestMoveUci: result.bestMove,
      }
      ui.appendMoveMessage(outcome.moveIndex, {
        key: `srs-${requestId}`,
        text: 'You made this mistake again!',
        variant: 'srs-fail',
        srsFailDetail,
        srsStats: srs
          ? { passCount: srs.pass_count, failCount: srs.fail_count + 1, streak: 0 }
          : undefined,
      })
      ui.onSrsFail(srsFailDetail, outcome.moveIndex)
      ui.playBuzzer()
    }
  }

  /** A `failed`/`skipped` for a request whose SRS review is already registered:
   *  the resolved that the awaiting_analysis slot expected will never come, so
   *  move it to awaiting_retry (a reschedule can still reopen it). */
  private transitionSrsSlotOnTermination(outcome: AnalysisOutcome): void {
    const pending = this.pendingSrsMap.get(outcome.requestId)
    if (!pending) return
    const slot = this.findSrsSlotBySrsDecisionId(pending.srsDecisionId)
    if (!slot || slot.status !== 'awaiting_analysis') return
    slot.status = 'awaiting_retry'
    this.scheduleAnalysisRetryTimeout(slot)
  }

  /** 30s analysis-retry lease: if no reschedule reopens the slot, terminate it,
   *  tombstone its CURRENT request id (migrated by reschedule), and drop the
   *  pending/terminated bookkeeping. */
  private scheduleAnalysisRetryTimeout(slot: SrsOutboxEntry): void {
    const gen = this.currentGeneration
    cancelTimer(slot.analysisRetryTimeoutId)
    slot.analysisRetryTimeoutId = setTimeout(() => {
      if (this.disposed) return
      if (this.currentGeneration !== gen) return
      if (slot.status !== 'awaiting_retry') return
      this.abandonedRequests.set(slot.requestId, slot.moveIndex)
      this.pendingSrsMap.delete(slot.requestId)
      this.terminatedRequests.delete(slot.requestId)
      this.markTerminal(slot, 'retry_timeout')
    }, MAX_AWAITING_RETRY_MS)
  }

  // --- Outbox drain + HTTP ---

  /** Select every send-eligible entry. SRS entries respect per-blunderId FIFO;
   *  blunder entries have no ordering constraint (blunderReserved dedupes). */
  private drainOutbox(): void {
    if (this.disposed) return
    for (const entry of this.outbox) {
      if (entry.kind === 'blunder') {
        if (entry.status === 'pending') this.sendEntry(entry)
      } else if (entry.status === 'pending' && this.canDrainSrs(entry)) {
        this.sendEntry(entry)
      }
    }
  }

  /** FIFO gate: an SRS entry may send only when every lower-registrationSeq entry
   *  for the same blunderId is already succeeded or terminal_error. */
  private canDrainSrs(entry: SrsOutboxEntry): boolean {
    for (const other of this.outbox) {
      if (other.kind !== 'srs') continue
      if (other.blunderId !== entry.blunderId) continue
      if (
        other.registrationSeq < entry.registrationSeq &&
        other.status !== 'succeeded' &&
        other.status !== 'terminal_error'
      ) {
        return false
      }
    }
    return true
  }

  private sendEntry(entry: OutboxEntry): void {
    entry.status = 'in_flight'
    entry.attempts += 1
    const promise =
      entry.kind === 'blunder'
        ? recordBlunder(
            entry.payload.session_id,
            entry.payload.pgn,
            entry.payload.fen,
            entry.payload.user_move,
            entry.payload.best_move,
            entry.payload.eval_before,
            entry.payload.eval_after,
            entry.payload.idempotency_key,
          )
        : reviewSrsBlunder(
            entry.payload!.session_id,
            entry.payload!.blunder_id,
            entry.payload!.passed,
            entry.payload!.user_move,
            entry.payload!.eval_delta,
            entry.payload!.idempotency_key,
          )
    promise
      .then(() => {
        // A POST that settles after dispose() can no longer re-enter the drain.
        if (this.disposed) return
        // Durable POST landed — survives any reset that happened meanwhile.
        entry.status = 'succeeded'
        this.drainOutbox()
      })
      .catch((err: unknown) => {
        // After dispose() a late rejection must not schedule a new retry timer.
        if (this.disposed) return
        this.handleSendError(entry, err)
      })
  }

  private handleSendError(entry: OutboxEntry, err: unknown): void {
    if (isLegacyAmbiguous(err)) {
      // Pre-idempotency duplicate: the row exists, so treat as success.
      entry.status = 'succeeded'
      this.drainOutbox()
      return
    }
    if (isRetryable(err)) {
      this.scheduleHttpRetry(entry, err)
      return
    }
    this.markTerminal(entry, errorCodeOf(err) ?? `http_${(err as ApiError)?.status ?? 'error'}`)
  }

  /** Backoff min(2^attempts, 60)s, honoring Retry-After. The timer is NOT
   *  generation-guarded — an awaiting_http_retry entry survives all resets. */
  private scheduleHttpRetry(entry: OutboxEntry, err: unknown): void {
    if (this.disposed) return
    const backoffMs = Math.min(2 ** entry.attempts, MAX_HTTP_BACKOFF_S) * 1000
    const retryAfterMs = err instanceof ApiError ? err.retryAfterMs ?? 0 : 0
    const delayMs = Math.max(backoffMs, retryAfterMs)
    entry.status = 'awaiting_http_retry'
    entry.httpRetryTimeoutId = setTimeout(() => {
      entry.httpRetryTimeoutId = undefined
      entry.status = 'pending'
      this.drainOutbox()
    }, delayMs)
  }

  /** Terminal settle helper (Finding 2): set status AND re-drain so the next FIFO
   *  successor for the same blunderId can advance immediately. */
  private markTerminal(entry: OutboxEntry, reason: string): void {
    entry.status = 'terminal_error'
    entry.terminalError = reason
    if (entry.kind === 'srs') {
      cancelTimer(entry.analysisRetryTimeoutId)
      entry.analysisRetryTimeoutId = undefined
    }
    cancelTimer(entry.httpRetryTimeoutId)
    entry.httpRetryTimeoutId = undefined
    this.drainOutbox()
  }

  // --- Resets ---

  /** Dispatch a coordinator reset: full session change (no fromMoveIndex) or a
   *  revert prune (fromMoveIndex present). */
  handleReset(info: AnalysisResetInfo): void {
    if (info.fromMoveIndex === undefined) {
      this.fullReset(info.generation)
    } else {
      this.partialReset(info.fromMoveIndex)
    }
  }

  /** Cancel not-yet-resolved SRS reviews for moveIndex >= `fromMoveIndex` (or ALL
   *  when undefined): drop their pendingSrsMap entries and terminate outbox SRS
   *  slots still in `awaiting_analysis`/`awaiting_retry`. Durable resolved slots
   *  (`pending`/`in_flight`/`awaiting_http_retry`) are UNTOUCHED — they POST
   *  regardless. The lifecycle calls this BEFORE awaited revert/new-game/new-drill
   *  network work so an analysis resolving during that async window cannot POST a
   *  review the flow is cancelling; `fullReset`/`partialReset` reuse it. */
  cancelPendingSrsReviews(fromMoveIndex?: number, reason = 'pruned'): void {
    const all = fromMoveIndex === undefined
    for (const [reqId, srs] of this.pendingSrsMap) {
      if (all || srs.moveIndex >= fromMoveIndex) this.pendingSrsMap.delete(reqId)
    }
    for (const entry of this.outbox) {
      if (entry.kind !== 'srs') continue
      if (!all && entry.moveIndex < fromMoveIndex) continue
      if (entry.status === 'awaiting_analysis' || entry.status === 'awaiting_retry') {
        this.markTerminal(entry, reason)
      }
    }
  }

  private fullReset(generation: number): void {
    this.nextDecisionIndex = 0
    this.committedDecisionIndex = 0
    this.currentGeneration = generation
    this.bumpUiEpoch()

    // Provisional SRS slots can never resolve now — terminate them. Durable
    // POST states (pending/awaiting_http_retry/in_flight) survive.
    this.cancelPendingSrsReviews(undefined, 'session_reset')

    for (const slot of this.frontier.values()) cancelTimer(slot.retryTimeoutId)
    this.frontier.clear()
    this.contextMap.clear()
    this.processedOutcomes.clear()
    this.terminatedRequests.clear()
    this.abandonedRequests.clear()
    this.blunderReserved = false
    // registrationSeqCounter is intentionally NOT reset (tab-lifetime monotonic).
  }

  private partialReset(k: number): void {
    this.nextDecisionIndex = Math.min(this.nextDecisionIndex, k)
    // committedDecisionIndex unchanged (monotonic boundary).
    this.bumpUiEpoch()

    this.cancelPendingSrsReviews(k, 'pruned')

    for (const [idx, slot] of this.frontier) {
      if (idx >= k) {
        cancelTimer(slot.retryTimeoutId)
        this.frontier.delete(idx)
      }
    }
    for (const [reqId, ctx] of this.contextMap) {
      if (ctx.moveIndex >= k) this.contextMap.delete(reqId)
    }
    for (const [key, moveIndex] of this.processedOutcomes) {
      if (moveIndex >= k) this.processedOutcomes.delete(key)
    }
    // Tombstones use deterministic, reusable synthetic request ids after a
    // takeback — a stale one would wrongly mark a reused id terminated/abandoned.
    for (const [reqId, info] of this.terminatedRequests) {
      if (info.moveIndex >= k) this.terminatedRequests.delete(reqId)
    }
    for (const [reqId, moveIndex] of this.abandonedRequests) {
      if (moveIndex >= k) this.abandonedRequests.delete(reqId)
    }
  }

  // --- Teardown ---

  /** Cancel every timer (analysis-retry, http-retry, frontier provisional) and
   *  bail any queued alert flush. */
  dispose(): void {
    this.disposed = true
    for (const entry of this.outbox) {
      if (entry.kind === 'srs') cancelTimer(entry.analysisRetryTimeoutId)
      cancelTimer(entry.httpRetryTimeoutId)
    }
    for (const slot of this.frontier.values()) cancelTimer(slot.retryTimeoutId)
    this.bumpUiEpoch()
  }

  // --- Test/inspection surface ---

  /** @internal — read-only snapshots for unit tests. */
  get committedIndex(): number {
    return this.committedDecisionIndex
  }
  get nextIndex(): number {
    return this.nextDecisionIndex
  }
  get registrationSeq(): number {
    return this.registrationSeqCounter
  }
  hasAbandonedRequest(requestId: string): boolean {
    return this.abandonedRequests.has(requestId)
  }
  frontierStatusAt(moveIndex: number): FrontierStatus | undefined {
    return this.frontier.get(moveIndex)?.status
  }
  outboxSnapshot(): ReadonlyArray<OutboxEntry> {
    return this.outbox
  }
}
