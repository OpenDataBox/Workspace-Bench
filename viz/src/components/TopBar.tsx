import { useQueryClient } from '@tanstack/react-query'
import { Moon, RefreshCw, Sun } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useTheme } from '@/hooks/useTheme'

export default function TopBar(props: {
  title?: string
  subtitle?: string
  rightSlot?: React.ReactNode
  className?: string
}) {
  const qc = useQueryClient()
  const { isDark, toggleTheme } = useTheme()

  return (
    <div
      className={cn(
        'sticky top-0 z-20 flex items-center justify-between border-b border-[var(--app-border)] bg-[var(--app-bg)] px-4 py-3 backdrop-blur',
        props.className,
      )}
    >
      <div className="min-w-0">
        <div className="text-sm font-semibold text-[var(--app-text)]">{props.title ?? '模型输出可视化工具'}</div>
        {props.subtitle ? <div className="truncate text-xs text-[var(--app-text-muted)]">{props.subtitle}</div> : null}
      </div>

      <div className="flex items-center gap-2">
        {props.rightSlot}
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
          onClick={toggleTheme}
        >
          {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          {isDark ? '浅色' : '深色'}
        </button>
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
          onClick={() => qc.invalidateQueries()}
        >
          <RefreshCw className="h-4 w-4" />
          刷新
        </button>
      </div>
    </div>
  )
}
