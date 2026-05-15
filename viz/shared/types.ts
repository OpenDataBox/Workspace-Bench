export type RunStatus = 'running' | 'success' | 'failed'

export interface RunSummary {
  id: string
  name: string
  status: RunStatus
  startedAt: string
  finishedAt?: string
  agent?: string
  model?: string
  run?: string
  backbone?: string
  modelLabel?: string
}

export type FsNodeType = 'dir' | 'file'

export interface FsNode {
  path: string
  name: string
  type: FsNodeType
  sizeBytes?: number
  mtimeMs?: number
  children?: FsNode[]
}

export interface FileReadResponse {
  path: string
  encoding: 'utf-8'
  truncated: boolean
  totalBytes?: number
  content: string
}

export interface ModelOutputChunk {
  runId: string
  seq: number
  ts: string
  level?: 'INFO' | 'WARN' | 'ERROR'
  text: string
}

export interface RunOutputResponse {
  runId: string
  format: 'text' | 'raw'
  content: string
}

export interface RunDetail {
  id: string
  name: string
  status: RunStatus
  startedAt: string
  finishedAt?: string
  agent?: string
  model?: string
  run?: string
  backbone?: string
  modelLabel?: string
  runPath?: string
  workspacePath?: string
  reportPath?: string
  agentLogPath?: string
  configPath?: string
  outputPath?: string
  outputDirPath?: string
  metadataPath?: string
}

export type TokenHistogramBucket = {
  start: number
  end: number
  count: number
}

export type EvalStatsResponse = {
  experiment?: string
  experiments: string[]
  group?: string
  groups: string[]
  tokenHistogram: {
    bucketSize: number
    buckets: TokenHistogramBucket[]
  }
  durationHistogram: {
    bucketSizeMs: number
    buckets: TokenHistogramBucket[]
  }
  modelCallsHistogram: {
    bucketSize: number
    buckets: TokenHistogramBucket[]
  }
  fullPairRatio: {
    ratio: number
    observedPairs: number
    expectedPairs: number
    agents: string[]
    models: string[]
  }
  evaluatedRatio: {
    ratio: number
    judgedRuns: number
    totalRuns: number
  }
  perfectRatio: {
    ratio: number
    perfectRuns: number
    judgedRuns: number
  }
  rubricsAccuracy: {
    ratio: number
    passedRubrics: number
    totalRubrics: number
    judgedRuns: number
  }
  groupRubricsAccuracy: Array<{
    group: string
    judgeModel: string
    ratio: number
    passedRubrics: number
    totalRubrics: number
    judgedRuns: number
    totalRuns: number
  }>
  totals: {
    totalRuns: number
    totalTokensKnown: number
    totalDurationsKnown: number
    totalModelCallsKnown: number
    totalModelCalls: number
    totalTokensUsed: number
    totalDurationMs: number
  }
}

export interface RunStepUsage {
  completionTokens: number
  promptTokens: number
  totalTokens: number
  cachedTokens?: number
  reasoningTokens?: number
}

export type RunStepType = 'system' | 'text' | 'tool' | 'result' | 'unknown'

export interface RunStepToolInfo {
  name: string
  callId?: string
  status?: string
  input?: unknown
  output?: unknown
  exitCode?: number
}

export interface RunStep {
  index: number
  type: RunStepType
  startTimeMs?: number
  endTimeMs?: number
  durationMs?: number
  offsetMs?: number
  turn?: number
  usage?: RunStepUsage
  text?: string
  tool?: RunStepToolInfo
}

export interface RunStepsSummary {
  toolCalls: number
  textMessages: number
  turns: number
  totalTokens?: number
  totalDurationMs?: number
  startedAt?: string
  finishedAt?: string
}

export interface RunStepsResponse {
  runId: string
  steps: RunStep[]
  summary: RunStepsSummary
}

export interface DependencyGraph {
  taskDir?: string
  agentKind?: string
  createdAt?: string
  nodes: string[]
  edges: [string, string][]
}

export interface RubricsJudge {
  taskId?: string
  agentKind?: string
  createdAt?: string
  rubrics: Array<{
    index: number
    rubric: string
    passed: boolean
    confidence?: number
    evidence?: string
  }>
  summary?: {
    total: number
    passed: number
    failed: number
  }
  judge?: {
    model?: string
    baseUrl?: string
    usage?: {
      completion_tokens?: number
      prompt_tokens?: number
      total_tokens?: number
      prompt_tokens_details?: { cached_tokens?: number }
      completion_tokens_details?: { reasoning_tokens?: number }
    }
    durationMs?: number
    rawResponseHead?: string
  }
}

export interface RunJudgeResponse {
  runId: string
  dependencyGraph?: DependencyGraph
  rubricsJudge?: RubricsJudge
  judgeModels: string[]
  selectedJudgeModel?: string
}
