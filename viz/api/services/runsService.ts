import fs from 'fs/promises'
import fsSync from 'fs'
import path from 'path'
import { safeResolve, toPosixRelPath } from '../utils/fsSafe.js'
import type { RunDetail, RunOutputResponse, RunStatus, RunSummary } from '../../shared/types.js'

type BatchReport = {
  startedAt?: string
  finishedAt?: string
  summary?: { passed?: number; failed?: number; total?: number }
  tasks?: Array<{ status?: string; stdout?: string; textOutputs?: string[]; errorMessage?: string | null }>
}

type WorkspaceMetadata = {
  created_at_ms?: number
}

export function getDefaultOutputRoot(): string {
  // Viz 需要读取 RIP-Bench 的输出目录：默认约定是 ../evaluation/output。
  // 如果你把评测输出写到别的地方，用 RIP_OUTPUT_ROOT 指向那个目录。
  const env = process.env.RIP_OUTPUT_ROOT
  if (env && env.trim().length > 0) return path.resolve(env)

  const sys = path.resolve(process.cwd(), '../evaluation_sys/output')
  if (fsSync.existsSync(sys)) return sys

  return path.resolve(process.cwd(), '../evaluation/output')
}

export async function listRuns(outputRootAbs: string): Promise<RunSummary[]> {
  const runs = await findRunDirs(outputRootAbs)
  const summaries: RunSummary[] = []

  for (const r of runs) {
    const reportPathAbs = path.join(r.runAbs, 'batch_test_report.json')
    const stat = await fs.stat(r.runAbs)
    const startedAtFallback = new Date(stat.mtimeMs).toISOString()

    const metadataCreatedAt = await readCreatedAtFromWorkspace(outputRootAbs, r.runRel)
    const startedAtFromMeta = typeof metadataCreatedAt === 'number' ? new Date(metadataCreatedAt).toISOString() : undefined

    const report = await readJsonIfExists<BatchReport>(reportPathAbs)
    
    let hasOutput = false
    try {
      const st = await fs.stat(path.join(r.runAbs, 'output'))
      hasOutput = st.isDirectory()
    } catch {
      hasOutput = false
    }

    const status = inferRunStatus(report, hasOutput)

    const startedAt = report?.startedAt ?? startedAtFromMeta ?? startedAtFallback
    const finishedAt = report?.finishedAt
    const { agentName, modelName, backbone, modelLabel, runName } = parseAgentFolder(r.agentFolder)

    summaries.push({
      id: r.runRel,
      name: r.runFolder,
      status,
      startedAt,
      finishedAt,
      agent: agentName,
      model: modelName,
      run: runName,
      backbone,
      modelLabel,
    })
  }

  return summaries.sort((a, b) => b.startedAt.localeCompare(a.startedAt))
}

export async function getRunDetail(outputRootAbs: string, runId: string): Promise<RunDetail> {
  // runId 支持两种形式：
  // 1) "agentFolder::runFolder"（前端 RunList 用的唯一 ID）
  // 2) "agentFolder/runFolder"（兼容旧格式）
  const resolved = await resolveRun(outputRootAbs, runId)
  const runAbs = safeResolve(outputRootAbs, resolved.runRel)
  const reportAbs = path.join(runAbs, 'batch_test_report.json')
  const configAbs = path.join(runAbs, 'batch_test_config.json')
  const agentLogAbs = path.join(runAbs, 'agent.log')
  const outputAbs = path.join(runAbs, 'output.json')
  const outputDirAbs = path.join(runAbs, 'output')
  const workspacePath = await findWorkspaceDirRel(outputRootAbs, resolved.runRel)
  const metadataAbs = workspacePath ? path.join(safeResolve(outputRootAbs, workspacePath), 'metadata.json') : path.join(runAbs, 'metadata.json')

  const report = await readJsonIfExists<BatchReport>(reportAbs)
  
  let hasOutput = false
  try {
    const st = await fs.stat(outputDirAbs)
    hasOutput = st.isDirectory()
  } catch {
    hasOutput = false
  }

  const status = inferRunStatus(report, hasOutput)

  const startedAt = report?.startedAt ?? new Date((await fs.stat(runAbs)).mtimeMs).toISOString()
  const finishedAt = report?.finishedAt
  const { agentName, modelName, backbone, modelLabel, runName } = parseAgentFolder(resolved.agentFolder)

  return {
    id: runId,
    name: resolved.runFolder,
    status,
    startedAt,
    finishedAt,
    agent: agentName,
    model: modelName,
    run: runName,
    backbone,
    modelLabel,
    runPath: toPosixRelPath(outputRootAbs, runAbs),
    workspacePath,
    reportPath: (await exists(reportAbs)) ? toPosixRelPath(outputRootAbs, reportAbs) : undefined,
    configPath: (await exists(configAbs)) ? toPosixRelPath(outputRootAbs, configAbs) : undefined,
    agentLogPath: (await exists(agentLogAbs)) ? toPosixRelPath(outputRootAbs, agentLogAbs) : undefined,
    outputPath: (await exists(outputAbs)) ? toPosixRelPath(outputRootAbs, outputAbs) : undefined,
    outputDirPath: (await existsDir(outputDirAbs)) ? toPosixRelPath(outputRootAbs, outputDirAbs) : undefined,
    metadataPath: (await exists(metadataAbs)) ? toPosixRelPath(outputRootAbs, metadataAbs) : undefined,
  }
}

export async function getRunOutput(outputRootAbs: string, runId: string, format: 'text' | 'raw'): Promise<RunOutputResponse> {
  // 输出读取的兜底顺序：
  // - format=text：优先 batch_test_report.json 的 textOutputs；其次尝试 openclaw 的 output.json 结构；最后读 agent.log。
  // - format=raw：优先 batch_test_report.json 的 stdout；其次读 agent.log。
  const resolved = await resolveRun(outputRootAbs, runId)
  const runAbs = safeResolve(outputRootAbs, resolved.runRel)
  const reportAbs = path.join(runAbs, 'batch_test_report.json')
  const agentLogAbs = path.join(runAbs, 'agent.log')
  const outputJsonAbs = path.join(runAbs, 'output.json')
  const agentJsonAbs = path.join(runAbs, 'agent.json')

  const report = await readJsonIfExists<BatchReport>(reportAbs)
  if (format === 'text') {
    const text = report?.tasks?.[0]?.textOutputs?.join('\n\n')
    if (text && text.trim().length > 0) {
      return { runId, format, content: text }
    }

    const openclawText = await readOpenclawText(outputJsonAbs)
    if (openclawText && openclawText.trim().length > 0) {
      return { runId, format, content: openclawText }
    }

    const agentJsonText = await readAgentJsonText(agentJsonAbs)
    if (agentJsonText && agentJsonText.trim().length > 0) {
      return { runId, format, content: agentJsonText }
    }
  }

  if (format === 'raw') {
    const raw = report?.tasks?.[0]?.stdout
    if (raw && raw.trim().length > 0) {
      return { runId, format, content: raw }
    }

    const openclawRaw = await readFileIfExists(agentLogAbs)
    if (openclawRaw && openclawRaw.trim().length > 0) {
      return { runId, format, content: openclawRaw }
    }

    const agentJsonRaw = await readAgentJsonRaw(agentJsonAbs)
    if (agentJsonRaw && agentJsonRaw.trim().length > 0) {
      return { runId, format, content: agentJsonRaw }
    }
  }

  if (await exists(agentLogAbs)) {
    const content = await fs.readFile(agentLogAbs, 'utf-8')
    return { runId, format, content }
  }

  return { runId, format, content: '' }
}

function inferRunStatus(report: BatchReport | undefined, hasOutput: boolean): RunStatus {
  // 只有存在output文件夹的任务才算是完成 (success / failed)
  if (!hasOutput) return 'running'

  const failed = report?.summary?.failed ?? 0
  const passed = report?.summary?.passed ?? 0
  const total = report?.summary?.total
  if (typeof total === 'number' && total > 0) {
    if (failed > 0) return 'failed'
    if (passed === total) return 'success'
  }
  return 'success'
}

async function readJsonIfExists<T>(absPath: string): Promise<T | undefined> {
  try {
    const raw = await fs.readFile(absPath, 'utf-8')
    return JSON.parse(raw) as T
  } catch {
    return undefined
  }
}

async function exists(absPath: string): Promise<boolean> {
  try {
    await fs.access(absPath)
    return true
  } catch {
    return false
  }
}

async function existsDir(absPath: string): Promise<boolean> {
  try {
    const st = await fs.stat(absPath)
    return st.isDirectory()
  } catch {
    return false
  }
}

async function readAgentJsonText(agentJsonAbs: string): Promise<string | undefined> {
  const agentJson = await readJsonIfExists<any>(agentJsonAbs)
  const trace = agentJson?.trace
  const stdout = typeof trace?.stdout === 'string' ? trace.stdout : typeof trace?.raw?.stdout === 'string' ? trace.raw.stdout : undefined
  const stderr = typeof trace?.stderr === 'string' ? trace.stderr : typeof trace?.raw?.stderr === 'string' ? trace.raw.stderr : undefined

  const parts: string[] = []
  if (stdout && stdout.trim().length > 0) parts.push(`STDOUT\n${stdout}`)
  if (stderr && stderr.trim().length > 0) parts.push(`STDERR\n${stderr}`)
  if (parts.length > 0) return parts.join('\n\n')

  const summary = trace?.summary
  if (summary && typeof summary === 'object') {
    try {
      return JSON.stringify(summary, null, 2)
    } catch {
      return undefined
    }
  }
  return undefined
}

async function readAgentJsonRaw(agentJsonAbs: string): Promise<string | undefined> {
  const agentJson = await readJsonIfExists<any>(agentJsonAbs)
  if (!agentJson) return undefined
  try {
    return JSON.stringify(agentJson, null, 2)
  } catch {
    return undefined
  }
}

async function findWorkspaceDirRel(outputRootAbs: string, runRel: string): Promise<string | undefined> {
  const runAbs = safeResolve(outputRootAbs, runRel)
  const entries = await fs.readdir(runAbs, { withFileTypes: true })
  const workspace = entries.find((e) => e.isDirectory() && e.name.startsWith('_'))
  if (!workspace) return undefined
  return toPosixRelPath(outputRootAbs, path.join(runAbs, workspace.name))
}

async function listAgentDirs(outputRootAbs: string): Promise<string[]> {
  const entries = await fs.readdir(outputRootAbs, { withFileTypes: true })
  return entries.filter((e) => e.isDirectory() && !e.name.startsWith('.')).map((e) => e.name)
}

async function listRunDirs(agentAbs: string): Promise<string[]> {
  const entries = await fs.readdir(agentAbs, { withFileTypes: true })
  return entries.filter((e) => e.isDirectory() && !e.name.startsWith('.')).map((e) => e.name)
}

function parseAgentFolder(folder: string): { agentName: string; modelName?: string; backbone?: string; modelLabel?: string; runName?: string } {
  const idx4 = folder.indexOf('----')
  if (idx4 >= 0) {
    const agentName = folder.slice(0, idx4) || folder
    const rest = folder.slice(idx4 + 4)
    const modelName = rest.trim().length > 0 ? rest : undefined
    return { agentName, modelName, backbone: modelName, modelLabel: modelName }
  }

  const parts = folder.split('--')
  if (parts.length >= 2) {
    const agentName = parts[0] || folder
    const modelName = parts[1] && parts[1].trim().length > 0 ? parts[1] : undefined
    const runName = parts.length >= 3 ? parts.slice(2).join('--') : undefined
    return { agentName, modelName, backbone: modelName, modelLabel: modelName, runName }
  }

  return { agentName: folder }
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
      const agentFolder = slash.length >= 2 ? slash[slash.length - 2] : ''
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
        const agentFolder = parts[parts.length - 2]
        const runFolder = parts[parts.length - 1]
        out.push({ runAbs: childAbs, runRel, agentFolder, runFolder })
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

async function readOpenclawText(outputJsonAbs: string): Promise<string | undefined> {
  const json = await readJsonIfExists<{ files?: Array<{ outputPath?: string; sourcePath?: string }> }>(outputJsonAbs)
  const files = json?.files
  if (!files || files.length === 0) return undefined
  const lines = files
    .map((f) => {
      const p = f.outputPath ?? f.sourcePath
      if (!p) return undefined
      return `output/${path.basename(p)}`
    })
    .filter((p): p is string => typeof p === 'string' && p.length > 0)
  if (lines.length === 0) return undefined
  return lines.join('\n')
}

async function readCreatedAtFromWorkspace(outputRootAbs: string, runRel: string): Promise<number | undefined> {
  try {
    const runAbs = safeResolve(outputRootAbs, runRel)
    const entries = await fs.readdir(runAbs, { withFileTypes: true })
    const workspace = entries.find((e) => e.isDirectory() && e.name.startsWith('_'))
    if (!workspace) return undefined
    const metaAbs = path.join(runAbs, workspace.name, 'metadata.json')
    const meta = await readJsonIfExists<WorkspaceMetadata>(metaAbs)
    return meta?.created_at_ms
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
