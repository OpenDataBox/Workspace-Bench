import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/utils/api'
import type { RunStepsResponse, RunStep } from '../../shared/types'
import Badge from '@/components/Badge'
import { cn } from '@/lib/utils'

function formatTime(ms?: number) {
  if (!ms || Number.isNaN(ms)) return '-'
  return new Date(ms).toLocaleString()
}

function formatDuration(ms?: number) {
  if (typeof ms !== 'number') return '-'
  const s = ms / 1000
  return `${s.toFixed(2)} s`
}

function stringifyValue(v: unknown) {
  if (v === undefined) return undefined
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function stepTitle(step: RunStep) {
  if (step.type === 'tool') return `工具：${step.tool?.name ?? 'unknown'}`
  if (step.type === 'text') return '文本输出'
  if (step.type === 'system') return '系统初始化'
  if (step.type === 'result') return '运行结果'
  return '未知事件'
}

export default function ToolStepsPanel({ runId }: { runId?: string }) {
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({})
  const q = useQuery({
    queryKey: ['runSteps', runId],
    enabled: !!runId,
    queryFn: () => apiGet<RunStepsResponse>(`/api/runs/${encodeURIComponent(runId!)}/steps`),
  })

  const steps = q.data?.steps ?? []

  if (!runId) {
    return (
      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">
        请选择运行记录
      </div>
    )
  }

  if (q.isLoading) {
    return (
      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">
        加载步骤中…
      </div>
    )
  }

  if (q.isError) {
    return <div className="rounded-lg border border-red-400/20 bg-red-400/10 p-3 text-xs text-red-100">{String(q.error)}</div>
  }

  if (steps.length === 0) {
    return (
      <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">
        暂无步骤数据
      </div>
    )
  }

  const toggleAll = (value: boolean) => {
    const next: Record<number, boolean> = {}
    for (const step of steps) next[step.index] = value
    setCollapsed(next)
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto">
      {q.data?.summary ? (
        <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">
          <div className="flex flex-wrap items-center gap-3">
            <div>工具调用：{q.data.summary.toolCalls}</div>
            <div>文本消息：{q.data.summary.textMessages}</div>
            <div>回合数：{q.data.summary.turns}</div>
            <div>总 Token：{q.data.summary.totalTokens ?? '-'}</div>
            <div>总耗时：{formatDuration(q.data.summary.totalDurationMs)}</div>
            <div>开始：{q.data.summary.startedAt ?? '-'}</div>
            <div>结束：{q.data.summary.finishedAt ?? '-'}</div>
          </div>
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-[var(--app-text-muted)]">步骤明细</div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-[11px] text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
            onClick={() => toggleAll(false)}
          >
            展开全部
          </button>
          <button
            type="button"
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-[11px] text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
            onClick={() => toggleAll(true)}
          >
            折叠全部
          </button>
        </div>
      </div>

      {steps.map((step) => {
        const inputText = stringifyValue(step.tool?.input)
        const outputText = stringifyValue(step.tool?.output)
        const isCollapsed = collapsed[step.index] ?? false
        return (
          <div key={`${step.index}-${step.type}`} className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-2 border-b border-[var(--app-border)] px-3 py-2 text-left"
              onClick={() => setCollapsed((prev) => ({ ...prev, [step.index]: !isCollapsed }))}
            >
              <div className="flex items-center gap-2">
                <div className="text-xs font-semibold text-[var(--app-text)]">步骤 {step.index}</div>
                <Badge tone={step.type === 'tool' ? 'success' : 'neutral'}>{step.type}</Badge>
                <div className="text-[11px] text-[var(--app-text-muted)]">{stepTitle(step)}</div>
              </div>
              <div className="text-[11px] text-[var(--app-text-muted)]">偏移 {formatDuration(step.offsetMs)}</div>
            </button>

            {!isCollapsed ? (
              <div className="flex flex-col gap-3">
                <div className="grid gap-3 px-3 py-2 text-[11px] text-[var(--app-text-muted)] md:grid-cols-3">
                  <div>
                    <div className="text-[var(--app-text-weak)]">开始时间</div>
                    <div className="text-[var(--app-text)]">{formatTime(step.startTimeMs)}</div>
                  </div>
                  <div>
                    <div className="text-[var(--app-text-weak)]">结束时间</div>
                    <div className="text-[var(--app-text)]">{formatTime(step.endTimeMs)}</div>
                  </div>
                  <div>
                    <div className="text-[var(--app-text-weak)]">耗时</div>
                    <div className="text-[var(--app-text)]">{formatDuration(step.durationMs)}</div>
                  </div>
                </div>

                {step.type === 'tool' ? (
                  <div className="grid gap-3 px-3 pb-3 text-[11px] text-[var(--app-text-muted)] md:grid-cols-3">
                    <div>
                      <div className="text-[var(--app-text-weak)]">工具名</div>
                      <div className="text-[var(--app-text)]">{step.tool?.name ?? '-'}</div>
                    </div>
                    <div>
                      <div className="text-[var(--app-text-weak)]">状态</div>
                      <div className={cn('text-[var(--app-text)]', step.tool?.status === 'completed' && 'text-emerald-300')}>
                        {step.tool?.status ?? '-'}
                      </div>
                    </div>
                    <div>
                      <div className="text-[var(--app-text-weak)]">Call ID</div>
                      <div className="truncate text-[var(--app-text)]">{step.tool?.callId ?? '-'}</div>
                    </div>
                  </div>
                ) : null}

                <div className="grid gap-3 px-3 pb-3 text-[11px] text-[var(--app-text-muted)] md:grid-cols-4">
                  <div>
                    <div className="text-[var(--app-text-weak)]">回合</div>
                    <div className="text-[var(--app-text)]">{step.turn ?? '-'}</div>
                  </div>
                  <div>
                    <div className="text-[var(--app-text-weak)]">Prompt Token</div>
                    <div className="text-[var(--app-text)]">{step.usage?.promptTokens ?? '-'}</div>
                  </div>
                  <div>
                    <div className="text-[var(--app-text-weak)]">Completion Token</div>
                    <div className="text-[var(--app-text)]">{step.usage?.completionTokens ?? '-'}</div>
                  </div>
                  <div>
                    <div className="text-[var(--app-text-weak)]">Total Token</div>
                    <div className="text-[var(--app-text)]">{step.usage?.totalTokens ?? '-'}</div>
                  </div>
                </div>

                {step.type === 'tool' ? (
                  <div className="grid gap-3 px-3 pb-3 text-[11px] text-[var(--app-text-muted)] md:grid-cols-2">
                    <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-code-bg)] p-2">
                      <div className="mb-1 text-[var(--app-text-weak)]">输入</div>
                      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--app-text)]">
                        {inputText ?? '（空）'}
                      </pre>
                    </div>
                    <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-code-bg)] p-2">
                      <div className="mb-1 text-[var(--app-text-weak)]">输出</div>
                      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--app-text)]">
                        {outputText ?? '（空）'}
                      </pre>
                    </div>
                  </div>
                ) : null}

                {step.type !== 'tool' ? (
                  <div className="px-3 pb-3">
                    <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-code-bg)] p-2">
                      <div className="mb-1 text-[var(--app-text-weak)] text-[11px]">内容</div>
                      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--app-text)]">
                        {step.text ?? '（空）'}
                      </pre>
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
