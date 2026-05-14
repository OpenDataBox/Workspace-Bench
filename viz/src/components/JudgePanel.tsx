import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { RunJudgeResponse } from '../../shared/types'
import { apiGet } from '@/utils/api'
import Badge from '@/components/Badge'

function pct(v: number) {
  if (!Number.isFinite(v)) return '0%'
  return `${Math.max(0, Math.min(100, Math.round(v * 100)))}%`
}

export default function JudgePanel({ runId }: { runId?: string }) {
  const [showAllEdges, setShowAllEdges] = useState(false)
  const [judgeModel, setJudgeModel] = useState<string>('')
  const q = useQuery({
    queryKey: ['runJudge', runId, judgeModel],
    enabled: !!runId,
    queryFn: () => {
      const params = new URLSearchParams()
      if (judgeModel) params.set('judgeModel', judgeModel)
      const qs = params.toString()
      return apiGet<RunJudgeResponse>(`/api/runs/${encodeURIComponent(runId!)}/judge${qs ? `?${qs}` : ''}`)
    },
  })

  const rubrics = q.data?.rubricsJudge
  const graph = q.data?.dependencyGraph
  const judgeModels = q.data?.judgeModels ?? []
  const effectiveJudgeModel = q.data?.selectedJudgeModel ?? judgeModel

  const rubricSummary = rubrics?.summary
  const passRate = useMemo(() => {
    if (!rubricSummary || rubricSummary.total <= 0) return 0
    return rubricSummary.passed / rubricSummary.total
  }, [rubricSummary])

  if (!runId) {
    return <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">请选择运行记录</div>
  }

  if (q.isLoading) {
    return <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">加载评估结果中…</div>
  }

  if (q.isError) {
    return <div className="rounded-lg border border-red-400/20 bg-red-400/10 p-3 text-xs text-red-100">{String(q.error)}</div>
  }

  if (!rubrics && !graph) {
    return <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">未发现评估结果文件</div>
  }

  const edges = graph?.edges ?? []
  const nodes = graph?.nodes ?? []
  const shownEdges = showAllEdges ? edges : edges.slice(0, 200)

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto">
      {rubrics ? (
        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs font-semibold text-[var(--app-text)]">LLM-as-a-Judge</div>
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-[var(--app-text-muted)]">
              {judgeModels.length > 0 ? (
                <select
                  className="h-7 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 text-[11px] text-[var(--app-text)]"
                  value={effectiveJudgeModel ?? ''}
                  onChange={(e) => setJudgeModel(e.target.value)}
                >
                  {judgeModels.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              ) : null}
              <div>agent：{rubrics.agentKind ?? '-'}</div>
              <div>createdAt：{rubrics.createdAt ?? '-'}</div>
            </div>
          </div>

          {rubricSummary ? (
            <div className="mt-2">
              <div className="flex flex-wrap items-center gap-3 text-[11px] text-[var(--app-text-muted)]">
                <div>通过：{rubricSummary.passed}</div>
                <div>失败：{rubricSummary.failed}</div>
                <div>总计：{rubricSummary.total}</div>
                <div>通过率：{pct(passRate)}</div>
                <div>judge：{rubrics.judge?.model ?? '-'}</div>
                <div>耗时：{typeof rubrics.judge?.durationMs === 'number' ? `${(rubrics.judge.durationMs / 1000).toFixed(2)} s` : '-'}</div>
              </div>
              <div className="mt-2 h-2 w-full overflow-hidden rounded bg-[var(--app-panel-strong)]">
                <div className="h-2 bg-emerald-500" style={{ width: pct(passRate) }} />
              </div>
            </div>
          ) : null}

          <div className="mt-3 flex flex-col gap-2">
            {(rubrics.rubrics ?? []).map((r) => (
              <details key={r.index} className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] p-2">
                <summary className="cursor-pointer list-none">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <div className="text-[11px] font-semibold text-[var(--app-text)]">#{r.index}</div>
                      <Badge tone={r.passed ? 'success' : 'danger'}>{r.passed ? 'passed' : 'failed'}</Badge>
                      <div className="truncate text-[11px] text-[var(--app-text)]">{r.rubric}</div>
                    </div>
                    <div className="text-[11px] text-[var(--app-text-muted)]">confidence：{typeof r.confidence === 'number' ? r.confidence : '-'}</div>
                  </div>
                </summary>
                {r.evidence ? (
                  <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--app-text-muted)]">{r.evidence}</pre>
                ) : (
                  <div className="mt-2 text-[11px] text-[var(--app-text-muted)]">（无 evidence）</div>
                )}
              </details>
            ))}
          </div>
        </div>
      ) : null}

      {graph ? (
        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs font-semibold text-[var(--app-text)]">Dependency Graph</div>
            <div className="flex items-center gap-2 text-[11px] text-[var(--app-text-muted)]">
              <div>nodes：{nodes.length}</div>
              <div>edges：{edges.length}</div>
              <button
                type="button"
                className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-[11px] text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
                onClick={() => setShowAllEdges((v) => !v)}
              >
                {showAllEdges ? '收起边' : '展开边'}
              </button>
            </div>
          </div>

          <div className="mt-2 grid gap-3 md:grid-cols-2">
            <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] p-2">
              <div className="mb-1 text-[11px] font-medium text-[var(--app-text)]">Edges</div>
              <div className="max-h-64 overflow-auto">
                <table className="w-full table-fixed">
                  <thead className="sticky top-0 bg-[var(--app-surface)]">
                    <tr className="text-left text-[11px] text-[var(--app-text-muted)]">
                      <th className="w-1/2 px-2 py-1">from</th>
                      <th className="w-1/2 px-2 py-1">to</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shownEdges.map((e, i) => (
                      <tr key={`${e[0]}->${e[1]}-${i}`} className="border-t border-[var(--app-border)] text-[11px] text-[var(--app-text)]">
                        <td className="truncate px-2 py-1">{e[0]}</td>
                        <td className="truncate px-2 py-1">{e[1]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!showAllEdges && edges.length > 200 ? (
                <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">仅展示前 200 条边</div>
              ) : null}
            </div>

            <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] p-2">
              <div className="mb-1 text-[11px] font-medium text-[var(--app-text)]">Nodes</div>
              <div className="max-h-64 overflow-auto">
                <ul className="space-y-1">
                  {nodes.slice(0, 500).map((n) => (
                    <li key={n} className="truncate text-[11px] text-[var(--app-text)]">
                      {n}
                    </li>
                  ))}
                </ul>
              </div>
              {nodes.length > 500 ? <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">仅展示前 500 个节点</div> : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
