import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { RunSummary } from '../../shared/types'
import { apiGet } from '@/utils/api'
import { cn } from '@/lib/utils'
import Badge from '@/components/Badge'
import { useAppStore } from '@/store/useAppStore'

function statusTone(status: RunSummary['status']): 'success' | 'danger' | 'neutral' {
  if (status === 'failed') return 'danger'
  if (status === 'success') return 'success'
  return 'neutral'
}

export default function RunList() {
  const navigate = useNavigate()
  const { selectedRunId, setSelectedRunId } = useAppStore()
  const q = useQuery({ queryKey: ['runs'], queryFn: () => apiGet<RunSummary[]>('/api/runs') })
  const [agentFilter, setAgentFilter] = useState('')
  const [modelFilter, setModelFilter] = useState('')
  const [runFilter, setRunFilter] = useState('')

  const runs = q.data ?? []

  const agentOptions = useMemo(() => {
    return Array.from(new Set(runs.map((r) => r.agent).filter((v): v is string => typeof v === 'string' && v.trim().length > 0))).sort(
      (a, b) => a.localeCompare(b),
    )
  }, [runs])

  const modelOptions = useMemo(() => {
    return Array.from(
      new Set(
        runs
          .map((r) => r.model ?? r.modelLabel ?? r.backbone)
          .filter((v): v is string => typeof v === 'string' && v.trim().length > 0),
      ),
    ).sort((a, b) => a.localeCompare(b))
  }, [runs])

  const runOptions = useMemo(() => {
    return Array.from(new Set(runs.map((r) => r.run).filter((v): v is string => typeof v === 'string' && v.trim().length > 0))).sort((a, b) =>
      a.localeCompare(b),
    )
  }, [runs])

  const filteredRuns = useMemo(() => {
    return runs.filter((r) => {
      const agentOk = agentFilter ? r.agent === agentFilter : true
      const m = r.model ?? r.modelLabel ?? r.backbone
      const modelOk = modelFilter ? m === modelFilter : true
      const runOk = runFilter ? r.run === runFilter : true
      return agentOk && modelOk && runOk
    })
  }, [runs, agentFilter, modelFilter, runFilter])

  if (q.isLoading) {
    return <div className="h-full animate-pulse rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]" />
  }
  if (q.isError) {
    return <div className="rounded-lg border border-red-400/20 bg-red-400/10 p-3 text-xs text-red-100">{String(q.error)}</div>
  }
  if (runs.length === 0) {
    return (
      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">
        未发现运行记录
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
      <div className="sticky top-0 z-10 border-b border-[var(--app-border)] bg-[var(--app-surface)] px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-[11px] text-[var(--app-text-muted)]">筛选</div>
          <select
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-xs text-[var(--app-text)]"
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
          >
            <option value="">全部 Agent</option>
            {agentOptions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>

          <select
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-xs text-[var(--app-text)]"
            value={modelFilter}
            onChange={(e) => setModelFilter(e.target.value)}
          >
            <option value="">全部 模型</option>
            {modelOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>

          <select
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-xs text-[var(--app-text)]"
            value={runFilter}
            onChange={(e) => setRunFilter(e.target.value)}
          >
            <option value="">全部 Run</option>
            {runOptions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
            onClick={() => {
              setAgentFilter('')
              setModelFilter('')
              setRunFilter('')
            }}
          >
            清空
          </button>

          <div className="ml-auto text-[11px] text-[var(--app-text-muted)]">
            命中 {filteredRuns.length} / {runs.length}
          </div>
        </div>
        {runs.length > 0 && filteredRuns.length === 0 ? (
          <div className="mt-2 text-[11px] text-[var(--app-text-muted)]">无匹配运行，请调整筛选</div>
        ) : null}
      </div>
      <table className="w-full table-fixed">
        <thead className="bg-[var(--app-surface)]">
          <tr className="text-left text-[11px] text-[var(--app-text-muted)]">
            <th className="w-40 px-3 py-2">运行</th>
            <th className="w-40 px-3 py-2">Agent</th>
            <th className="w-56 px-3 py-2">模型</th>
            <th className="w-40 px-3 py-2">Run</th>
            <th className="w-24 px-3 py-2">状态</th>
            <th className="px-3 py-2">开始时间</th>
            <th className="px-3 py-2">结束时间</th>
          </tr>
        </thead>
        <tbody>
          {filteredRuns.map((r) => {
            const active = r.id === selectedRunId
            const modelText = r.model ?? r.modelLabel ?? r.backbone ?? '-'
            const runText = r.run ?? '-'
            return (
              <tr
                key={r.id}
                className={cn(
                  'cursor-pointer border-t border-[var(--app-border)] text-xs text-[var(--app-text)] hover:bg-[var(--app-panel-strong)]',
                  active && 'bg-[var(--app-panel-strong)]',
                )}
                onClick={() => {
                  setSelectedRunId(r.id)
                }}
                onDoubleClick={() => navigate(`/runs/${encodeURIComponent(r.id)}`)}
              >
                <td className="truncate px-3 py-2 font-medium text-[var(--app-text)]">{r.name}</td>
                <td className="truncate px-3 py-2 text-[var(--app-text-muted)]">{r.agent ?? '-'}</td>
                <td className="truncate px-3 py-2 text-[var(--app-text-muted)]">{modelText}</td>
                <td className="truncate px-3 py-2 text-[var(--app-text-muted)]">{runText}</td>
                <td className="px-3 py-2">
                  <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                </td>
                <td className="truncate px-3 py-2 text-[var(--app-text-muted)]">{r.startedAt}</td>
                <td className="truncate px-3 py-2 text-[var(--app-text-muted)]">{r.finishedAt ?? '-'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
