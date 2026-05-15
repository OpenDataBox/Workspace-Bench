import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Copy, Download } from 'lucide-react'
import TopBar from '@/components/TopBar'
import Badge from '@/components/Badge'
import EmptyState from '@/components/EmptyState'
import ToolStepsPanel from '@/components/ToolStepsPanel'
import JudgePanel from '@/components/JudgePanel'
import type { RunDetail, RunOutputResponse } from '../../shared/types'
import { apiGet } from '@/utils/api'

function tone(status: RunDetail['status']): 'success' | 'danger' | 'neutral' {
  if (status === 'failed') return 'danger'
  if (status === 'success') return 'success'
  return 'neutral'
}

export default function RunDetailPage() {
  const navigate = useNavigate()
  const params = useParams()
  const runId = params.runId

  const [view, setView] = useState<'text' | 'raw' | 'steps' | 'judge'>('text')
  const detailQ = useQuery({
    queryKey: ['runDetail', runId],
    enabled: !!runId,
    queryFn: () => apiGet<RunDetail>(`/api/runs/${encodeURIComponent(runId!)}`),
  })

  const outQ = useQuery({
    queryKey: ['runOutput', runId, view],
    enabled: !!runId && view !== 'steps' && view !== 'judge',
    queryFn: () => apiGet<RunOutputResponse>(`/api/runs/${encodeURIComponent(runId!)}/output?format=${view}`),
  })

  const content = outQ.data?.content ?? ''
  const filename = useMemo(() => `${runId ?? 'run'}_${view}.txt`, [runId, view])

  return (
    <div className="min-h-screen bg-[var(--app-bg)] text-[var(--app-text)]">
      <TopBar
        title="输出详情"
        subtitle={runId ? `runId：${runId}` : undefined}
        rightSlot={
          <button
            type="button"
            className="inline-flex items-center gap-2 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
            onClick={() => navigate('/')}
          >
            <ArrowLeft className="h-4 w-4" />
            返回
          </button>
        }
      />

      <div className="mx-auto max-w-[1200px] p-4">
        {!runId ? <EmptyState title="缺少 runId" description="请从工作台进入输出详情页" /> : null}

        {detailQ.data ? (
          <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-4">
            <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
              <div className="text-xs text-[var(--app-text-muted)]">状态</div>
              <div className="mt-1">
                <Badge tone={tone(detailQ.data.status)}>{detailQ.data.status}</Badge>
              </div>
            </div>
            <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
              <div className="text-xs text-[var(--app-text-muted)]">Agent / 模型</div>
              <div className="mt-1 text-xs text-[var(--app-text)]">{detailQ.data.agent ?? '-'}</div>
              <div className="mt-1 text-xs text-[var(--app-text-muted)]">
                {detailQ.data.model ?? detailQ.data.modelLabel ?? detailQ.data.backbone ?? '-'}
              </div>
            </div>
            <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
              <div className="text-xs text-[var(--app-text-muted)]">开始时间</div>
              <div className="mt-1 text-xs text-[var(--app-text)]">{detailQ.data.startedAt}</div>
            </div>
            <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
              <div className="text-xs text-[var(--app-text-muted)]">结束时间</div>
              <div className="mt-1 text-xs text-[var(--app-text)]">{detailQ.data.finishedAt ?? '-'}</div>
            </div>
          </div>
        ) : null}

        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={
                view === 'text'
                  ? 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel-strong)] px-3 py-1.5 text-xs text-[var(--app-text)]'
                  : 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]'
              }
              onClick={() => setView('text')}
            >
              纯文本
            </button>
            <button
              type="button"
              className={
                view === 'raw'
                  ? 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel-strong)] px-3 py-1.5 text-xs text-[var(--app-text)]'
                  : 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]'
              }
              onClick={() => setView('raw')}
            >
              原始 stdout
            </button>
            <button
              type="button"
              className={
                view === 'steps'
                  ? 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel-strong)] px-3 py-1.5 text-xs text-[var(--app-text)]'
                  : 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]'
              }
              onClick={() => setView('steps')}
            >
              步骤
            </button>
            <button
              type="button"
              className={
                view === 'judge'
                  ? 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel-strong)] px-3 py-1.5 text-xs text-[var(--app-text)]'
                  : 'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]'
              }
              onClick={() => setView('judge')}
            >
              评估
            </button>
          </div>

          {view !== 'steps' && view !== 'judge' ? (
            <div className="flex items-center gap-2">
            <button
              type="button"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)] disabled:opacity-50"
              disabled={!content}
              onClick={async () => {
                await navigator.clipboard.writeText(content)
              }}
            >
              <Copy className="h-4 w-4" />
              复制
            </button>
            <button
              type="button"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)] disabled:opacity-50"
              disabled={!content}
              onClick={() => {
                const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = filename
                a.click()
                URL.revokeObjectURL(url)
              }}
            >
              <Download className="h-4 w-4" />
              下载
            </button>
            </div>
          ) : null}
        </div>

        {view === 'steps' ? (
          <div className="h-[70vh]">
            <ToolStepsPanel runId={runId} />
          </div>
        ) : view === 'judge' ? (
          <div className="h-[70vh]">
            <JudgePanel runId={runId} />
          </div>
        ) : (
          <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
            <div className="h-[70vh] overflow-auto p-3">
              {outQ.isLoading ? <div className="text-xs text-[var(--app-text-weak)]">加载中…</div> : null}
              {outQ.isError ? <div className="text-xs text-red-200">{String(outQ.error)}</div> : null}
              {!outQ.isLoading && !outQ.isError ? (
                <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-[var(--app-text)]">{content || '（空输出）'}</pre>
              ) : null}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
