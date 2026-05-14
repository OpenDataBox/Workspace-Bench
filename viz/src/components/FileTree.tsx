import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, FileText, Folder } from 'lucide-react'
import type { FsNode } from '../../shared/types'
import { apiGet } from '@/utils/api'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/store/useAppStore'

function useFsTree(path: string, enabled: boolean) {
  return useQuery({
    queryKey: ['fsTree', path],
    enabled,
    queryFn: () => apiGet<FsNode>(`/api/fs/tree?path=${encodeURIComponent(path)}`),
  })
}

function TreeNode(props: {
  node: FsNode
  depth: number
  expanded: Set<string>
  toggle: (p: string) => void
  fileFilter: string
}) {
  const { setSelectedFilePath, setPreviewTab } = useAppStore()
  const isDir = props.node.type === 'dir'
  const isExpanded = props.expanded.has(props.node.path)
  const padding = 8 + props.depth * 12

  const q = useFsTree(props.node.path, isDir && isExpanded)
  const filteredChildren = useMemo(() => {
    const children = isDir && isExpanded ? q.data?.children ?? [] : []
    if (!props.fileFilter.trim()) return children
    const t = props.fileFilter.trim().toLowerCase()
    return children.filter((c) => c.name.toLowerCase().includes(t))
  }, [isDir, isExpanded, q.data?.children, props.fileFilter])

  return (
    <div>
      <button
        type="button"
        className={cn(
          'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs text-[var(--app-text)] hover:bg-[var(--app-panel-strong)]',
        )}
        style={{ paddingLeft: padding }}
        onClick={() => {
          if (isDir) {
            props.toggle(props.node.path)
          } else {
            setSelectedFilePath(props.node.path)
            setPreviewTab('file')
          }
        }}
      >
        {isDir ? (
          <ChevronRight className={cn('h-4 w-4 text-[var(--app-text-weak)] transition-transform', isExpanded && 'rotate-90')} />
        ) : (
          <span className="inline-block h-4 w-4" />
        )}
        {isDir ? <Folder className="h-4 w-4 text-[var(--app-text-muted)]" /> : <FileText className="h-4 w-4 text-[var(--app-text-muted)]" />}
        <span className="truncate">{props.node.name || props.node.path}</span>
      </button>

      {isDir && isExpanded ? (
        <div>
          {q.isLoading ? (
            <div className="px-3 py-2 text-[11px] text-[var(--app-text-weak)]" style={{ paddingLeft: padding + 20 }}>
              加载中…
            </div>
          ) : null}
          {q.isError ? (
            <div className="px-3 py-2 text-[11px] text-red-200" style={{ paddingLeft: padding + 20 }}>
              {String(q.error)}
            </div>
          ) : null}
          {filteredChildren.map((c) => (
            <TreeNode key={c.path} node={c} depth={props.depth + 1} expanded={props.expanded} toggle={props.toggle} fileFilter={props.fileFilter} />
          ))}
        </div>
      ) : null}
    </div>
  )
}

export default function FileTree(props: { rootPath: string }) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set([props.rootPath]))
  const [filter, setFilter] = useState('')
  const rootQ = useFsTree(props.rootPath, true)

  const toggle = (p: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  }

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)]">
      <div className="border-b border-[var(--app-border)] p-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="按文件名过滤"
          className="w-full rounded-md border border-[var(--app-border)] bg-[var(--app-surface)] px-2 py-1 text-xs text-[var(--app-text)] placeholder:text-[var(--app-text-weak)] focus:outline-none"
        />
      </div>
      <div className="flex-1 overflow-auto py-2">
        {rootQ.isLoading ? <div className="px-3 py-2 text-xs text-[var(--app-text-weak)]">加载中…</div> : null}
        {rootQ.isError ? <div className="px-3 py-2 text-xs text-red-200">{String(rootQ.error)}</div> : null}
        {rootQ.data ? (
          <TreeNode node={rootQ.data} depth={0} expanded={expanded} toggle={toggle} fileFilter={filter} />
        ) : null}
      </div>
    </div>
  )
}
