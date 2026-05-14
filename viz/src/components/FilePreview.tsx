import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import { ExternalLink } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import type { FileReadResponse } from '../../shared/types'
import { apiGet } from '@/utils/api'
import { useAppStore } from '@/store/useAppStore'
import Badge from '@/components/Badge'
import { useTheme } from '@/hooks/useTheme'

export default function FilePreview() {
  const navigate = useNavigate()
  const { selectedFilePath } = useAppStore()
  const { isDark } = useTheme()
  const q = useQuery({
    queryKey: ['file', selectedFilePath],
    enabled: !!selectedFilePath,
    queryFn: () => apiGet<FileReadResponse>(`/api/fs/file?path=${encodeURIComponent(selectedFilePath!)}&maxBytes=262144`),
  })

  const content = q.data?.content ?? ''
  const language = useMemo(() => {
    if (!selectedFilePath) return undefined
    const p = selectedFilePath.toLowerCase()
    if (p.endsWith('.md')) return 'markdown'
    if (p.endsWith('.json')) return 'json'
    if (p.endsWith('.ts') || p.endsWith('.tsx')) return 'typescript'
    if (p.endsWith('.js') || p.endsWith('.jsx')) return 'javascript'
    if (p.endsWith('.py')) return 'python'
    if (p.endsWith('.yml') || p.endsWith('.yaml')) return 'yaml'
    if (p.endsWith('.log') || p.endsWith('.txt')) return 'plaintext'
    return 'plaintext'
  }, [selectedFilePath])

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
      <div className="flex items-center justify-between gap-2 border-b border-[var(--app-border)] px-3 py-2">
        <div className="min-w-0">
          <div className="truncate text-xs font-medium text-[var(--app-text)]">文件预览</div>
          {selectedFilePath ? <div className="truncate text-[11px] text-[var(--app-text-muted)]">{selectedFilePath}</div> : null}
        </div>
        <div className="flex items-center gap-2">
          {q.data?.truncated ? <Badge tone="warning">已截断</Badge> : null}
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-[11px] text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)] disabled:opacity-50"
            disabled={!selectedFilePath}
            onClick={() => navigate(`/files?path=${encodeURIComponent(selectedFilePath!)}`)}
          >
            <ExternalLink className="h-3.5 w-3.5" />
            打开
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-hidden">
        {!selectedFilePath ? (
          <div className="p-3 text-xs text-[var(--app-text-weak)]">请选择一个文件</div>
        ) : q.isLoading ? (
          <div className="p-3 text-xs text-[var(--app-text-weak)]">加载中…</div>
        ) : q.isError ? (
          <div className="p-3 text-xs text-red-200">{String(q.error)}</div>
        ) : (
          <Editor
            height="100%"
            theme={isDark ? 'vs-dark' : 'light'}
            defaultLanguage={language}
            value={content}
            options={{
              readOnly: true,
              minimap: { enabled: false },
              fontSize: 12,
              wordWrap: 'on',
              scrollBeyondLastLine: false,
            }}
          />
        )}
      </div>
    </div>
  )
}
