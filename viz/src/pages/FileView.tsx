import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import Editor from '@monaco-editor/react'
import { ArrowLeft, Copy } from 'lucide-react'
import TopBar from '@/components/TopBar'
import EmptyState from '@/components/EmptyState'
import type { FileReadResponse } from '../../shared/types'
import { apiGet } from '@/utils/api'
import Badge from '@/components/Badge'
import { useTheme } from '@/hooks/useTheme'

export default function FileView() {
  const navigate = useNavigate()
  const [sp] = useSearchParams()
  const p = sp.get('path') ?? ''
  const { isDark } = useTheme()

  const q = useQuery({
    queryKey: ['fileView', p],
    enabled: !!p,
    queryFn: () => apiGet<FileReadResponse>(`/api/fs/file?path=${encodeURIComponent(p)}&maxBytes=5000000`),
  })

  const language = useMemo(() => {
    const low = p.toLowerCase()
    if (low.endsWith('.md')) return 'markdown'
    if (low.endsWith('.json')) return 'json'
    if (low.endsWith('.ts') || low.endsWith('.tsx')) return 'typescript'
    if (low.endsWith('.js') || low.endsWith('.jsx')) return 'javascript'
    if (low.endsWith('.py')) return 'python'
    if (low.endsWith('.yml') || low.endsWith('.yaml')) return 'yaml'
    if (low.endsWith('.log') || low.endsWith('.txt')) return 'plaintext'
    return 'plaintext'
  }, [p])

  return (
    <div className="min-h-screen bg-[var(--app-bg)] text-[var(--app-text)]">
      <TopBar
        title="文件查看"
        subtitle={p ? `path：${p}` : undefined}
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

      <div className="mx-auto max-w-[1400px] p-4">
        {!p ? <EmptyState title="缺少 path" description="请从工作台的文件预览入口进入" /> : null}

        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="min-w-0 text-xs text-[var(--app-text-muted)]">
            <span className="truncate">{p}</span>
          </div>
          <div className="flex items-center gap-2">
            {q.data?.truncated ? <Badge tone="warning">已截断</Badge> : null}
            <button
              type="button"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)] disabled:opacity-50"
              disabled={!q.data?.content}
              onClick={async () => {
                await navigator.clipboard.writeText(q.data?.content ?? '')
              }}
            >
              <Copy className="h-4 w-4" />
              复制全文
            </button>
          </div>
        </div>

        <div className="h-[78vh] overflow-hidden rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
          {!p ? null : q.isLoading ? (
            <div className="p-3 text-xs text-[var(--app-text-weak)]">加载中…</div>
          ) : q.isError ? (
            <div className="p-3 text-xs text-red-200">{String(q.error)}</div>
          ) : (
            <Editor
              height="100%"
              theme={isDark ? 'vs-dark' : 'light'}
              defaultLanguage={language}
              value={q.data?.content ?? ''}
              options={{
                readOnly: true,
                minimap: { enabled: false },
                fontSize: 12,
                wordWrap: 'on',
                lineNumbers: 'on',
                scrollBeyondLastLine: false,
              }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
