import fs from 'fs/promises'
import path from 'path'
import { safeResolve } from '../utils/fsSafe.js'
import type { RunJudgeResponse, DependencyGraph, RubricsJudge } from '../../shared/types.js'

export async function getRunJudge(outputRootAbs: string, runId: string, judgeModel?: string): Promise<RunJudgeResponse> {
  const runAbs = await resolveRunAbs(outputRootAbs, runId)
  const depAbs = (await findFirstJsonByPrefix(runAbs, 'dependency_graph')) ?? path.join(runAbs, 'dependency_graph.json')
  const rubFiles = await listRubricsJudgeFiles(runAbs)
  const selectedRub = pickRubricsJudgeFile(rubFiles, judgeModel)
  const rubAbs = selectedRub?.abs ?? path.join(runAbs, 'rubrics_judge.json')

  const dependencyGraph = await readJsonIfExists<DependencyGraph>(depAbs)
  const rubricsJudge = await readJsonIfExists<RubricsJudge>(rubAbs)

  return {
    runId,
    dependencyGraph: dependencyGraph && Array.isArray(dependencyGraph.nodes) && Array.isArray(dependencyGraph.edges) ? dependencyGraph : undefined,
    rubricsJudge: rubricsJudge && Array.isArray(rubricsJudge.rubrics) ? rubricsJudge : undefined,
    judgeModels: rubFiles.map((f) => f.modelLabel),
    selectedJudgeModel: selectedRub?.modelLabel,
  }
}

type RubricsJudgeFile = {
  abs: string
  fileName: string
  modelLabel: string
}

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

function pickRubricsJudgeFile(files: RubricsJudgeFile[], judgeModel?: string): RubricsJudgeFile | undefined {
  if (!files || files.length === 0) return undefined
  if (judgeModel && judgeModel.trim().length > 0) {
    const hit = files.find((f) => f.modelLabel === judgeModel)
    if (hit) return hit
  }
  return files[0]
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

async function readJsonIfExists<T>(absPath: string): Promise<T | undefined> {
  try {
    const raw = await fs.readFile(absPath, 'utf-8')
    return JSON.parse(raw) as T
  } catch {
    return undefined
  }
}

async function resolveRunAbs(outputRootAbs: string, runId: string): Promise<string> {
  const slash = runId.split('/').filter(Boolean)
  if (slash.length >= 2) {
    const runRel = slash.join('/')
    const abs = safeResolve(outputRootAbs, runRel)
    const st = await fs.stat(abs)
    if (st.isDirectory()) return abs
  }

  const parts = runId.split('::').filter(Boolean)
  if (parts.length >= 2) {
    const runRel = path.posix.join(parts[0], parts.slice(1).join('::'))
    const abs = safeResolve(outputRootAbs, runRel)
    const st = await fs.stat(abs)
    if (st.isDirectory()) return abs
  }

  const legacy = runId
  const found = await findRunDirByLeaf(outputRootAbs, legacy)
  if (found) return found

  throw new Error(`Run not found: ${runId}`)
}

async function findRunDirByLeaf(outputRootAbs: string, runFolder: string): Promise<string | undefined> {
  return await walk(outputRootAbs, 0)

  async function walk(dirAbs: string, depth: number): Promise<string | undefined> {
    if (depth > 3) return undefined
    let entries: Array<import('fs').Dirent>
    try {
      entries = await fs.readdir(dirAbs, { withFileTypes: true })
    } catch {
      return undefined
    }
    for (const e of entries) {
      if (!e.isDirectory()) continue
      if (e.name.startsWith('.')) continue
      const childAbs = path.join(dirAbs, e.name)
      if (e.name === runFolder) {
        try {
          const st = await fs.stat(childAbs)
          if (st.isDirectory()) return childAbs
        } catch {
          // ignore
        }
      }
      const res = await walk(childAbs, depth + 1)
      if (res) return res
    }
    return undefined
  }
}
