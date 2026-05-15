import fs from 'fs/promises'
import path from 'path'
import { safeResolve, toPosixRelPath } from '../utils/fsSafe.js'
import type { RunStepsResponse, RunStep, RunStepToolInfo, RunStepUsage } from '../../shared/types.js'

// 这个文件负责把“评测运行的原始产物”转成前端可展示的时间线步骤：
// - 输入：output/<agent>/<run>/ 下的 batch_test_report.json、agent.log(JSONL)、result.json（openclaw 专用）等
// - 输出：RunStepsResponse.steps（按时间排序的 RunStep：system/text/tool/result/unknown）
//
// 关键点：
// 1) 工具调用在日志里是两条 event：tool_use running + tool_use completed，需要按 callID 合并。
// 2) Turn/usage 信息主要来自 batch_test_report.json 的 llmCalls；再把每个 step 映射到最近的 turn 窗口。
// 3) openclaw 的轨迹格式与通用 agent.log 不同，走单独的解析分支。

type BatchReport = {
  startedAt?: string
  finishedAt?: string
  totalDurationMs?: number
  tasks?: Array<{
    llmCalls?: Array<{
      turn?: number
      startedAt?: string
      finishedAt?: string
      durationMs?: number
      usage?: {
        completion_tokens?: number
        prompt_tokens?: number
        total_tokens?: number
        prompt_tokens_details?: { cached_tokens?: number }
        completion_tokens_details?: { reasoning_tokens?: number }
      }
    }>
  }>
}

type AgentLogEvent = {
  type?: string
  timestamp?: number
  usage?: {
    completion_tokens?: number
    prompt_tokens?: number
    total_tokens?: number
    prompt_tokens_details?: { cached_tokens?: number }
    completion_tokens_details?: { reasoning_tokens?: number }
  }
  part?: unknown
} & Record<string, unknown>

type AgentToolUsePart = {
  callID?: string
  tool?: string
  state?: {
    status?: string
    input?: unknown
    output?: unknown
    metadata?: { exit?: number }
  }
}

type AgentTextPart = {
  text?: string
  time?: { start?: number; end?: number }
}

type TurnWindow = {
  turn: number
  startMs: number
  endMs: number
  usage?: RunStepUsage
}

export async function getRunSteps(outputRootAbs: string, runId: string): Promise<RunStepsResponse> {
  const resolved = await resolveRun(outputRootAbs, runId)
  const runAbs = safeResolve(outputRootAbs, resolved.runRel)
  const reportAbs = path.join(runAbs, 'batch_test_report.json')
  const agentLogAbs = path.join(runAbs, 'agent.log')
  const resultAbs = path.join(runAbs, 'result.json')
  const agentJsonAbs = path.join(runAbs, 'agent.json')
  const sessionJsonlAbs = path.join(runAbs, 'session.jsonl')
  const openclawStateDirAbs = path.join(runAbs, 'openclaw_state')
  const rawOpenclawStateDirAbs = path.join(runAbs, 'raw', 'openclaw_state')

  const { agentName } = parseAgentFolder(resolved.agentFolder)
  const agentNameNorm = agentName.toLowerCase()
  const agentJsonSteps = await getStepsFromAgentJson(runId, agentJsonAbs)
  if (agentJsonSteps) return agentJsonSteps

  if (agentNameNorm === 'openclaw') {
    // openclaw 的结果优先走 result.json（包含结构化 trajectory）；若没有则退化到解析 agent.log。
    const fromResult = await getOpenclawRunStepsFromResultJson(outputRootAbs, runId, runAbs, resultAbs)
    if (fromResult) return fromResult

    const fromSession = await getOpenclawRunStepsFromSessionJsonl(runId, sessionJsonlAbs)
    if (fromSession) return fromSession

    const fromState = await getOpenclawRunStepsFromOpenclawState(runId, openclawStateDirAbs)
    if (fromState) return fromState
    const fromRawState = await getOpenclawRunStepsFromOpenclawState(runId, rawOpenclawStateDirAbs)
    if (fromRawState) return fromRawState

    try {
      const legacy = await getOpenclawRunSteps(outputRootAbs, runId, runAbs, agentLogAbs)
      const tools = legacy.steps?.filter((s) => s.type === 'tool').length ?? 0
      const texts = legacy.steps?.filter((s) => s.type === 'text').length ?? 0
      if (tools === 0 && texts === 0) {
        return {
          runId,
          steps: [
            {
              index: 1,
              type: 'text',
              text:
                `未找到可解析的 openclaw 轨迹文件，因此无法展示工具调用。` +
                `\n\n已尝试读取：` +
                `\n- agent.json (trace.trajectory / trace.executionTrace)` +
                `\n- result.json (trace.trajectory)` +
                `\n- session.jsonl` +
                `\n- openclaw_state/**/sessions/*.jsonl (含 raw/openclaw_state)` +
                `\n- agent.log` +
                `\n\n提示：如果你当前只落盘了 openclaw_state/sessions.json，请确保它引用的 sessionFile(.jsonl) 也被拷贝到输出目录。`,
            },
          ],
          summary: { toolCalls: 0, textMessages: 1, turns: legacy.summary?.turns ?? 0 },
        }
      }
      return legacy
    } catch (e) {
      return {
        runId,
        steps: [
          {
            index: 1,
            type: 'text',
            text:
              `未找到可解析的 openclaw 轨迹文件，因此无法展示工具调用。` +
              `\n\n已尝试读取：` +
              `\n- agent.json (trace.trajectory / trace.executionTrace)` +
              `\n- result.json (trace.trajectory)` +
              `\n- session.jsonl` +
              `\n- openclaw_state/**/sessions/*.jsonl (含 raw/openclaw_state)` +
              `\n- agent.log` +
              `\n\n原始错误：${(e as any)?.message ?? String(e)}`,
          },
        ],
        summary: { toolCalls: 0, textMessages: 1, turns: 0 },
      }
    }
  }

  const report = await readJsonIfExists<BatchReport>(reportAbs)
  const turns = buildTurnWindows(report)

  const rawLog = await readFileIfExists(agentLogAbs)
  const events = rawLog ? parseJsonl(rawLog) : []
  const { steps, firstTs } = buildSteps(events)
  assignTurnUsage(steps, turns)
  assignIndexesAndOffsets(steps, firstTs)

  const toolCalls = steps.filter((s) => s.type === 'tool').length
  const textMessages = steps.filter((s) => s.type === 'text').length
  const totalTokens = sumTotalTokens(turns)
  const totalDurationMs =
    report?.totalDurationMs ?? (report?.startedAt && report?.finishedAt ? Date.parse(report.finishedAt) - Date.parse(report.startedAt) : undefined)

  return {
    runId,
    steps,
    summary: {
      toolCalls,
      textMessages,
      turns: turns.length,
      totalTokens,
      totalDurationMs: typeof totalDurationMs === 'number' && !Number.isNaN(totalDurationMs) ? totalDurationMs : undefined,
      startedAt: report?.startedAt,
      finishedAt: report?.finishedAt,
    },
  }
}

type OpenclawAgentLog = {
  payloads?: Array<{ text?: string }>
  meta?: {
    durationMs?: number
    agentMeta?: { lastCallUsage?: { input?: number; output?: number; cacheRead?: number; cacheWrite?: number; total?: number } }
  }
}

type WorkspaceMetadata = {
  steps?: string[]
  created_at_ms?: number
  updated_at_ms?: number
}

type OpenclawResultJson = {
  runner?: string
  exitCode?: number
  cwd?: string
  trace?: {
    trajectory?: Array<{
      type?: string
      timestamp?: string
      text?: string
      tool?: string
      callID?: string
      input?: unknown
      output?: unknown
    }>
  }
  stdout?: string
}

type OpenclawSessionEvent =
  | {
      type: 'message'
      timestamp?: string
      message?: {
        role?: string
        content?: Array<any>
        toolCallId?: string
        toolName?: string
        details?: any
      }
    }
  | {
      type: string
      timestamp?: string
      [k: string]: any
    }

type AgentJsonToolCall = {
  function?: {
    name?: string
    arguments?: string
  }
  id?: string
  type?: string
}

type AgentJsonTraceItem = {
  type?: string
  role?: string
  content?: string
  tool_calls?: AgentJsonToolCall[]
  tool_call_id?: string
  timestamp?: string | number

  tool?: string
  callID?: string
  startedAt?: string
  finishedAt?: string
  durationMs?: number
  input?: unknown
  output?: unknown
  exitCode?: number
  status?: string
  turn?: number
}

type AgentJsonSummary = {
  start_time?: string
  end_time?: string
  duration_seconds?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
}

type AgentJsonFile = {
  durationMs?: number
  totalTokens?: number
  promptTokens?: number
  completionTokens?: number
  turns?: number
  trace?: {
    executionTrace?: AgentJsonTraceItem[]
    trajectory?: Array<{
      type?: string
      timestamp?: string | number
      text?: string
      tool?: string
      callID?: string
      input?: unknown
      output?: unknown
    }>
    summary?: AgentJsonSummary
  }
}

function getTurnsFromAgentJson(agentJson: AgentJsonFile | undefined): number {
  if (agentJson?.trace?.executionTrace && Array.isArray(agentJson.trace.executionTrace)) {
    const times = new Set<string>()
    for (const item of agentJson.trace.executionTrace) {
      if ((item.role === 'tool' || item.role === 'assistant') && typeof item.timestamp === 'string') {
        times.add(item.timestamp)
      }
    }
    if (times.size > 0) return times.size
  }
  return agentJson?.turns ?? 0
}

async function getStepsFromAgentJson(runId: string, agentJsonAbs: string): Promise<RunStepsResponse | undefined> {
  const agentJson = await readJsonIfExists<AgentJsonFile>(agentJsonAbs)
  const trajectory = agentJson?.trace?.trajectory
  if (trajectory && trajectory.length > 0) {
    return buildStepsFromTrajectory(runId, trajectory, agentJson)
  }

  const trace = agentJson?.trace?.executionTrace
  if (!trace || trace.length === 0) return undefined

  const steps: RunStep[] = []
  const byCallId = new Map<string, RunStep>()

  const startedAtMs = agentJson?.trace?.summary?.start_time ? Date.parse(agentJson.trace.summary.start_time) : undefined
  const finishedAtMs = agentJson?.trace?.summary?.end_time ? Date.parse(agentJson.trace.summary.end_time) : undefined
  const firstTraceTs = trace.map((t) => parseAnyTimestamp((t as any)?.timestamp)).find((t) => typeof t === 'number')
  const baseMs =
    typeof startedAtMs === 'number' && !Number.isNaN(startedAtMs)
      ? startedAtMs
      : typeof firstTraceTs === 'number'
        ? firstTraceTs
        : undefined

  for (let i = 0; i < trace.length; i++) {
    const item = trace[i]
    if (!item) continue

    const ts = parseAnyTimestamp((item as any).timestamp) ?? (typeof baseMs === 'number' ? baseMs + i : undefined)

    if (item.type === 'tool' && typeof item.tool === 'string') {
      const callId = item.callID
      const startMs = parseAnyTimestamp(item.startedAt) ?? ts
      const endMs = parseAnyTimestamp(item.finishedAt) ?? ts
      steps.push({
        index: 0,
        type: 'tool',
        startTimeMs: startMs,
        endTimeMs: endMs,
        durationMs: typeof item.durationMs === 'number' ? item.durationMs : calcDuration(startMs, endMs),
        offsetMs: typeof baseMs === 'number' && typeof startMs === 'number' ? Math.max(0, startMs - baseMs) : undefined,
        turn: item.turn,
        tool: {
          name: item.tool,
          callId,
          status: item.status,
          input: truncatePayload(item.input),
          output: truncatePayload((item.output as any)?.text ?? item.output),
          exitCode: typeof item.exitCode === 'number' ? item.exitCode : typeof (item.output as any)?.exitCode === 'number' ? (item.output as any).exitCode : undefined,
        },
      })
      continue
    }

    if ((item.role === 'system' || item.role === 'user') && typeof item.content === 'string' && item.content.trim().length > 0) {
      steps.push({
        index: 0,
        type: item.role === 'system' ? 'system' : 'text',
        startTimeMs: ts,
        endTimeMs: ts,
        durationMs: 0,
        offsetMs: typeof baseMs === 'number' && typeof ts === 'number' ? Math.max(0, ts - baseMs) : undefined,
        text: truncateText(item.content),
      })
      continue
    }

    if (item.role === 'assistant' && typeof item.content === 'string' && item.content.trim().length > 0 && (!item.tool_calls || item.tool_calls.length === 0)) {
      steps.push({
        index: 0,
        type: 'text',
        startTimeMs: ts,
        endTimeMs: ts,
        durationMs: 0,
        offsetMs: typeof baseMs === 'number' && typeof ts === 'number' ? Math.max(0, ts - baseMs) : undefined,
        text: truncateText(item.content),
      })
      continue
    }

    if (item.role === 'assistant' && Array.isArray(item.tool_calls) && item.tool_calls.length > 0) {
      for (const tc of item.tool_calls) {
        const callId = tc.id
        const name = tc.function?.name
        if (!callId || !name) continue

        const input = safeParseJson(tc.function?.arguments)
        const startTimeMs = ts
        byCallId.set(callId, {
          index: 0,
          type: 'tool',
          startTimeMs,
          endTimeMs: undefined,
          durationMs: undefined,
          offsetMs: typeof baseMs === 'number' && typeof startTimeMs === 'number' ? Math.max(0, startTimeMs - baseMs) : undefined,
          turn: undefined,
          tool: {
            name,
            callId,
            status: 'running',
            input: truncatePayload(input ?? tc.function?.arguments),
          },
        })
      }
      continue
    }

    if (item.role === 'tool' && typeof item.tool_call_id === 'string') {
      const callId = item.tool_call_id
      const existing = byCallId.get(callId)
      const endTimeMs = ts
      const toolName = existing?.tool?.name ?? 'tool'
      const nextStep: RunStep = existing
        ? {
            ...existing,
            endTimeMs,
            durationMs: calcDuration(existing.startTimeMs, endTimeMs),
            tool: mergeTool(existing.tool, {
              name: toolName,
              callId,
              status: 'completed',
              output: truncatePayload(item.content),
            }),
          }
        : {
            index: 0,
            type: 'tool',
            startTimeMs: undefined,
            endTimeMs,
            durationMs: undefined,
            offsetMs: typeof baseMs === 'number' && typeof endTimeMs === 'number' ? Math.max(0, endTimeMs - baseMs) : undefined,
            tool: {
              name: toolName,
              callId,
              status: 'completed',
              output: truncatePayload(item.content),
            },
          }
      byCallId.set(callId, nextStep)
      continue
    }
  }

  const toolSteps = Array.from(byCallId.values())
  toolSteps.sort((a, b) => (a.startTimeMs ?? Number.MAX_SAFE_INTEGER) - (b.startTimeMs ?? Number.MAX_SAFE_INTEGER))

  for (let i = 0; i < toolSteps.length; i++) {
    toolSteps[i].index = i + 1
    toolSteps[i].durationMs = calcDuration(toolSteps[i].startTimeMs, toolSteps[i].endTimeMs)
  }

  steps.push(...toolSteps)

  steps.sort((a, b) => (a.startTimeMs ?? Number.MAX_SAFE_INTEGER) - (b.startTimeMs ?? Number.MAX_SAFE_INTEGER))
  for (let i = 0; i < steps.length; i++) {
    steps[i].index = i + 1
  }

  const startedAt = typeof startedAtMs === 'number' && !Number.isNaN(startedAtMs) ? new Date(startedAtMs).toISOString() : undefined
  const finishedAt = typeof finishedAtMs === 'number' && !Number.isNaN(finishedAtMs) ? new Date(finishedAtMs).toISOString() : undefined
  const totalDurationMs =
    typeof agentJson?.durationMs === 'number'
      ? agentJson.durationMs
      : typeof startedAtMs === 'number' && typeof finishedAtMs === 'number'
        ? Math.max(0, finishedAtMs - startedAtMs)
        : undefined

  return {
    runId,
    steps,
    summary: {
      toolCalls: steps.filter((s) => s.type === 'tool').length,
      textMessages: steps.filter((s) => s.type === 'text' || s.type === 'system').length,
      turns: getTurnsFromAgentJson(agentJson),
      totalTokens: agentJson?.totalTokens ?? agentJson?.trace?.summary?.total_tokens,
      totalDurationMs,
      startedAt,
      finishedAt,
    },
  }
}

function buildStepsFromTrajectory(
  runId: string,
  trajectory: NonNullable<AgentJsonFile['trace']>['trajectory'],
  agentJson: AgentJsonFile,
): RunStepsResponse {
  const steps: RunStep[] = []
  const toolMap = new Map<string, RunStep>()

  let firstTs: number | undefined
  let lastTs: number | undefined

  for (const item of trajectory ?? []) {
    const ts = parseAnyTimestamp(item?.timestamp)
    if (typeof ts === 'number') {
      if (firstTs === undefined || ts < firstTs) firstTs = ts
      if (lastTs === undefined || ts > lastTs) lastTs = ts
    }

    const type = item?.type
    if (type === 'tool_call') {
      const callId = item?.callID
      const toolName = item?.tool
      if (!callId || !toolName) continue
      const existing = toolMap.get(callId)
      const s: RunStep = existing ?? {
        index: 0,
        type: 'tool',
        startTimeMs: ts,
        endTimeMs: undefined,
        durationMs: undefined,
        tool: { name: toolName, callId },
      }
      s.startTimeMs = s.startTimeMs ?? ts
      s.tool = {
        name: toolName,
        callId,
        status: 'running',
        input: truncatePayload(item?.input),
        output: s.tool?.output,
        exitCode: s.tool?.exitCode,
      }
      toolMap.set(callId, s)
      continue
    }

    if (type === 'tool_result') {
      const callId = item?.callID
      const toolName = item?.tool
      if (!callId || !toolName) continue
      const existing = toolMap.get(callId)
      const endMs = ts
      const outputObj = item?.output as any
      const exitCode = typeof outputObj?.exitCode === 'number' ? outputObj.exitCode : undefined
      const status = typeof outputObj?.status === 'string' ? outputObj.status : 'completed'

      const s: RunStep = existing ?? {
        index: 0,
        type: 'tool',
        startTimeMs: undefined,
        endTimeMs: endMs,
        durationMs: undefined,
        tool: { name: toolName, callId },
      }
      s.endTimeMs = s.endTimeMs ?? endMs
      s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
      s.tool = {
        name: toolName,
        callId,
        status,
        input: s.tool?.input,
        output: truncatePayload(item?.output),
        exitCode: s.tool?.exitCode ?? exitCode,
      }
      toolMap.set(callId, s)
      continue
    }

    if (type === 'thinking' || type === 'text' || type === 'error') {
      const t = item?.text
      if (typeof t === 'string' && t.trim().length > 0) {
        steps.push({
          index: 0,
          type: 'text',
          startTimeMs: ts,
          endTimeMs: ts,
          durationMs: 0,
          text: truncateText(t),
        })
      }
      continue
    }
  }

  const toolSteps = Array.from(toolMap.values()).map((s) => {
    s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
    return s
  })

  const all = [...steps, ...toolSteps]
  all.sort((a, b) => (a.startTimeMs ?? Number.MAX_SAFE_INTEGER) - (b.startTimeMs ?? Number.MAX_SAFE_INTEGER))
  assignIndexesAndOffsets(all, firstTs)

  const toolCalls = all.filter((s) => s.type === 'tool').length
  const textMessages = all.filter((s) => s.type === 'text').length
  const totalDurationMs =
    typeof agentJson?.durationMs === 'number'
      ? agentJson.durationMs
      : typeof firstTs === 'number' && typeof lastTs === 'number'
        ? Math.max(0, lastTs - firstTs)
        : undefined
  const startedAt = typeof firstTs === 'number' ? new Date(firstTs).toISOString() : agentJson.trace?.summary?.start_time
  const finishedAt = typeof lastTs === 'number' ? new Date(lastTs).toISOString() : agentJson.trace?.summary?.end_time

  return {
    runId,
    steps: all,
    summary: {
      toolCalls,
      textMessages,
      turns: getTurnsFromAgentJson(agentJson),
      totalTokens: agentJson?.totalTokens ?? agentJson?.trace?.summary?.total_tokens,
      totalDurationMs,
      startedAt,
      finishedAt,
    },
  }
}

function parseAnyTimestamp(v: unknown): number | undefined {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string') {
    const t = Date.parse(v)
    return Number.isFinite(t) ? t : undefined
  }
  return undefined
}

function safeParseJson(v?: string): unknown {
  if (typeof v !== 'string') return undefined
  try {
    return JSON.parse(v)
  } catch {
    return v
  }
}

async function getOpenclawRunStepsFromResultJson(
  outputRootAbs: string,
  runId: string,
  runAbs: string,
  resultAbs: string,
): Promise<RunStepsResponse | undefined> {
  const result = await readJsonIfExists<OpenclawResultJson>(resultAbs)
  if (!result) return undefined

  const trajectory = result.trace?.trajectory ?? []
  if (trajectory.length === 0) return undefined

  const steps: RunStep[] = []
  const toolMap = new Map<string, RunStep>()

  let firstTs: number | undefined
  let lastTs: number | undefined

  for (const item of trajectory) {
    const ts = item.timestamp ? Date.parse(item.timestamp) : undefined
    if (typeof ts === 'number' && !Number.isNaN(ts)) {
      if (firstTs === undefined || ts < firstTs) firstTs = ts
      if (lastTs === undefined || ts > lastTs) lastTs = ts
    }

    const type = item.type
    if (type === 'tool_call') {
      const callId = item.callID
      const toolName = item.tool
      if (!callId || !toolName) continue
      const existing = toolMap.get(callId)
      const s: RunStep = existing ?? {
        index: 0,
        type: 'tool',
        startTimeMs: ts,
        endTimeMs: undefined,
        durationMs: undefined,
        tool: { name: toolName, callId },
      }
      s.startTimeMs = s.startTimeMs ?? ts
      s.tool = {
        name: toolName,
        callId,
        status: 'running',
        input: truncatePayload(item.input),
        output: s.tool?.output,
        exitCode: s.tool?.exitCode,
      }
      toolMap.set(callId, s)
      continue
    }

    if (type === 'tool_result') {
      const callId = item.callID
      const toolName = item.tool
      if (!callId || !toolName) continue
      const existing = toolMap.get(callId)
      const endMs = ts
      const outputObj = item.output as any
      const exitCode = typeof outputObj?.exitCode === 'number' ? outputObj.exitCode : undefined
      const status = typeof outputObj?.status === 'string' ? outputObj.status : 'completed'

      const s: RunStep = existing ?? {
        index: 0,
        type: 'tool',
        startTimeMs: undefined,
        endTimeMs: endMs,
        durationMs: undefined,
        tool: { name: toolName, callId },
      }
      s.endTimeMs = s.endTimeMs ?? endMs
      s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
      s.tool = {
        name: toolName,
        callId,
        status,
        input: s.tool?.input,
        output: truncatePayload(item.output),
        exitCode: s.tool?.exitCode ?? exitCode,
      }
      toolMap.set(callId, s)
      continue
    }

    if (type === 'thinking') {
      steps.push({
        index: 0,
        type: 'text',
        startTimeMs: ts,
        endTimeMs: ts,
        durationMs: 0,
        text: truncateText(item.text),
      })
      continue
    }
  }

  const toolSteps = Array.from(toolMap.values()).map((s) => {
    s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
    return s
  })

  const all = [...steps, ...toolSteps]
  all.sort((a, b) => (a.startTimeMs ?? Number.MAX_SAFE_INTEGER) - (b.startTimeMs ?? Number.MAX_SAFE_INTEGER))
  assignIndexesAndOffsets(all, firstTs)

  const toolCalls = all.filter((s) => s.type === 'tool').length
  const textMessages = all.filter((s) => s.type === 'text').length
  const totalDurationMs = typeof firstTs === 'number' && typeof lastTs === 'number' ? Math.max(0, lastTs - firstTs) : undefined
  const startedAt = typeof firstTs === 'number' ? new Date(firstTs).toISOString() : undefined
  const finishedAt = typeof lastTs === 'number' ? new Date(lastTs).toISOString() : undefined

  return {
    runId,
    steps: all,
    summary: {
      toolCalls,
      textMessages,
      turns: 1,
      totalDurationMs,
      startedAt,
      finishedAt,
    },
  }
}

async function getOpenclawRunStepsFromSessionJsonl(runId: string, sessionJsonlAbs: string): Promise<RunStepsResponse | undefined> {
  try {
    const st = await fs.stat(sessionJsonlAbs)
    if (!st.isFile()) return undefined
  } catch {
    return undefined
  }

  const raw = await fs.readFile(sessionJsonlAbs, 'utf-8')
  const events = parseJsonl(raw) as any as OpenclawSessionEvent[]
  const steps = buildStepsFromOpenclawSessionEvents(events)
  if (steps.steps.length === 0) return undefined
  return { ...steps, runId }
}

async function getOpenclawRunStepsFromOpenclawState(runId: string, openclawStateDirAbs: string): Promise<RunStepsResponse | undefined> {
  try {
    const st = await fs.stat(openclawStateDirAbs)
    if (!st.isDirectory()) return undefined
  } catch {
    return undefined
  }

  const sessionsDirAbs = path.join(openclawStateDirAbs, 'agents', 'main', 'sessions')
  let entries: Array<import('fs').Dirent>
  try {
    entries = await fs.readdir(sessionsDirAbs, { withFileTypes: true })
  } catch {
    return undefined
  }

  const jsonlFiles = entries
    .filter((e) => e.isFile() && e.name.endsWith('.jsonl'))
    .map((e) => path.join(sessionsDirAbs, e.name))
  if (jsonlFiles.length === 0) return undefined

  let best: { abs: string; mtime: number } | undefined
  for (const abs of jsonlFiles) {
    try {
      const st = await fs.stat(abs)
      const m = st.mtimeMs
      if (!best || m > best.mtime) best = { abs, mtime: m }
    } catch {
      continue
    }
  }
  if (!best) return undefined

  const raw = await fs.readFile(best.abs, 'utf-8')
  const events = parseJsonl(raw) as any as OpenclawSessionEvent[]
  const steps = buildStepsFromOpenclawSessionEvents(events)
  if (steps.steps.length === 0) {
    return {
      runId,
      steps: [
        {
          index: 1,
          type: 'text',
          text: `openclaw_state 存在但未解析到可展示的 message/toolCall 事件。已尝试读取：${path.basename(best.abs)}`,
        },
      ],
      summary: { toolCalls: 0, textMessages: 1, turns: 0 },
    }
  }
  return { ...steps, runId }
}

function buildStepsFromOpenclawSessionEvents(events: OpenclawSessionEvent[]): Omit<RunStepsResponse, 'runId'> {
  const steps: RunStep[] = []
  const toolMap = new Map<string, RunStep>()
  let firstTs: number | undefined
  let lastTs: number | undefined

  for (const e of events) {
    if (!e || e.type !== 'message') continue
    const ts = e.timestamp ? Date.parse(e.timestamp) : undefined
    const tsMs = typeof ts === 'number' && Number.isFinite(ts) ? ts : undefined
    if (typeof tsMs === 'number') {
      if (firstTs === undefined || tsMs < firstTs) firstTs = tsMs
      if (lastTs === undefined || tsMs > lastTs) lastTs = tsMs
    }

    const m = (e as any).message
    const role = m?.role
    const contentArr: any[] = Array.isArray(m?.content) ? m.content : []

    if (role === 'assistant') {
      for (const c of contentArr) {
        if (c?.type === 'thinking' && typeof c.thinking === 'string' && c.thinking.trim().length > 0) {
          steps.push({ index: 0, type: 'text', startTimeMs: tsMs, endTimeMs: tsMs, durationMs: 0, text: truncateText(c.thinking) })
        }
        if (c?.type === 'text' && typeof c.text === 'string' && c.text.trim().length > 0) {
          steps.push({ index: 0, type: 'text', startTimeMs: tsMs, endTimeMs: tsMs, durationMs: 0, text: truncateText(c.text) })
        }
        if (c?.type === 'toolCall') {
          const callId = c.id
          const name = c.name
          if (!callId || !name) continue
          const existing = toolMap.get(callId)
          const s: RunStep = existing ?? { index: 0, type: 'tool', startTimeMs: tsMs, endTimeMs: undefined, durationMs: undefined, tool: { name, callId } }
          s.startTimeMs = s.startTimeMs ?? tsMs
          s.tool = {
            name,
            callId,
            status: 'running',
            input: truncatePayload(c.arguments),
            output: s.tool?.output,
            exitCode: s.tool?.exitCode,
          }
          toolMap.set(callId, s)
        }
      }
      continue
    }

    if (role === 'toolResult') {
      const callId = m?.toolCallId
      const name = m?.toolName
      if (!callId || !name) continue

      const outputText = contentArr.find((c) => c?.type === 'text')?.text
      const details = contentArr.find((c) => c?.details)?.details ?? m?.details
      const exitCode = typeof details?.exitCode === 'number' ? details.exitCode : undefined
      const status = typeof details?.status === 'string' ? details.status : 'completed'

      const existing = toolMap.get(callId)
      const s: RunStep = existing ?? { index: 0, type: 'tool', startTimeMs: undefined, endTimeMs: tsMs, durationMs: undefined, tool: { name, callId } }
      s.endTimeMs = s.endTimeMs ?? tsMs
      s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
      s.tool = {
        name,
        callId,
        status,
        input: s.tool?.input,
        output: truncatePayload(outputText ?? details?.aggregated ?? contentArr),
        exitCode: s.tool?.exitCode ?? exitCode,
      }
      toolMap.set(callId, s)
      continue
    }
  }

  const toolSteps = Array.from(toolMap.values()).map((s) => {
    s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
    return s
  })
  const all = [...steps, ...toolSteps]
  all.sort((a, b) => (a.startTimeMs ?? Number.MAX_SAFE_INTEGER) - (b.startTimeMs ?? Number.MAX_SAFE_INTEGER))
  assignIndexesAndOffsets(all, firstTs)

  return {
    steps: all,
    summary: {
      toolCalls: all.filter((s) => s.type === 'tool').length,
      textMessages: all.filter((s) => s.type === 'text').length,
      turns: 0,
      totalDurationMs: typeof firstTs === 'number' && typeof lastTs === 'number' ? Math.max(0, lastTs - firstTs) : undefined,
      startedAt: typeof firstTs === 'number' ? new Date(firstTs).toISOString() : undefined,
      finishedAt: typeof lastTs === 'number' ? new Date(lastTs).toISOString() : undefined,
    },
  }
}

async function getOpenclawRunSteps(
  outputRootAbs: string,
  runId: string,
  runAbs: string,
  agentLogAbs: string,
): Promise<RunStepsResponse> {
  const { metadata, workspaceRel } = await readWorkspaceMetadata(outputRootAbs, runAbs)
  const steps: RunStep[] = []

  const baseTime = metadata?.created_at_ms
  const metaSteps = metadata?.steps ?? []
  for (let i = 0; i < metaSteps.length; i++) {
    steps.push({
      index: i + 1,
      type: 'text',
      startTimeMs: typeof baseTime === 'number' ? baseTime : undefined,
      endTimeMs: typeof baseTime === 'number' ? baseTime : undefined,
      durationMs: 0,
      offsetMs: typeof baseTime === 'number' ? 0 : undefined,
      turn: 0,
      text: metaSteps[i],
    })
  }

  const log = await readFileIfExists(agentLogAbs)
  const parsed = log ? parseOpenclawAgentLog(log) : undefined
  const usage = parsed?.meta?.agentMeta?.lastCallUsage
  const durationMs = parsed?.meta?.durationMs
  const payloadText = parsed?.payloads?.map((p) => p.text).filter(Boolean).join('\n')

  steps.push({
    index: steps.length + 1,
    type: 'result',
    startTimeMs: typeof baseTime === 'number' ? baseTime : undefined,
    endTimeMs: typeof baseTime === 'number' && typeof durationMs === 'number' ? baseTime + durationMs : undefined,
    durationMs: typeof durationMs === 'number' ? durationMs : undefined,
    offsetMs: typeof baseTime === 'number' && typeof durationMs === 'number' ? durationMs : undefined,
    turn: 0,
    usage: usage
      ? {
          completionTokens: usage.output ?? 0,
          promptTokens: usage.input ?? 0,
          totalTokens: usage.total ?? (usage.input ?? 0) + (usage.output ?? 0),
          cachedTokens: usage.cacheRead,
        }
      : undefined,
    text: payloadText,
  })

  return {
    runId,
    steps,
    summary: {
      toolCalls: 0,
      textMessages: metaSteps.length,
      turns: 1,
      totalTokens: usage?.total,
      totalDurationMs: typeof durationMs === 'number' ? durationMs : undefined,
      startedAt: typeof baseTime === 'number' ? new Date(baseTime).toISOString() : undefined,
      finishedAt: typeof baseTime === 'number' && typeof durationMs === 'number' ? new Date(baseTime + durationMs).toISOString() : undefined,
    },
  }
}

async function readWorkspaceMetadata(
  outputRootAbs: string,
  runAbs: string,
): Promise<{ metadata?: WorkspaceMetadata; workspaceRel?: string }> {
  try {
    const entries = await fs.readdir(runAbs, { withFileTypes: true })
    const workspace = entries.find((e) => e.isDirectory() && e.name.startsWith('_'))
    if (!workspace) return {}
    const metaAbs = path.join(runAbs, workspace.name, 'metadata.json')
    const meta = await readJsonIfExists<WorkspaceMetadata>(metaAbs)
    return { metadata: meta, workspaceRel: toPosixRelPath(outputRootAbs, path.join(runAbs, workspace.name)) }
  } catch {
    return {}
  }
}

function parseAgentFolder(folder: string): { agentName: string } {
  const delim = folder.includes('----') ? '----' : folder.includes('--') ? '--' : undefined
  if (!delim) return { agentName: folder }
  const parts = folder.split(delim)
  const agentName = parts[0] || folder
  return { agentName }
}

function parseOpenclawAgentLog(raw: string): OpenclawAgentLog | undefined {
  const start = raw.indexOf('{')
  const end = raw.lastIndexOf('}')
  if (start < 0 || end < 0 || end <= start) return undefined
  const candidate = raw.slice(start, end + 1)
  const jsonText = stripAnsi(candidate)
  try {
    return JSON.parse(jsonText) as OpenclawAgentLog
  } catch {
    return undefined
  }
}

function stripAnsi(s: string): string {
  return s.replace(/\u001b\[[0-9;]*m/g, '')
}

function buildTurnWindows(report: BatchReport | undefined): TurnWindow[] {
  // batch_test_report.json 里会记录每次调用 LLM 的时间窗口与 token usage。
  // 这里把它们转成 [startMs, endMs] 的 turn window，后续把每个 step 挂到最近的 turn 上。
  const llmCalls = report?.tasks?.[0]?.llmCalls ?? []
  const windows: TurnWindow[] = []
  for (const c of llmCalls) {
    const turn = typeof c.turn === 'number' ? c.turn : windows.length
    const startMs = c.startedAt ? Date.parse(c.startedAt) : undefined
    const endMs = c.finishedAt ? Date.parse(c.finishedAt) : undefined
    if (!startMs || !endMs || Number.isNaN(startMs) || Number.isNaN(endMs)) continue
    const u = c.usage
    const usage: RunStepUsage | undefined = u
      ? {
          completionTokens: u.completion_tokens ?? 0,
          promptTokens: u.prompt_tokens ?? 0,
          totalTokens: u.total_tokens ?? 0,
          cachedTokens: u.prompt_tokens_details?.cached_tokens,
          reasoningTokens: u.completion_tokens_details?.reasoning_tokens,
        }
      : undefined
    windows.push({ turn, startMs, endMs, usage })
  }
  return windows.sort((a, b) => a.turn - b.turn)
}

function buildSteps(events: AgentLogEvent[]): { steps: RunStep[]; firstTs?: number } {
  // agent.log 是 JSONL，每行一条事件。这里会把事件分成两类：
  // - tool_use：按 callID 合并成一个 tool step（start/end/输入输出/exitCode）
  // - 其他：text/system_init/result/unknown 直接映射成 step
  const toolMap = new Map<string, RunStep>()
  const nonToolSteps: RunStep[] = []
  let firstTs: number | undefined

  for (const ev of events) {
    const ts = typeof ev.timestamp === 'number' ? ev.timestamp : undefined
    if (typeof ts === 'number' && (firstTs === undefined || ts < firstTs)) firstTs = ts

    const t = typeof ev.type === 'string' ? ev.type : 'unknown'
    if (t === 'tool_use') {
      // tool_use 事件包含 callID/tool/state：
      // - running：通常意味着“开始执行工具”
      // - completed：意味着“拿到工具结果”，同时携带 output/exit code
      const part = (ev.part ?? {}) as AgentToolUsePart
      const callId = part.callID
      const toolName = part.tool
      if (!callId || !toolName) continue

      const state = part.state ?? {}
      const status = state.status
      const existing = toolMap.get(callId)
      const tool: RunStepToolInfo = {
        name: toolName,
        callId,
        status,
        input: truncatePayload(state.input),
        output: truncatePayload(state.output),
        exitCode: state.metadata?.exit,
      }

      if (!existing) {
        // 首次看到这个 callId：直接建一个 tool step。
        const s: RunStep = {
          index: 0,
          type: 'tool',
          startTimeMs: status === 'completed' ? undefined : ts,
          endTimeMs: status === 'completed' ? ts : undefined,
          durationMs: undefined,
          tool,
        }
        toolMap.set(callId, s)
        continue
      }

      existing.tool = mergeTool(existing.tool, tool)
      if (status === 'running' && ts && (existing.startTimeMs === undefined || ts < existing.startTimeMs)) {
        existing.startTimeMs = ts
      }
      if (status === 'completed' && ts && (existing.endTimeMs === undefined || ts > existing.endTimeMs)) {
        existing.endTimeMs = ts
      }
      existing.durationMs = calcDuration(existing.startTimeMs, existing.endTimeMs)
      continue
    }

    if (t === 'text') {
      // 文本事件：time.start/time.end 优先，其次用 event.timestamp 兜底。
      const part = (ev.part ?? {}) as AgentTextPart
      const startMs = part.time?.start ?? ts
      const endMs = part.time?.end ?? ts
      nonToolSteps.push({
        index: 0,
        type: 'text',
        startTimeMs: typeof startMs === 'number' ? startMs : undefined,
        endTimeMs: typeof endMs === 'number' ? endMs : undefined,
        durationMs: calcDuration(startMs, endMs),
        text: truncateText(part.text),
      })
      continue
    }

    if (t === 'system_init') {
      // 初始化事件通常包含 cwd、model、工具列表等；前端作为第一条 step 展示。
      nonToolSteps.push({
        index: 0,
        type: 'system',
        startTimeMs: ts,
        endTimeMs: ts,
        durationMs: 0,
        text: truncateText(JSON.stringify(ev.part ?? ev, null, 2)),
      })
      continue
    }

    if (t === 'result') {
      // result 事件常见于一次运行结束，可能附带 usage；这里把 usage 单独抽取出来。
      const usage = ev.usage
      const u: RunStepUsage | undefined = usage
        ? {
            completionTokens: usage.completion_tokens ?? 0,
            promptTokens: usage.prompt_tokens ?? 0,
            totalTokens: usage.total_tokens ?? 0,
            cachedTokens: usage.prompt_tokens_details?.cached_tokens,
            reasoningTokens: usage.completion_tokens_details?.reasoning_tokens,
          }
        : undefined
      nonToolSteps.push({
        index: 0,
        type: 'result',
        startTimeMs: ts,
        endTimeMs: ts,
        durationMs: 0,
        usage: u,
        text: truncateText(JSON.stringify({ subtype: ev.subtype, durationMs: ev.durationMs, usage: ev.usage }, null, 2)),
      })
      continue
    }

    const payload = ev.part ?? ev
    nonToolSteps.push({
      index: 0,
      type: 'unknown',
      startTimeMs: ts,
      endTimeMs: ts,
      durationMs: 0,
      text: truncateText(typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)),
    })
  }

  const toolSteps = Array.from(toolMap.values()).map((s) => {
    s.durationMs = calcDuration(s.startTimeMs, s.endTimeMs)
    return s
  })

  const all = [...nonToolSteps, ...toolSteps]
  all.sort((a, b) => (a.startTimeMs ?? Number.MAX_SAFE_INTEGER) - (b.startTimeMs ?? Number.MAX_SAFE_INTEGER))
  return { steps: all, firstTs }
}

function assignTurnUsage(steps: RunStep[], turns: TurnWindow[]) {
  // 把每个 step 归属到某个 turn：优先命中窗口区间，否则选最近的窗口边界。
  if (turns.length === 0) return
  for (const s of steps) {
    const t = findTurnForTimestamp(turns, s.startTimeMs)
    if (!t) continue
    s.turn = t.turn
    s.usage = t.usage
  }
}

function assignIndexesAndOffsets(steps: RunStep[], firstTs?: number) {
  // 前端展示用：
  // - index：步骤序号（从 1 开始）
  // - offsetMs：相对第一条事件的时间偏移
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i]
    s.index = i + 1
    if (typeof firstTs === 'number' && typeof s.startTimeMs === 'number') {
      s.offsetMs = s.startTimeMs - firstTs
    }
  }
}

function findTurnForTimestamp(turns: TurnWindow[], ts?: number): TurnWindow | undefined {
  if (!ts) return undefined
  for (const t of turns) {
    if (ts >= t.startMs && ts <= t.endMs) return t
  }
  let best: { d: number; t: TurnWindow } | undefined
  for (const t of turns) {
    const d = Math.min(Math.abs(ts - t.startMs), Math.abs(ts - t.endMs))
    if (!best || d < best.d) best = { d, t }
  }
  return best?.t
}

function mergeTool(a: RunStepToolInfo | undefined, b: RunStepToolInfo): RunStepToolInfo {
  if (!a) return b
  return {
    name: b.name || a.name,
    callId: a.callId ?? b.callId,
    status: preferStatus(a.status, b.status),
    input: a.input ?? b.input,
    output: b.output ?? a.output,
    exitCode: a.exitCode ?? b.exitCode,
  }
}

function preferStatus(a?: string, b?: string): string | undefined {
  if (a === 'completed' || b === 'completed') return 'completed'
  return b ?? a
}

function calcDuration(start?: number, end?: number): number | undefined {
  if (typeof start !== 'number' || typeof end !== 'number') return undefined
  return Math.max(0, end - start)
}

function truncateText(v: unknown, maxChars = 8000): string | undefined {
  if (typeof v !== 'string') return undefined
  if (v.length <= maxChars) return v
  const head = v.slice(0, maxChars)
  return `${head}\n…(truncated, omitted ${v.length - maxChars} chars)`
}

function truncatePayload(v: unknown): unknown {
  if (typeof v === 'string') return truncateText(v, 8000)
  if (Array.isArray(v)) return truncateArray(v)
  if (v && typeof v === 'object') return deepTruncateObject(v as Record<string, unknown>, 8000)
  return v
}

function deepTruncateObject(obj: Record<string, unknown>, maxChars: number): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  const entries = Object.entries(obj)
  for (const [k, v] of entries) {
    if (k === 'content' && typeof v === 'string') {
      out[k] = truncateText(v, Math.min(maxChars, 2000))
      continue
    }
    if (typeof v === 'string') {
      out[k] = truncateText(v, maxChars)
      continue
    }
    if (Array.isArray(v)) {
      out[k] = truncateArray(v)
      continue
    }
    if (v && typeof v === 'object') {
      out[k] = deepTruncateObject(v as Record<string, unknown>, maxChars)
      continue
    }
    out[k] = v
  }
  return out
}

function truncateArray(arr: unknown[]): unknown[] {
  if (arr.length <= 200) return arr
  return [...arr.slice(0, 200), `…(truncated, omitted ${arr.length - 200} items)`]
}

function parseJsonl(raw: string): AgentLogEvent[] {
  // agent.log 约定为 JSONL；如果某行不是合法 JSON，也不会让整个解析失败。
  // 解析失败的行以 type=unknown 的形式保留，方便前端定位异常输出。
  const lines = raw.split('\n')
  const events: AgentLogEvent[] = []
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      events.push(JSON.parse(trimmed) as AgentLogEvent)
    } catch {
      events.push({ type: 'unknown', part: { raw: truncateText(trimmed) } })
    }
  }
  return events
}

async function readJsonIfExists<T>(absPath: string): Promise<T | undefined> {
  try {
    const raw = await fs.readFile(absPath, 'utf-8')
    return JSON.parse(raw) as T
  } catch {
    return undefined
  }
}

async function readFileIfExists(absPath: string): Promise<string | undefined> {
  try {
    return await fs.readFile(absPath, 'utf-8')
  } catch {
    return undefined
  }
}

function sumTotalTokens(turns: TurnWindow[]): number | undefined {
  if (turns.length === 0) return undefined
  let sum = 0
  for (const t of turns) {
    sum += t.usage?.totalTokens ?? 0
  }
  return sum
}

type ResolvedRun = {
  runRel: string
  agentFolder: string
  runFolder: string
}

function splitRunId(runId: string): { agentFolder?: string; runFolder?: string } {
  const parts = runId.split('::').filter(Boolean)
  if (parts.length >= 2) {
    return { agentFolder: parts[0], runFolder: parts.slice(1).join('::') }
  }

  const slash = runId.split('/').filter(Boolean)
  if (slash.length === 2) {
    return { agentFolder: slash[0], runFolder: slash[1] }
  }
  return {}
}

async function resolveRun(outputRootAbs: string, runId: string): Promise<ResolvedRun> {
  const parsed = splitRunId(runId)
  if (parsed.agentFolder && parsed.runFolder) {
    const candidateRel = path.posix.join(parsed.agentFolder, parsed.runFolder)
    const candidateAbs = safeResolve(outputRootAbs, candidateRel)
    const st = await fs.stat(candidateAbs)
    if (!st.isDirectory()) throw new Error(`Run not found: ${runId}`)
    return { runRel: candidateRel, agentFolder: parsed.agentFolder, runFolder: parsed.runFolder }
  }

  const slash = runId.split('/').filter(Boolean)
  if (slash.length >= 2) {
    const runRel = slash.join('/')
    const candidateAbs = safeResolve(outputRootAbs, runRel)
    const st = await fs.stat(candidateAbs)
    if (st.isDirectory()) {
      const agentFolder = slash[slash.length - 2]
      const runFolder = slash[slash.length - 1]
      return { runRel, agentFolder, runFolder }
    }
  }

  const legacyRun = runId
  const runs = await findRunDirs(outputRootAbs)
  const found = runs.find((r) => r.runFolder === legacyRun)
  if (found) return { runRel: found.runRel, agentFolder: found.agentFolder, runFolder: found.runFolder }

  throw new Error(`Run not found: ${runId}`)
}

type FoundRun = {
  runAbs: string
  runRel: string
  agentFolder: string
  runFolder: string
}

async function findRunDirs(outputRootAbs: string): Promise<FoundRun[]> {
  const out: FoundRun[] = []
  await walk(outputRootAbs, 0)
  return out

  async function walk(dirAbs: string, depth: number): Promise<void> {
    if (depth > 3) return
    let entries: Array<import('fs').Dirent>
    try {
      entries = await fs.readdir(dirAbs, { withFileTypes: true })
    } catch {
      return
    }

    for (const e of entries) {
      if (!e.isDirectory()) continue
      if (e.name.startsWith('.')) continue
      const childAbs = path.join(dirAbs, e.name)
      if (await isRunDir(childAbs)) {
        const runRel = toPosixRelPath(outputRootAbs, childAbs)
        const parts = runRel.split('/').filter(Boolean)
        if (parts.length < 2) continue
        out.push({
          runAbs: childAbs,
          runRel,
          agentFolder: parts[parts.length - 2],
          runFolder: parts[parts.length - 1],
        })
        continue
      }
      await walk(childAbs, depth + 1)
    }
  }
}

async function isRunDir(dirAbs: string): Promise<boolean> {
  const markers = ['agent.log', 'agent.json', 'result.json', 'output.json', 'batch_test_report.json', 'session.jsonl', 'metadata.json']
  for (const name of markers) {
    try {
      const st = await fs.stat(path.join(dirAbs, name))
      if (st.isFile()) return true
    } catch {
      continue
    }
  }
  return false
}
