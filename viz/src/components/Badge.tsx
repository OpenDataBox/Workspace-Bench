import { cn } from '@/lib/utils'

export default function Badge(props: {
  children: React.ReactNode
  tone?: 'neutral' | 'success' | 'warning' | 'danger'
  className?: string
}) {
  const tone = props.tone ?? 'neutral'
  const toneCls =
    tone === 'success'
      ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200'
      : tone === 'warning'
        ? 'border-amber-400/20 bg-amber-400/10 text-amber-100'
        : tone === 'danger'
          ? 'border-red-400/20 bg-red-400/10 text-red-200'
          : 'border-[var(--app-border)] bg-[var(--app-panel)] text-[var(--app-text-muted)]'

  return (
    <span className={cn('inline-flex items-center rounded-md border px-2 py-0.5 text-[11px]', toneCls, props.className)}>
      {props.children}
    </span>
  )
}
