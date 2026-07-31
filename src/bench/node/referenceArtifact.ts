import type { CorpusPosition, CorpusTag } from './corpus'
import type {
  AcceptedReference,
  PositionReferences,
  ReferenceDepths,
} from './references'
import { compareRootScores } from '../../workers/compareRootScores'
import type { MoveClassification } from '../../workers/analysisUtils'
import {
  BROWSER_ENGINE_IDENTITY,
  BROWSER_ENGINE_RESOURCES,
} from '../../workers/browserEngineIdentity'
import { REFERENCE_DEPTHS } from './references'

export const REFERENCE_ARTIFACT_SCHEMA_VERSION = 1

export type ReferenceSource = {
  gitRevision: string
  gitDirty: boolean
  corpusSha256: string
  engineVersion: string
  engineBuild: string
  evalFileId: string
  npmPackage: string
  hashMb: 128
  threads: 1
  nodeVersion: string
  os: string
}

export type ThresholdReferenceRow = {
  positionId: string
  tags: CorpusTag[]
  status: 'adjudicated' | 'unadjudicable'
  classification: MoveClassification | null
  deltaCp: number | null
}

export type ReferenceSummary = {
  total: number
  adjudicated: number
  unadjudicable: number
  unadjudicableRate: number
  withinTenPercentGate: boolean
  unadjudicableIds: string[]
  observedMatchRate: { m: number | null; n: number }
  classifications: Record<MoveClassification, number>
  gKgiqBranch:
    | 'played-promoted'
    | 'named-best-equal'
    | 'unadjudicable'
    | 'unexpected'
  thresholdRows: ThresholdReferenceRow[]
}

export type ReferenceArtifact = {
  kind: 'ghostreplay-grade-references'
  schemaVersion: number
  complete: boolean
  createdAtIso: string
  source: ReferenceSource
  depths: ReferenceDepths
  rows: PositionReferences[]
  summary: ReferenceSummary
}

const classificationCounts = (): Record<MoveClassification, number> => ({
  best: 0,
  excellent: 0,
  good: 0,
  inaccuracy: 0,
  mistake: 0,
  blunder: 0,
})

const THRESHOLD_TAGS = new Set<CorpusTag>([
  'win-chance-02pct',
  'win-chance-10pct',
  'win-chance-20pct',
  'win-chance-30pct',
  'recording-50cp',
  'drill-0cp',
  'drill-10cp',
  'drill-25cp',
  'drill-50cp',
  'blunder-boundary-30pct',
])

export const summarizeReferences = (
  rows: readonly PositionReferences[],
  positions: readonly CorpusPosition[],
): ReferenceSummary => {
  const byId = new Map(positions.map((position) => [position.id, position]))
  const accepted = rows.filter(
    (row): row is PositionReferences & {
      adjudication: { status: 'adjudicated'; reference: AcceptedReference }
    } => row.adjudication.status === 'adjudicated',
  )
  const unadjudicableIds = rows
    .filter((row) => row.adjudication.status === 'unadjudicable')
    .map((row) => row.positionId)
    .sort()
  const classifications = classificationCounts()
  for (const row of accepted) {
    classifications[row.adjudication.reference.classification] += 1
  }
  const equal = accepted.filter((row) => row.adjudication.reference.pEqualsB).length
  const gKgiq = rows.find((row) => row.positionId === 'regression-g-kgiq-nb6')
  let gKgiqBranch: ReferenceSummary['gKgiqBranch'] = 'unexpected'
  if (!gKgiq || gKgiq.adjudication.status === 'unadjudicable') {
    gKgiqBranch = 'unadjudicable'
  } else if (gKgiq.adjudication.reference.bestMove === 'c4b6') {
    gKgiqBranch = 'played-promoted'
  } else if (
    gKgiq.adjudication.reference.bestMove === 'a5a6' &&
    gKgiq.adjudication.reference.deltaCp === 0 &&
    gKgiq.adjudication.reference.classification === 'excellent'
  ) {
    gKgiqBranch = 'named-best-equal'
  }

  const thresholdRows = rows.flatMap((row): ThresholdReferenceRow[] => {
    const position = byId.get(row.positionId)
    const tags = position?.tags.filter((tag) => THRESHOLD_TAGS.has(tag)) ?? []
    if (tags.length === 0) return []
    return [{
      positionId: row.positionId,
      tags,
      status: row.adjudication.status,
      classification:
        row.adjudication.status === 'adjudicated'
          ? row.adjudication.reference.classification
          : null,
      deltaCp:
        row.adjudication.status === 'adjudicated'
          ? row.adjudication.reference.deltaCp
          : null,
    }]
  })

  const total = rows.length
  const unadjudicable = unadjudicableIds.length
  const rate = total > 0 ? unadjudicable / total : 0
  return {
    total,
    adjudicated: accepted.length,
    unadjudicable,
    unadjudicableRate: rate,
    withinTenPercentGate: rate <= 0.10,
    unadjudicableIds,
    observedMatchRate: {
      m: accepted.length > 0 ? equal / accepted.length : null,
      n: accepted.length,
    },
    classifications,
    gKgiqBranch,
    thresholdRows,
  }
}

const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`)
      .join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

export const referenceArtifactProblems = (
  artifact: ReferenceArtifact,
  positions: readonly CorpusPosition[],
): string[] => {
  const problems: string[] = []
  if (artifact.kind !== 'ghostreplay-grade-references') problems.push('kind is invalid')
  if (artifact.schemaVersion !== REFERENCE_ARTIFACT_SCHEMA_VERSION) {
    problems.push(`schemaVersion is ${artifact.schemaVersion}`)
  }
  if (!Number.isFinite(Date.parse(artifact.createdAtIso))) problems.push('createdAtIso is invalid')
  if (!/^[0-9a-f]{40}$/.test(artifact.source.gitRevision)) {
    problems.push('source.gitRevision is not a full commit SHA')
  }
  if (artifact.source.engineVersion !== BROWSER_ENGINE_IDENTITY.engine_version) {
    problems.push('source.engineVersion does not match the production engine')
  }
  if (artifact.source.engineBuild !== BROWSER_ENGINE_IDENTITY.engine_build) {
    problems.push('source.engineBuild does not match the production WASM')
  }
  if (artifact.source.evalFileId !== BROWSER_ENGINE_IDENTITY.eval_file_id) {
    problems.push('source.evalFileId does not match the production net')
  }
  if (
    artifact.source.hashMb !== BROWSER_ENGINE_RESOURCES.hash_mb ||
    artifact.source.threads !== BROWSER_ENGINE_RESOURCES.threads
  ) {
    problems.push('source engine resources do not match production')
  }
  if (artifact.complete && canonicalJson(artifact.depths) !== canonicalJson(REFERENCE_DEPTHS)) {
    problems.push('complete artifact does not use the fixed depth-26/27 references')
  }
  if (artifact.complete && artifact.rows.length !== positions.length) {
    problems.push(
      `complete artifact has ${artifact.rows.length} rows for ${positions.length} corpus positions`,
    )
  }

  const expectedIds = new Set(positions.map((position) => position.id))
  const seen = new Set<string>()
  for (const row of artifact.rows) {
    if (!expectedIds.has(row.positionId)) problems.push(`unknown row ${row.positionId}`)
    if (seen.has(row.positionId)) problems.push(`duplicate row ${row.positionId}`)
    seen.add(row.positionId)

    for (const observation of [row.primary, row.bias]) {
      if (!Number.isFinite(observation.resetMs) || observation.resetMs < 0) {
        problems.push(`${row.positionId} has invalid resetMs`)
      }
    }
    if (row.adjudication.status === 'adjudicated') {
      const reference = row.adjudication.reference
      if (
        row.primary.verdict.status !== 'accepted' ||
        row.bias.verdict.status !== 'accepted'
      ) {
        problems.push(`${row.positionId} adjudicates a declined observation`)
      } else {
        if (
          row.primary.verdict.bestMove !== row.bias.verdict.bestMove ||
          row.primary.verdict.classification !== row.bias.verdict.classification
        ) {
          problems.push(`${row.positionId} adjudicates disagreeing observations`)
        }
        if (canonicalJson(reference) !== canonicalJson(row.primary.verdict)) {
          problems.push(`${row.positionId} adjudication does not preserve primary reference`)
        }
      }
      if (reference.deltaCp < 0) problems.push(`${row.positionId} has negative delta`)
      if (compareRootScores(reference.bestRoot, reference.playedRoot) < 0) {
        problems.push(`${row.positionId} played score outranks best score`)
      }
      if (reference.pEqualsB &&
          (reference.bestMove !== reference.playedMove ||
            reference.deltaCp !== 0 ||
            reference.classification !== 'best')) {
        problems.push(`${row.positionId} has incoherent P===B tuple`)
      }
    }
  }

  const recomputed = summarizeReferences(artifact.rows, positions)
  if (canonicalJson(recomputed) !== canonicalJson(artifact.summary)) {
    problems.push('summary is not what the reference rows produce')
  }
  return problems
}
