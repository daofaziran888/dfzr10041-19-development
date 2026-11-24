<template>
  <section class="page quality-page" data-module="quality_remediation">
    <header class="page-head">
      <div>
        <h2>质量补采闭环</h2>
        <p class="page-desc">
          解析失败明细按来源与物理行展开；签审结果驱动补采清单，同一物理位置只可出单一次，
          规则试算不覆盖已派范围。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn ghost" type="button" data-testid="reload" @click="reload">刷新台账</button>
      </div>
    </header>

    <div v-if="errorMessage" class="error-banner" data-testid="error-banner">{{ errorMessage }}</div>

    <template v-if="caseData">
      <!-- 案情概览 -->
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">物理行总数</span>
          <strong class="stat-value">{{ caseData.physical_total }}</strong>
        </article>
        <article class="stat-card" v-for="(label, key) in failureLabels" :key="key">
          <span class="stat-label">{{ label }}</span>
          <strong class="stat-value" :class="{ 'abnormal': key !== 'ok' }">
            {{ caseData.failure_counter[key] ?? 0 }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">签审 / 补采单</span>
          <strong class="stat-value">{{ caseData.review_count }} / {{ caseData.assignment_count }}</strong>
        </article>
      </div>

      <!-- 覆盖率：逐字段核对实际解析行 -->
      <section class="panel" data-testid="coverage-panel">
        <h3>
          字段覆盖率（当前规则 v{{ caseData.current_rule_version }}）
          <small>
            分母 {{ currentVersion.coverage.denominator_lines }} 行为实际解析行
            （物理 {{ currentVersion.coverage.physical_total }} 行中剔除空行与格式不符）
          </small>
        </h3>
        <table class="data-table coverage-table">
          <thead>
            <tr><th>必需字段</th><th>已填行数</th><th>分母（实际解析行）</th><th>覆盖率</th></tr>
          </thead>
          <tbody>
            <tr v-for="(cell, name) in currentVersion.coverage.per_field" :key="name"
                :data-testid="`coverage-${name}`" :class="{ 'low-coverage': cell.rate < 1 }">
              <td>{{ name }}</td>
              <td>{{ cell.filled }}</td>
              <td>{{ cell.denominator }}</td>
              <td>{{ formatRate(cell.rate) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="coverage-overall">
          整体字段覆盖率（按字段格计）：<strong>{{ formatRate(currentVersion.coverage.overall_rate) }}</strong>
          —— 不使用“解析成功”替代字段齐全。
        </p>
      </section>

      <!-- 解析失败明细：可展开的来源与物理位置 -->
      <section class="panel" data-testid="failures-panel">
        <h3>
          解析失败明细
          <div class="version-tabs">
            <button v-for="version in caseData.versions" :key="version.version"
                    class="tab" :class="{ active: version.version === selectedVersion }"
                    type="button" @click="selectedVersion = version.version">
              试算 v{{ version.version }}（规则 v{{ version.rule_version }}）
            </button>
          </div>
        </h3>
        <div class="source-tree">
          <div v-for="group in groupedFindings" :key="group.source_key" class="source-group">
            <button class="tree-toggle" type="button"
                    :data-testid="`toggle-${group.source_key}`"
                    @click="toggleGroup(group.source_key)">
              <span>{{ expanded[group.source_key] ? '▾' : '▸' }}</span>
              <strong>{{ group.source_key }}</strong>
              <span class="muted">（{{ group.findings.length }} 个物理行）</span>
            </button>
            <table v-if="expanded[group.source_key]" class="data-table finding-table">
              <thead>
                <tr>
                  <th>选择</th><th>物理行</th><th>失败类型</th><th>失败原因 / 缺字段组</th>
                  <th>原始文本</th><th>签审状态</th><th>规则更正后</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="finding in group.findings" :key="finding.finding_id"
                    :data-testid="`finding-${finding.physical_line}`"
                    :data-failure="finding.failure_type"
                    :class="{ selectable: finding.failure_type === 'field_missing' && finding.review_state === 'none' }">
                  <td>
                    <input v-if="finding.failure_type === 'field_missing' && finding.review_state === 'none'"
                           type="checkbox" :value="finding.finding_id" v-model="selectedFindingIds"
                           :data-testid="`check-${finding.finding_id}`" />
                    <span v-else class="muted">—</span>
                  </td>
                  <td><strong>L{{ finding.physical_line }}</strong></td>
                  <td><span class="badge" :class="`badge-${finding.failure_type}`">{{ failureLabels[finding.failure_type] }}</span></td>
                  <td class="reason-cell">
                    {{ finding.reason }}
                    <div v-if="finding.parsed_fields" class="parsed-fields">
                      已解析：{{ formatFields(finding.parsed_fields) }}
                    </div>
                  </td>
                  <td class="raw-cell">{{ finding.raw_text || '（空行）' }}</td>
                  <td>{{ reviewStateLabel(finding.review_state) }}</td>
                  <td>
                    <span v-if="finding.failure_type === 'field_missing'">
                      {{ finding.now_parseable ? '新规则可解析' : '仍需补采' }}
                    </span>
                    <span v-else class="muted">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- 签审 -->
      <section class="panel" data-testid="review-panel">
        <h3>补采签审（仅受理明确的缺字段组）</h3>
        <div class="action-bar">
          <span class="muted">已选 {{ selectedFindingIds.length }} 条字段缺失行</span>
          <input v-model="reviewer" placeholder="签审人" data-testid="reviewer-input" />
          <input v-model="reviewReason" placeholder="签审意见（可选）" class="grow"
                 data-testid="review-reason-input" />
          <button class="btn primary" type="button" :disabled="!selectedFindingIds.length"
                  data-testid="submit-review" @click="submitReview">
            提交签审
          </button>
        </div>
        <table v-if="caseData.reviews.length" class="data-table">
          <thead>
            <tr><th>签审单号</th><th>试算版本</th><th>物理行</th><th>缺字段</th><th>签审人</th><th>时间</th><th>明细快照</th></tr>
          </thead>
          <tbody>
            <tr v-for="review in caseData.reviews" :key="review.review_id" :data-testid="`review-${review.review_id}`">
              <td>{{ review.review_id }}</td>
              <td>v{{ review.case_version }} / 规则 v{{ review.rule_version }}</td>
              <td>{{ review.locations.map((l) => `L${l.physical_line}`).join('、') }}</td>
              <td>{{ [...new Set(review.locations.flatMap((l) => l.missing_fields))].join('、') }}</td>
              <td>{{ review.reviewer }}</td>
              <td>{{ review.reviewed_at }}</td>
              <td>已冻结 {{ review.snapshot.length }} 行签审前明细</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 派单领用 -->
      <section class="panel" data-testid="dispatch-panel">
        <h3>补采派单（同一物理位置只可出单一次）</h3>
        <div class="action-bar">
          <input v-model="jobId" placeholder="补采作业编号，如 JOB-2026-07" data-testid="job-input" />
          <button class="btn primary" type="button" :disabled="!jobId"
                  data-testid="dispatch-btn" @click="dispatch">领用待处理行</button>
          <button class="btn" type="button" data-testid="reconcile-btn" @click="reconcile">
            断点恢复核对
          </button>
        </div>
        <table v-if="caseData.assignments.length" class="data-table">
          <thead>
            <tr><th>补采单号</th><th>作业</th><th>来源签审</th><th>物理行</th><th>领用键</th><th>规则版本</th><th>回执</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="asg in caseData.assignments" :key="asg.assignment_id"
                :data-testid="`asg-${asg.assignment_id}`">
              <td>{{ asg.assignment_id }}</td>
              <td>{{ asg.job_id }}</td>
              <td>{{ asg.review_id }}</td>
              <td>{{ asg.locations.map((l) => `L${l.physical_line}`).join('、') }}</td>
              <td class="claim-cell">{{ asg.claim_keys.join('；') }}</td>
              <td>v{{ asg.rule_version_at_claim }}</td>
              <td>
                <span class="badge" :class="asg.receipt_state === 'received' ? 'badge-ok' : 'badge-pending'">
                  {{ asg.receipt_state === 'received' ? '回执已落' : '待回执' }}
                </span>
              </td>
              <td>
                <button v-if="asg.receipt_state !== 'received'" class="link" type="button"
                        @click="receipt(asg.assignment_id)">落派单回执</button>
                <span v-else class="muted">{{ asg.receipt_at }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="reconcileInfo" class="reconcile-box" data-testid="reconcile-box">
          <strong>恢复核对结果：</strong>实际已领用 {{ reconcileInfo.claimed_locations.length }} 个物理位置：
          {{ reconcileInfo.claimed_claim_keys.join('；') }}
          <p class="muted">重开后以此台账为准，不会把同一行重派给另一作业。</p>
        </div>
      </section>

      <!-- 规则试算与交接 -->
      <section class="panel" data-testid="retrial-panel">
        <h3>规则更正与交接</h3>
        <div class="action-bar">
          <button class="btn primary" type="button" data-testid="retrial-btn" @click="retrial">
            用更正后的规则 v{{ nextRuleVersion }} 重新试算
          </button>
          <button class="btn" type="button" data-testid="handoff-btn" :disabled="!caseData.reviews.length"
                  @click="handoff">生成交接记录</button>
        </div>
        <p class="muted">
          试算只产生新版本：旧失败明细、旧签审与已派补采单范围保持不变；
          已派出的补采作业不会被后来的规则试算悄悄改范围。
        </p>
        <div v-if="latestHandoff" class="handoff-box" data-testid="handoff-box">
          <h4>{{ latestHandoff.handoff_id }} · {{ latestHandoff.created_at }}</h4>
          <p class="scope-guard">{{ latestHandoff.scope_guard }}</p>
          <table class="data-table">
            <thead>
              <tr><th>物理行</th><th>签审时缺字段</th><th>新规则能否解析</th><th>领用作业</th><th>交接说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="line in latestHandoff.lines" :key="line.finding_id">
                <td>L{{ line.physical_line }}</td>
                <td>{{ line.missing_fields_at_review.join('、') }}</td>
                <td>{{ line.now_parseable ? '可处理' : '仍缺字段' }}</td>
                <td>{{ line.job_id ?? '未派单' }}</td>
                <td class="reason-cell">{{ line.instruction }}</td>
              </tr>
            </tbody>
          </table>
          <p class="muted">
            签审前实际看到的失败明细保留在各签审单快照中：
            {{ latestHandoff.reviewed_snapshot_kept.map((s: any) => `${s.review_id}（${s.snapshot_size} 行）`).join('；') }}
          </p>
        </div>
      </section>
    </template>

    <footer class="page-foot">
      <span v-if="caseData">案情 {{ caseData.case_id }} · {{ caseData.source_name }} · {{ caseData.file_path }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  buildHandoff, createReview, dispatchAssignment, getQualityCase,
  reconcileCase, recordReceipt, runRetrial,
} from '@/api/quality-service'
import type { Finding, RemediationCase, ReviewRecord } from '@/api/quality-service'

const failureLabels: Record<string, string> = {
  line_empty: '空行',
  format_mismatch: '格式不符',
  field_missing: '字段缺失',
  ok: '解析成功',
}

const caseData = ref<RemediationCase | null>(null)
const errorMessage = ref('')
const selectedVersion = ref(1)
const selectedFindingIds = ref<string[]>([])
const reviewer = ref('值班员')
const reviewReason = ref('')
const jobId = ref('')
const expanded = ref<Record<string, boolean>>({})
const reconcileInfo = ref<{ claimed_claim_keys: string[]; claimed_locations: { physical_line: number }[] } | null>(null)
const latestHandoff = ref<RemediationCase['handoffs'][number] | null>(null)

const currentVersion = computed(() => {
  const fallback = caseData.value?.versions[0]
  return caseData.value?.versions.find((v) => v.version === selectedVersion.value) ?? fallback!
})

const nextRuleVersion = computed(() => (caseData.value?.current_rule_version ?? 1) + 1)

const groupedFindings = computed(() => {
  const groups = new Map<string, { source_key: string; findings: Finding[] }>()
  for (const finding of currentVersion.value.findings) {
    const key = `${finding.source_name}｜${finding.file_path}`
    if (!groups.has(key)) groups.set(key, { source_key: key, findings: [] })
    groups.get(key)!.findings.push(finding)
  }
  return [...groups.values()]
})

const latestHandoffFromCase = computed(() =>
  caseData.value?.handoffs.length ? caseData.value.handoffs[caseData.value.handoffs.length - 1] : null)

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(2)}%`
}

function formatFields(fields: Record<string, string>): string {
  return Object.entries(fields).map(([key, value]) => `${key}=${value}`).join('，')
}

function reviewStateLabel(state: string): string {
  if (state === 'approved') return '已签审'
  return '未签审'
}

function toggleGroup(key: string): void {
  expanded.value[key] = !expanded.value[key]
}

async function reload(): Promise<void> {
  errorMessage.value = ''
  try {
    const payload = await getQualityCase('CASE-DEMO-01')
    caseData.value = payload.case
    selectedVersion.value = payload.case.current_rule_version
    latestHandoff.value = latestHandoffFromCase.value
    for (const group of groupedFindings.value) {
      if (expanded.value[group.source_key] === undefined) {
        expanded.value[group.source_key] = true
      }
    }
    selectedFindingIds.value = selectedFindingIds.value.filter((id) =>
      currentVersion.value.findings.some((f) => f.finding_id === id && f.review_state === 'none'),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量补采台账读取失败'
  }
}

async function submitReview(): Promise<void> {
  if (!caseData.value || !selectedFindingIds.value.length) return
  try {
    await createReview(caseData.value.case_id, [...selectedFindingIds.value],
      reviewer.value || '值班员', reviewReason.value)
    selectedFindingIds.value = []
    reviewReason.value = ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '签审失败'
  }
}

async function dispatch(): Promise<void> {
  if (!caseData.value || !jobId.value) return
  try {
    await dispatchAssignment(caseData.value.case_id, jobId.value)
    reconcileInfo.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派单失败'
  }
}

async function receipt(assignmentId: string): Promise<void> {
  if (!caseData.value) return
  try {
    await recordReceipt(caseData.value.case_id, assignmentId)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回执登记失败'
  }
}

async function retrial(): Promise<void> {
  if (!caseData.value) return
  try {
    await runRetrial(caseData.value.case_id, nextRuleVersion.value,
      'request_id 调整为非必需字段，缺该字段的行按新口径可解析')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '规则试算失败'
  }
}

async function reconcile(): Promise<void> {
  if (!caseData.value) return
  try {
    const payload = await reconcileCase(caseData.value.case_id)
    reconcileInfo.value = payload.reconcile
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复核对失败'
  }
}

async function handoff(): Promise<void> {
  if (!caseData.value) return
  try {
    const payload = await buildHandoff(caseData.value.case_id)
    latestHandoff.value = payload.handoff
    await reload()
    latestHandoff.value = payload.handoff
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接记录生成失败'
  }
}

onMounted(reload)

// 供单元测试/控制台核对类型用的显式导出
export type { ReviewRecord }
</script>

<style scoped>
.quality-page .panel {
  background: #fff;
  border: 1px solid #e3e6ee;
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 18px;
}
.quality-page h3 {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 0 0 12px;
  font-size: 16px;
}
.quality-page h3 small {
  font-weight: 400;
  color: #7a8194;
  font-size: 12px;
}
.version-tabs {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
.tab {
  border: 1px solid #c9cfdd;
  background: #f6f7fb;
  border-radius: 999px;
  padding: 4px 12px;
  cursor: pointer;
  font-size: 12px;
}
.tab.active {
  background: #2f6bff;
  border-color: #2f6bff;
  color: #fff;
}
.coverage-table td, .coverage-table th {
  font-size: 13px;
}
.low-coverage td {
  color: #c0392b;
  font-weight: 600;
}
.coverage-overall {
  margin: 10px 0 0;
  font-size: 13px;
  color: #444b5e;
}
.source-group {
  margin-bottom: 10px;
}
.tree-toggle {
  width: 100%;
  text-align: left;
  background: #f6f7fb;
  border: 1px solid #e3e6ee;
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
}
.muted {
  color: #8a91a5;
  font-size: 12px;
}
.finding-table {
  margin-top: 8px;
}
.finding-table tr.selectable:hover {
  background: #f2f6ff;
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.badge-line_empty { background: #eceff5; color: #66708a; }
.badge-format_mismatch { background: #fdecec; color: #c0392b; }
.badge-field_missing { background: #fff4e0; color: #b96b00; }
.badge-ok { background: #e6f7ec; color: #1d8348; }
.badge-pending { background: #fff4e0; color: #b96b00; }
.reason-cell {
  max-width: 320px;
}
.parsed-fields {
  margin-top: 4px;
  font-size: 12px;
  color: #5a6276;
}
.raw-cell {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  max-width: 360px;
  word-break: break-all;
}
.action-bar {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.action-bar input {
  padding: 7px 10px;
  border: 1px solid #c9cfdd;
  border-radius: 8px;
}
.action-bar .grow {
  flex: 1;
  min-width: 200px;
}
.claim-cell {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  max-width: 280px;
  word-break: break-all;
}
.reconcile-box, .handoff-box {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px dashed #8aa6ff;
  border-radius: 8px;
  background: #f7f9ff;
  font-size: 13px;
}
.scope-guard {
  color: #b96b00;
  font-weight: 600;
}
.error-banner {
  background: #fdecec;
  color: #c0392b;
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 14px;
}
.abnormal {
  color: #c0392b;
}
</style>
