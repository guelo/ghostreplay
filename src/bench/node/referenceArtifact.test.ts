import { describe, expect, it } from 'vitest'
import {
  BROWSER_ENGINE_IDENTITY,
  BROWSER_ENGINE_RESOURCES,
} from '../../workers/browserEngineIdentity'
import type { CorpusPosition } from './corpus'
import type {
  AcceptedReference,
  PositionReferences,
} from './references'
import { REFERENCE_DEPTHS } from './references'
import type { ReferenceArtifact } from './referenceArtifact'
import {
  REFERENCE_ARTIFACT_SCHEMA_VERSION,
  referenceArtifactProblems,
  summarizeReferences,
} from './referenceArtifact'

const position: CorpusPosition = {
  id: 'reference-row',
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  playedMove: 'e2e4',
  playerColor: 'white',
  phase: 'opening',
  tags: ['quiet', 'target-best', 'drill-0cp'],
  label: 'reference row',
  source: 'test',
}

const accepted: AcceptedReference = {
  status: 'accepted',
  bestMove: 'e2e4',
  playedMove: 'e2e4',
  bestRoot: { type: 'cp', value: 25 },
  playedRoot: { type: 'cp', value: 25 },
  bestPost: { type: 'cp', value: -25 },
  playedPost: { type: 'cp', value: -25 },
  deltaCp: 0,
  classification: 'best',
  bestLine: ['e2e4', 'e7e5'],
  resolutionUsed: false,
  pEqualsB: true,
}

const row: PositionReferences = {
  positionId: position.id,
  primary: { resetMs: 1, phases: [], verdict: accepted },
  bias: { resetMs: 1, phases: [], verdict: accepted },
  adjudication: { status: 'adjudicated', reference: accepted },
}

const artifact = (): ReferenceArtifact => {
  const rows = [row]
  return {
    kind: 'ghostreplay-grade-references',
    schemaVersion: REFERENCE_ARTIFACT_SCHEMA_VERSION,
    complete: true,
    createdAtIso: '2026-07-30T00:00:00.000Z',
    source: {
      gitRevision: 'a'.repeat(40),
      gitDirty: false,
      corpusSha256: 'b'.repeat(64),
      engineVersion: BROWSER_ENGINE_IDENTITY.engine_version,
      engineBuild: BROWSER_ENGINE_IDENTITY.engine_build,
      evalFileId: BROWSER_ENGINE_IDENTITY.eval_file_id,
      npmPackage: 'stockfish@18.0.7',
      hashMb: BROWSER_ENGINE_RESOURCES.hash_mb,
      threads: BROWSER_ENGINE_RESOURCES.threads,
      nodeVersion: 'v26.5.0',
      os: 'test',
    },
    depths: REFERENCE_DEPTHS,
    rows,
    summary: summarizeReferences(rows, [position]),
  }
}

describe('reference artifact validation', () => {
  it('accepts a complete, coherent artifact', () => {
    expect(referenceArtifactProblems(artifact(), [position])).toEqual([])
  })

  it('rejects a summary that does not match its rows', () => {
    const value = artifact()
    value.summary.adjudicated = 0

    expect(referenceArtifactProblems(value, [position])).toContain(
      'summary is not what the reference rows produce',
    )
  })

  it('rejects adjudication that bypasses dual-reference agreement', () => {
    const value = artifact()
    if (value.rows[0].bias.verdict.status === 'accepted') {
      value.rows[0].bias.verdict = {
        ...value.rows[0].bias.verdict,
        classification: 'good',
      }
    }

    expect(referenceArtifactProblems(value, [position])).toContain(
      'reference-row adjudicates disagreeing observations',
    )
  })

  it('rejects a complete artifact at diagnostic depths', () => {
    const value = artifact()
    value.depths = { ...REFERENCE_DEPTHS, primaryRoot: 4 }

    expect(referenceArtifactProblems(value, [position])).toContain(
      'complete artifact does not use the fixed depth-26/27 references',
    )
  })
})
