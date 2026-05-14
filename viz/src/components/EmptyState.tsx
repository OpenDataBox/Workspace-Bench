import { cn } from '@/lib/utils'

export default function EmptyState(props: { title: string; description?: string; className?: string }) {
  return (
    <div className={cn('rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-4', props.className)}>
      <div className="text-sm font-semibold text-[var(--app-text)]">{props.title}</div>
      {props.description ? <div className="mt-1 text-xs text-[var(--app-text-muted)]">{props.description}</div> : null}
    </div>
  )
}
