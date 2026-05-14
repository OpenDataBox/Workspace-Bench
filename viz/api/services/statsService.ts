import fs from 'fs/promises'
import path from 'path'
import { safeResolve, toPosixRelPath } from '../utils/fsSafe.js'
import type { EvalStatsResponse, RunSummary, RubricsJudge } from '../../shared/types.js'
import { listRuns } from './runsService.js'

type AgentRunnerReport = {
  outputRoot?: string
  groups?: Array<{
    groupId?: string
    cases?: Array<{
      caseId?: string
      agents?: Array<{
        agentId?: string
        sandboxDir?: string
        totalTokens?: number
        durationMs?: number
        turns?: number
      }>
    }>
  }>
}

export async function getEvalStats(outputRootAbs: string, experiment?: string, group?: string, bucketSize?: number): Promise<EvalStatsResponse> {
  const runs = await listRuns(outputRootAbs)
  const groupKey = (r: RunSummary) => groupKeyFromRunRel(r.id)

  const experimentKey = (r: RunSummary) => experimentKeyFromRunRel(r.id)
  const experiments = Array.from(new Set(runs.map(experimentKey))).sort((a, b) => a.localeCompare(b))

  const selectedExperiment = pickExperiment(runs, experiments, groupKey, experimentKey, experiment, group)
  const experimentRuns = selectedExperiment ? runs.filter((r) => experimentKey(r) === selectedExperiment) : runs

  const groups = Array.from(new Set(experimentRuns.map(groupKey))).sort((a, b) => a.localeCompare(b))
  const selectedGroup = group && groups.includes(group) ? group : groups[0]
  const groupRuns = experimentRuns.filter((r) => groupKey(r) === selectedGroup)

  const reportMaps = await readAgentRunnerReportMaps(outputRootAbs)
  const tokenValues: number[] = []
  const durationValues: number[] = []
  const modelCallsValues: number[] = []
  const judged: Array<{ runId: string; perfect: boolean }> = []
  let passedRubrics = 0
  let totalRubrics = 0
  let totalModelCalls = 0
  let totalTokensUsed = 0
  let totalDurationMs = 0

  for (const r of groupRuns) {
    const token = reportMaps.tokens.get(r.id) ?? (await readRunTotalTokens(outputRootAbs, r.id))
    if (typeof token === 'number' && Number.isFinite(token) && token >= 0) {
      tokenValues.push(token)
      totalTokensUsed += token
    }

    const duration = reportMaps.durations.get(r.id) ?? (await readRunDurationMs(outputRootAbs, r.id))
    if (typeof duration === 'number' && Number.isFinite(duration) && duration >= 0) {
      durationValues.push(duration)
      totalDurationMs += duration
    }

    const modelCalls = reportMaps.modelCalls.get(r.id) ?? (await readRunModelCalls(outputRootAbs, r.id))
    if (typeof modelCalls === 'number' && Number.isFinite(modelCalls) && modelCalls >= 0) {
      modelCallsValues.push(modelCalls)
      totalModelCalls += modelCalls
    }

    const hasOut = await hasOutputDir(outputRootAbs, r.id)
    const judge = await readRubricsJudgeIfExists(outputRootAbs, r.id)
    if (judge && hasOut) {
      const total = judge.summary?.total
      const passed = judge.summary?.passed
      const perfect = typeof total === 'number' && typeof passed === 'number' && total > 0 && total === passed
      judged.push({ runId: r.id, perfect })

      if (typeof total === 'number' && typeof passed === 'number' && total >= 0 && passed >= 0) {
        passedRubrics += passed
        totalRubrics += total
      }
    }
  }

  const agents = Array.from(new Set(groupRuns.map((r) => r.agent).filter((v): v is string => typeof v === 'string' && v.trim().length > 0))).sort(
    (a, b) => a.localeCompare(b),
  )
  const modelOf = (r: RunSummary) => {
    const m = r.model ?? r.modelLabel ?? r.backbone
    return typeof m === 'string' && m.trim().length > 0 ? m : '(unknown)'
  }
  const models = Array.from(new Set(groupRuns.map(modelOf))).sort((a, b) => a.localeCompare(b))

  const observedPairs = new Set<string>()
  for (const r of groupRuns) {
    const a = r.agent
    if (!a) continue
    observedPairs.add(`${a}::${modelOf(r)}`)
  }
  const expectedPairs = agents.length * models.length
  const fullPairRatio = expectedPairs > 0 ? observedPairs.size / expectedPairs : 0

  const judgedRuns = judged.length
  let totalRuns = 0
  for (const r of groupRuns) {
    if (await hasOutputDir(outputRootAbs, r.id)) {
      totalRuns++
    }
  }
  const perfectRuns = judged.filter((j) => j.perfect).length

  const tokenBucketSize =
    typeof bucketSize === 'number' && Number.isFinite(bucketSize) && bucketSize > 0
      ? Math.floor(bucketSize)
      : chooseNiceBucketSize(tokenValues, 40)
  const durationBucketSizeMs = chooseNiceDurationBucketSizeMs(durationValues, 40)
  const modelCallsBucketSize = Math.max(1, chooseNiceBucketSize(modelCallsValues, 40))

  const histogram = buildHistogram(tokenValues, tokenBucketSize)
  const durationHistogram = buildHistogram(durationValues, durationBucketSizeMs)
  const modelCallsHistogram = modelCallsValues.length > 0 ? buildHistogram(modelCallsValues, modelCallsBucketSize) : { bucketSize: 1, buckets: [] }

  const groupRubricsAccuracy = await buildGroupRubricsAccuracy(outputRootAbs, experimentRuns, groups, groupKey)
  const rubricsAccuracyRatio = totalRubrics > 0 ? passedRubrics / totalRubrics : 0

  return {
    experiment: selectedExperiment,
    experiments,
    group: selectedGroup,
    groups,
    tokenHistogram: histogram,
    durationHistogram: { bucketSizeMs: durationBucketSizeMs, buckets: durationHistogram.buckets },
    modelCallsHistogram: { bucketSize: modelCallsBucketSize, buckets: modelCallsHistogram.buckets },
    fullPairRatio: {
      ratio: fullPairRatio,
      observedPairs: observedPairs.size,
      expectedPairs,
      agents,
      models,
    },
    evaluatedRatio: {
      ratio: totalRuns > 0 ? judgedRuns / totalRuns : 0,
      judgedRuns,
      totalRuns,
    },
    perfectRatio: {
      ratio: judgedRuns > 0 ? perfectRuns / judgedRuns : 0,
      perfectRuns,
      judgedRuns,
    },
    rubricsAccuracy: {
      ratio: rubricsAccuracyRatio,
      passedRubrics,
      totalRubrics,
      judgedRuns,
    },
    groupRubricsAccuracy,
    totals: {
      totalRuns,
      totalTokensKnown: tokenValues.length,
      totalDurationsKnown: durationValues.length,
      totalModelCallsKnown: modelCallsValues.length,
      totalModelCalls,
      totalTokensUsed,
      totalDurationMs,
    },
  }
}

function buildTokenHistogram(tokens: number[], bucketSize: number): { bucketSize: number; buckets: Array<{ start: number; end: number; count: number }> } {
  const size = Number.isFinite(bucketSize) && bucketSize > 0 ? Math.floor(bucketSize) : 1000
  if (tokens.length === 0) return { bucketSize: size, buckets: [] }

  const max = Math.max(...tokens)
  const bucketCount = Math.max(1, Math.ceil((max + 1) / size))
  const counts = new Array<number>(bucketCount).fill(0)
  for (const t of tokens) {
    const idx = Math.min(bucketCount - 1, Math.max(0, Math.floor(t / size)))
    counts[idx] += 1
  }

  const buckets = counts.map((count, i) => ({ start: i * size, end: (i + 1) * size, count }))
  return { bucketSize: size, buckets }
}

function buildHistogram(values: number[], bucketSize: number): { bucketSize: number; buckets: Array<{ start: number; end: number; count: number }> } {
  const size = Number.isFinite(bucketSize) && bucketSize > 0 ? Math.floor(bucketSize) : 1000
  if (values.length === 0 || size <= 0) return { bucketSize: size, buckets: [] }

  const max = Math.max(...values)
  const bucketCount = Math.max(1, Math.ceil((max + 1) / size))
  const counts = new Array<number>(bucketCount).fill(0)
  for (const t of values) {
    const idx = Math.min(bucketCount - 1, Math.max(0, Math.floor(t / size)))
    counts[idx] += 1
  }

  let first = counts.findIndex((c) => c > 0)
  let last = -1
  for (let i = counts.length - 1; i >= 0; i--) {
    if (counts[i] > 0) {
      last = i
      break
    }
  }
  if (first < 0 || last < 0) {
    return { bucketSize: size, buckets: [] }
  }
  first = Math.max(0, first - 1)
  last = Math.min(counts.length - 1, last + 1)

  const buckets = [] as Array<{ start: number; end: number; count: number }>
  for (let i = first; i <= last; i++) {
    buckets.push({ start: i * size, end: (i + 1) * size, count: counts[i] })
  }
  return { bucketSize: size, buckets }
}

function chooseNiceBucketSize(values: number[], targetBuckets: number): number {
  if (values.length === 0) return 1000
  const max = Math.max(...values)
  if (!Number.isFinite(max) || max <= 0) return 1000
  const raw = max / Math.max(1, targetBuckets)
  const step = niceStep(raw)
  return step > 0 ? step : 1
}

function chooseNiceDurationBucketSizeMs(values: number[], targetBuckets: number): number {
  if (values.length === 0) return 5000
  const max = Math.max(...values)
  if (!Number.isFinite(max) || max <= 0) return 5000
  const rawSec = (max / 1000) / Math.max(1, targetBuckets)
  const sec = Math.max(1, niceStep(rawSec))
  return sec * 1000
}

function niceStep(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return 1
  const exp = Math.floor(Math.log10(raw))
  const base = Math.pow(10, exp)
  const f = raw / base
  const step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10
  return step * base
}

function groupKeyFromRunRel(runRel: string): string {
  const parts = runRel.split('/').filter(Boolean)
  if (parts.length >= 2) return parts[parts.length - 2]
  return '(root)'
}

function experimentKeyFromRunRel(runRel: string): string {
  const parts = runRel.split('/').filter(Boolean)
  if (parts.length >= 3) return parts[0]
  return '(root)'
}

function pickExperiment(
  runs: RunSummary[],
  experiments: string[],
  groupKey: (r: RunSummary) => string,
  experimentKey: (r: RunSummary) => string,
  selectedExperiment?: string,
  selectedGroup?: string,
): string | undefined {
  if (experiments.length === 0) return undefined

  if (selectedExperiment && experiments.includes(selectedExperiment)) return selectedExperiment

  if (selectedGroup) {
    const byExp = new Map<string, { count: number; latest: number }>()
    for (const exp of experiments) byExp.set(exp, { count: 0, latest: 0 })

    for (const r of runs) {
      if (groupKey(r) !== selectedGroup) continue
      const exp = experimentKey(r)
      const item = byExp.get(exp)
      if (!item) continue
      item.count += 1
      const t = Date.parse(r.startedAt)
      if (Number.isFinite(t) && t > item.latest) item.latest = t
    }

    const best = Array.from(byExp.entries()).sort((a, b) => {
      if (b[1].count !== a[1].count) return b[1].count - a[1].count
      return b[1].latest - a[1].latest
    })[0]

    if (best && best[1].count > 0) return best[0]
  }

  const score = new Map<string, { groups: Set<string>; runs: number; latest: number }>()
  for (const exp of experiments) score.set(exp, { groups: new Set<string>(), runs: 0, latest: 0 })

  for (const r of runs) {
    const exp = experimentKey(r)
    const s = score.get(exp)
    if (!s) continue
    s.runs += 1
    s.groups.add(groupKey(r))
    const t = Date.parse(r.startedAt)
    if (Number.isFinite(t) && t > s.latest) s.latest = t
  }

  const best = Array.from(score.entries()).sort((a, b) => {
    if (b[1].groups.size !== a[1].groups.size) return b[1].groups.size - a[1].groups.size
    if (b[1].runs !== a[1].runs) return b[1].runs - a[1].runs
    return b[1].latest - a[1].latest
  })[0]
  return best ? best[0] : experiments[0]
}

async function readAgentRunnerReportMaps(outputRootAbs: string): Promise<{ tokens: Map<string, number>; durations: Map<string, number>; modelCalls: Map<string, number> }> {
  const tokens = new Map<string, number>()
  const durations = new Map<string, number>()
  const modelCalls = new Map<string, number>()

  const reportAbsList = await findFilesNamed(outputRootAbs, 'agent_runner_report.json', 2)
  for (const reportAbs of reportAbsList) {
    const report = await readJsonIfExists<AgentRunnerReport>(reportAbs)
    const groups = report?.groups
    if (!groups || groups.length === 0) continue
    for (const g of groups) {
      for (const c of g.cases ?? []) {
        for (const a of c.agents ?? []) {
          const abs = a.sandboxDir
          if (!abs) continue
          const rel = toPosixRelPath(outputRootAbs, abs)
          if (!rel) continue
          if (typeof a.totalTokens === 'number' && Number.isFinite(a.totalTokens) && a.totalTokens >= 0) tokens.set(rel, a.totalTokens)
          if (typeof a.durationMs === 'number' && Number.isFinite(a.durationMs) && a.durationMs >= 0) durations.set(rel, a.durationMs)
          
          if (a.turns != null && typeof a.turns === 'number' && Number.isFinite(a.turns) && a.turns >= 0) {
            modelCalls.set(rel, a.turns)
          }
        }
      }
    }
  }

  return { tokens, durations, modelCalls }
}

async function findFilesNamed(rootAbs: string, filename: string, maxDepth: number): Promise<string[]> {
  const out: string[] = []
  await walk(rootAbs, 0)
  return out

  async function walk(dirAbs: string, depth: number): Promise<void> {
    if (depth > maxDepth) return
    let entries: Array<import('fs').Dirent>
    try {
      entries = await fs.readdir(dirAbs, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      if (e.name.startsWith('.')) continue
      const childAbs = path.join(dirAbs, e.name)
      if (e.isDirectory()) {
        await walk(childAbs, depth + 1)
        continue
      }
      if (e.isFile() && e.name === filename) out.push(childAbs)
    }
  }
}

async function readRunTotalTokens(outputRootAbs: string, runRel: string): Promise<number | undefined> {
  const runAbs = safeResolve(outputRootAbs, runRel)

  const reportAbs = path.join(runAbs, 'batch_test_report.json')
  const report = await readJsonIfExists<{ tasks?: Array<{ llmCalls?: Array<{ usage?: { total_tokens?: number } }> }> }>(reportAbs)
  const fromReport = report?.tasks
    ?.flatMap((t) => t.llmCalls ?? [])
    .map((c) => c.usage?.total_tokens)
    .filter((n): n is number => typeof n === 'number')
    .reduce((a, b) => a + b, 0)
  if (typeof fromReport === 'number' && fromReport > 0) return fromReport

  const agentJsonAbs = path.join(runAbs, 'agent.json')
  const agentJson = await readJsonIfExists<{ totalTokens?: number; trace?: { summary?: { total_tokens?: number } } }>(agentJsonAbs)
  const fromAgentJson = agentJson?.totalTokens ?? agentJson?.trace?.summary?.total_tokens
  if (typeof fromAgentJson === 'number' && fromAgentJson > 0) return fromAgentJson

  return undefined
}

async function readRunDurationMs(outputRootAbs: string, runRel: string): Promise<number | undefined> {
  const runAbs = safeResolve(outputRootAbs, runRel)

  const agentJsonAbs = path.join(runAbs, 'agent.json')
  const agentJson = await readJsonIfExists<{ durationMs?: number; trace?: { summary?: { duration_seconds?: number } } }>(agentJsonAbs)
  const fromAgentJson = agentJson?.durationMs
  if (typeof fromAgentJson === 'number' && Number.isFinite(fromAgentJson) && fromAgentJson >= 0) return fromAgentJson
  const fromSummarySeconds = agentJson?.trace?.summary?.duration_seconds
  if (typeof fromSummarySeconds === 'number' && Number.isFinite(fromSummarySeconds) && fromSummarySeconds >= 0) return Math.round(fromSummarySeconds * 1000)

  const resultAbs = path.join(runAbs, 'result.json')
  const result = await readJsonIfExists<{ meta?: { durationMs?: number } }>(resultAbs)
  const fromResult = result?.meta?.durationMs
  if (typeof fromResult === 'number' && Number.isFinite(fromResult) && fromResult >= 0) return fromResult

  const reportAbs = path.join(runAbs, 'batch_test_report.json')
  const report = await readJsonIfExists<{ startedAt?: string; finishedAt?: string }>(reportAbs)
  if (report?.startedAt && report?.finishedAt) {
    const s = Date.parse(report.startedAt)
    const e = Date.parse(report.finishedAt)
    if (Number.isFinite(s) && Number.isFinite(e) && e >= s) return e - s
  }

  const agentLogAbs = path.join(runAbs, 'agent.log')
  const fromOpenclawLog = await readDurationFromOpenclawAgentLog(agentLogAbs)
  if (typeof fromOpenclawLog === 'number') return fromOpenclawLog

  return undefined
}

async function readRunModelCalls(outputRootAbs: string, runRel: string): Promise<number | undefined> {
  const runAbs = safeResolve(outputRootAbs, runRel)

  const reportAbs = path.join(runAbs, 'batch_test_report.json')
  const report = await readJsonIfExists<{ tasks?: Array<{ llmCalls?: Array<unknown>, turns?: number }> }>(reportAbs)
  let fromReport: number | undefined = undefined
  if (report?.tasks) {
    const hasLlmCallsField = report.tasks.some(t => 'llmCalls' in t)
    if (hasLlmCallsField) {
      fromReport = report.tasks.flatMap(t => t.llmCalls ?? []).length
    } else {
        const hasTurnsField = report.tasks.some(t => 'turns' in t)
        if (hasTurnsField) {
             fromReport = report.tasks.reduce((sum, t) => sum + (t.turns ?? 0), 0)
        }
    }
  }
  if (typeof fromReport === 'number' && fromReport >= 0) return fromReport

  const agentJsonAbs = path.join(runAbs, 'agent.json')
  const agentJson = await readJsonIfExists<{ turns?: number, trace?: { llm?: { calls?: Array<unknown> }, summary?: { llm_calls?: number }, executionTrace?: Array<{ role?: string, timestamp?: string }> } }>(agentJsonAbs)
  
  if (agentJson?.trace?.executionTrace && Array.isArray(agentJson.trace.executionTrace)) {
    const times = new Set<string>()
    for (const item of agentJson.trace.executionTrace) {
      if ((item.role === 'tool' || item.role === 'assistant') && typeof item.timestamp === 'string') {
        times.add(item.timestamp)
      }
    }
    if (times.size > 0) return times.size
  }

  if (typeof agentJson?.turns === 'number' && agentJson.turns >= 0) return agentJson.turns
  
  const fromAgentJsonCallsArray = agentJson?.trace?.llm?.calls?.length
  if (typeof fromAgentJsonCallsArray === 'number' && fromAgentJsonCallsArray >= 0) return fromAgentJsonCallsArray
  const fromAgentJsonSummary = agentJson?.trace?.summary?.llm_calls
  if (typeof fromAgentJsonSummary === 'number' && fromAgentJsonSummary >= 0) return fromAgentJsonSummary

  return undefined
}

async function readDurationFromOpenclawAgentLog(agentLogAbs: string): Promise<number | undefined> {
  try {
    const st = await fs.stat(agentLogAbs)
    if (st.size > 1_000_000) return undefined
    const raw = await fs.readFile(agentLogAbs, 'utf-8')
    const start = raw.indexOf('{')
    const end = raw.lastIndexOf('}')
    if (start < 0 || end < 0 || end <= start) return undefined
    const candidate = raw.slice(start, end + 1)
    const jsonText = candidate.replace(/\u001b\[[0-9;]*m/g, '')
    const obj = JSON.parse(jsonText) as any
    const v = obj?.meta?.durationMs
    if (typeof v === 'number' && Number.isFinite(v) && v >= 0) return v
    return undefined
  } catch {
    return undefined
  }
}

async function buildGroupRubricsAccuracy(
  outputRootAbs: string,
  runs: RunSummary[],
  groups: string[],
  groupKey: (r: RunSummary) => string,
): Promise<EvalStatsResponse['groupRubricsAccuracy']> {
  const totalRunsByGroup = new Map<string, number>()
  for (const g of groups) totalRunsByGroup.set(g, 0)
  for (const r of runs) {
    if (await hasOutputDir(outputRootAbs, r.id)) {
      const g = groupKey(r)
      totalRunsByGroup.set(g, (totalRunsByGroup.get(g) ?? 0) + 1)
    }
  }

  const acc = new Map<string, { group: string; judgeModel: string; passed: number; total: number; judgedRuns: number; totalRuns: number }>()

  for (const r of runs) {
    if (!(await hasOutputDir(outputRootAbs, r.id))) continue

    const g = groupKey(r)
    const groupTotalRuns = totalRunsByGroup.get(g) ?? 0

    const runAbs = safeResolve(outputRootAbs, r.id)
    const rubFiles = await listRubricsJudgeFiles(runAbs)
    if (rubFiles.length === 0) continue

    for (const f of rubFiles) {
      const judge = await readJsonIfExists<RubricsJudge>(f.abs)
      if (!judge || !Array.isArray(judge.rubrics)) continue
      const { total, passed } = calcRubricsPassedTotal(judge)
      if (total <= 0) continue

      const key = `${g}@@${f.modelLabel}`
      const item =
        acc.get(key) ??
        ({ group: g, judgeModel: f.modelLabel, passed: 0, total: 0, judgedRuns: 0, totalRuns: groupTotalRuns } as const)
      const next = {
        group: item.group,
        judgeModel: item.judgeModel,
        passed: item.passed + passed,
        total: item.total + total,
        judgedRuns: item.judgedRuns + 1,
        totalRuns: groupTotalRuns,
      }
      acc.set(key, next)
    }
  }

  return Array.from(acc.values())
    .map((v) => ({
      group: v.group,
      judgeModel: v.judgeModel,
      ratio: v.total > 0 ? v.passed / v.total : 0,
      passedRubrics: v.passed,
      totalRubrics: v.total,
      judgedRuns: v.judgedRuns,
      totalRuns: v.totalRuns,
    }))
    .sort((a, b) => {
      if (a.judgeModel !== b.judgeModel) return a.judgeModel.localeCompare(b.judgeModel)
      return a.group.localeCompare(b.group)
    })
}

type RubricsJudgeFile = { abs: string; fileName: string; modelLabel: string }

async function listRubricsJudgeFiles(runAbs: string): Promise<RubricsJudgeFile[]> {
  let entries: Array<import('fs').Dirent>
  try {
    entries = await fs.readdir(runAbs, { withFileTypes: true })
  } catch {
    return []
  }

  const out: RubricsJudgeFile[] = []
  for (const e of entries) {
    if (!e.isFile()) continue
    if (!e.name.startsWith('rubrics_judge')) continue
    if (!e.name.endsWith('.json')) continue
    const modelLabel = parseRubricsJudgeModelLabel(e.name)
    out.push({ abs: path.join(runAbs, e.name), fileName: e.name, modelLabel })
  }

  out.sort((a, b) => {
    if (a.modelLabel !== b.modelLabel) return a.modelLabel.localeCompare(b.modelLabel)
    return a.fileName.localeCompare(b.fileName)
  })
  return out
}

function parseRubricsJudgeModelLabel(fileName: string): string {
  const m = /^rubrics_judge--(.+)\.json$/i.exec(fileName)
  if (m && m[1] && m[1].trim().length > 0) return m[1]
  if (/^rubrics_judge\.json$/i.test(fileName)) return 'default'
  if (fileName.toLowerCase().startsWith('rubrics_judge--') && fileName.toLowerCase().endsWith('.json')) {
    const core = fileName.slice('rubrics_judge--'.length, fileName.length - '.json'.length)
    return core.trim().length > 0 ? core : 'default'
  }
  return 'default'
}

function calcRubricsPassedTotal(judge: RubricsJudge): { total: number; passed: number } {
  const totalFromSummary = judge.summary?.total
  const passedFromSummary = judge.summary?.passed
  if (typeof totalFromSummary === 'number' && typeof passedFromSummary === 'number' && totalFromSummary >= 0 && passedFromSummary >= 0) {
    return { total: totalFromSummary, passed: passedFromSummary }
  }
  const rubrics = Array.isArray(judge.rubrics) ? judge.rubrics : []
  const total = rubrics.length
  const passed = rubrics.filter((r) => r?.passed).length
  return { total, passed }
}

async function readRubricsJudgeIfExists(outputRootAbs: string, runRel: string): Promise<RubricsJudge | undefined> {
  const runAbs = safeResolve(outputRootAbs, runRel)
  const abs = (await findFirstJsonByPrefix(runAbs, 'rubrics_judge')) ?? path.join(runAbs, 'rubrics_judge.json')
  const judge = await readJsonIfExists<RubricsJudge>(abs)
  return judge && Array.isArray(judge.rubrics) ? judge : undefined
}

async function findFirstJsonByPrefix(dirAbs: string, prefix: string): Promise<string | undefined> {
  let entries: Array<import('fs').Dirent>
  try {
    entries = await fs.readdir(dirAbs, { withFileTypes: true })
  } catch {
    return undefined
  }
  const candidates = entries
    .filter((e) => e.isFile() && e.name.startsWith(prefix) && e.name.endsWith('.json'))
    .map((e) => e.name)
    .sort((a, b) => a.localeCompare(b))
  if (candidates.length === 0) return undefined
  return path.join(dirAbs, candidates[0])
}

async function hasOutputDir(outputRootAbs: string, runRel: string): Promise<boolean> {
  try {
    const runAbs = safeResolve(outputRootAbs, runRel)
    const st = await fs.stat(path.join(runAbs, 'output'))
    return st.isDirectory()
  } catch {
    return false
  }
}

async function readJsonIfExists<T>(absPath: string): Promise<T | undefined> {
  try {
    const raw = await fs.readFile(absPath, 'utf-8')
    return JSON.parse(raw) as T
  } catch {
    return undefined
  }
}
