/** 质量补采闭环 API：解析失败明细、签审、派单领用、规则试算、断点恢复、交接。 */
import { request } from './client'

export type Finding = {
  finding_id: string
  case_id: string
  source_id: string
  source_name: string
  file_path: string
  fingerprint: string
  review_state: string
  physical_line: number
  raw_text: string
  failure_type: 'line_empty' | 'format_mismatch' | 'field_missing' | 'ok'
  reason: string
  missing_fields: string[]
  parsed_fields: Record<string, string> | null
  now_parseable?: boolean
}

export type FieldCoverage = {
  filled: number
  denominator: number
  rate: number
}

export type CaseVersion = {
  version: number
  rule_id: string
  rule_version: number
  rule_note: string
  note: string
  created_at: string
  coverage: {
    denominator_lines: number
    physical_total: number
    per_field: Record<string, FieldCoverage>
    overall_rate: number
  }
  findings: Finding[]
}

export type ReviewRecord = {
  review_id: string
  case_id: string
  case_version: number
  rule_version: number
  finding_ids: string[]
  locations: { source_id: string; source_name: string; file_path: string; physical_line: number; missing_fields: string[]; finding_id: string }[]
  reviewer: string
  reason: string
  reviewed_at: string
  state: string
  snapshot: Finding[]
}

export type Assignment = {
  assignment_id: string
  case_id: string
  review_id: string
  job_id: string
  claim_keys: string[]
  locations: { source_id: string; source_name: string; file_path: string; physical_line: number; missing_fields: string[]; finding_id: string }[]
  rule_version_at_claim: number
  created_at: string
  receipt_state: 'pending' | 'received'
  receipt_at: string | null
}

export type HandoffLine = {
  finding_id: string
  physical_line: number
  file_path: string
  missing_fields_at_review: string[]
  now_parseable: boolean
  job_id: string | null
  assignment_id: string | null
  receipt_state: string | null
  instruction: string
}

export type RemediationCase = {
  case_id: string
  source_id: string
  source_name: string
  file_path: string
  physical_total: number
  current_rule_version: number
  failure_counter: Record<string, number>
  coverage: CaseVersion['coverage']
  review_count: number
  assignment_count: number
  created_at: string
  raw_lines: string[]
  versions: CaseVersion[]
  reviews: ReviewRecord[]
  assignments: Assignment[]
  handoffs: { handoff_id: string; created_at: string; lines: HandoffLine[]; scope_guard: string; reviewed_snapshot_kept: unknown[] }[]
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

export function listQualityCases() {
  return call<{ ok: boolean; items: RemediationCase[]; total: number }>('/api/quality/cases')
}

export function getQualityCase(caseId: string) {
  return call<{ ok: boolean; case: RemediationCase }>(`/api/quality/cases/${caseId}`)
}

export function createReview(caseId: string, findingIds: string[], reviewer: string, reason = '') {
  return call<{ ok: boolean; message: string; review: ReviewRecord }>(
    `/api/quality/cases/${caseId}/reviews`,
    { method: 'POST', body: JSON.stringify({ finding_ids: findingIds, reviewer, reason }) },
  )
}

export function runRetrial(caseId: string, ruleVersion: number, note = '') {
  return call<{ ok: boolean; message: string; version: CaseVersion }>(
    `/api/quality/cases/${caseId}/retrial`,
    { method: 'POST', body: JSON.stringify({ rule_version: ruleVersion, note }) },
  )
}

export function dispatchAssignment(caseId: string, jobId: string, reviewId: string | null = null) {
  return call<{ ok: boolean; message: string; assignment: Assignment }>(
    `/api/quality/cases/${caseId}/dispatch`,
    { method: 'POST', body: JSON.stringify({ job_id: jobId, review_id: reviewId }) },
  )
}

export function recordReceipt(caseId: string, assignmentId: string) {
  return call<{ ok: boolean; message: string; assignment: Assignment }>(
    `/api/quality/cases/${caseId}/receipts`,
    { method: 'POST', body: JSON.stringify({ assignment_id: assignmentId }) },
  )
}

export function reconcileCase(caseId: string) {
  return call<{ ok: boolean; reconcile: { claimed_claim_keys: string[]; claimed_locations: { physical_line: number }[]; assignments: Assignment[] } }>(
    `/api/quality/cases/${caseId}/reconcile`,
  )
}

export function buildHandoff(caseId: string) {
  return call<{ ok: boolean; handoff: RemediationCase['handoffs'][number] }>(
    `/api/quality/cases/${caseId}/handoff`,
    { method: 'POST', body: JSON.stringify({}) },
  )
}

export function continueCollection(caseId: string) {
  return call<{ ok: boolean; continuation: Record<string, unknown> }>(
    `/api/quality/cases/${caseId}/continue`,
  )
}
