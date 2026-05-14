import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Copy, ExternalLink } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import type { RunOutputResponse } from '../../shared/types'
import { apiGet } from '@/utils/api'
import { useAppStore } from '@/store/useAppStore'

export default function OutputPreview() {
  const navigate = useNavigate()
  const { selectedRunId, autoScrollOutput, setAutoScrollOutput } = useAppStore()
  const q = useQuery({
    queryKey: ['runOutput', selectedRunId, 'text'],
    enabled: !!selectedRunId,
    queryFn: () => apiGet<RunOutputResponse>(`/api/runs/${encodeURIComponent(selectedRunId!)}/output?format=text`),
  })

  const text = q.data?.content ?? ''
  const lines = useMemo(() => text.split('\n').slice(-2000).join('\n'), [text])

  const boxRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!autoScrollOutput) return
    const el = boxRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [lines, autoScrollOutput])

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
      <div className="flex items-center justify-between gap-2 border-b border-[var(--app-border)] px-3 py-2">
        <div className="text-xs font-medium text-[var(--app-text)]">输出预览</div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-[11px] text-[var(--app-text-muted)]">
            <input type="checkbox" checked={autoScrollOutput} onChange={(e) => setAutoScrollOutput(e.target.checked)} />
            自动滚动
          </label>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-[11px] text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)] disabled:opacity-50"
            disabled={!text}
            onClick={async () => {
              await navigator.clipboard.writeText(text)
            }}
          >
            <Copy className="h-3.5 w-3.5" />
            复制
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-[11px] text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)] disabled:opacity-50"
            disabled={!selectedRunId}
            onClick={() => navigate(`/runs/${encodeURIComponent(selectedRunId!)}`)}
          >
            <ExternalLink className="h-3.5 w-3.5" />
            详情
          </button>
        </div>
      </div>

      <div ref={boxRef} className="flex-1 overflow-auto p-3">
        {q.isLoading ? <div className="animate-pulse text-xs text-[var(--app-text-weak)]">加载中…</div> : null}
        {q.isError ? <div className="text-xs text-red-200">{String(q.error)}</div> : null}
        {!selectedRunId ? <div className="text-xs text-[var(--app-text-weak)]">请选择一个运行记录</div> : null}
        {selectedRunId && !q.isLoading && !q.isError ? (
          <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-[var(--app-text)]">{lines || '（空输出）'}</pre>
        ) : null}
      </div>
    </div>
  )
}
