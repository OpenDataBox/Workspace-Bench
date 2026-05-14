import { useMemo, useState } from 'react'
import type { TokenHistogramBucket } from '../../shared/types'

export default function Histogram(props: {
  title: string
  subtitle?: string
  buckets: TokenHistogramBucket[]
  heightPx?: number
  formatRange?: (b: TokenHistogramBucket) => string
  footerLeft?: string
}) {
  const [hover, setHover] = useState<TokenHistogramBucket | undefined>(undefined)
  const maxCount = useMemo(() => Math.max(0, ...props.buckets.map((b) => b.count)), [props.buckets])
  const heightPx = props.heightPx ?? 180
  const formatRange = props.formatRange ?? ((b: TokenHistogramBucket) => `${b.start}~${b.end}`)

  if (props.buckets.length === 0) {
    return <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">暂无数据</div>
  }

  return (
    <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold text-[var(--app-text)]">{props.title}</div>
        {props.subtitle ? <div className="text-[11px] text-[var(--app-text-muted)]">{props.subtitle}</div> : null}
      </div>

      <div className="mt-3 flex items-end gap-1 overflow-x-auto pb-2">
        {props.buckets.map((b) => {
          const h = maxCount > 0 ? Math.max(2, Math.round((b.count / maxCount) * heightPx)) : 2
          const active = hover?.start === b.start && hover?.end === b.end
          return (
            <button
              key={`${b.start}-${b.end}`}
              type="button"
              className="relative w-7 shrink-0 rounded-sm bg-sky-500/60 hover:bg-sky-500"
              style={{ height: `${h}px` }}
              onMouseEnter={() => setHover(b)}
              onMouseLeave={() => setHover(undefined)}
              title={`${formatRange(b)} : ${b.count}`}
            >
              {active ? (
                <div className="absolute -top-10 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-[var(--app-surface)] px-2 py-1 text-[11px] text-[var(--app-text)] shadow">
                  <div className="text-[11px]">{formatRange(b)}</div>
                  <div className="text-[11px]">count: {b.count}</div>
                </div>
              ) : null}
            </button>
          )
        })}
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-[var(--app-text-muted)]">
        <div>{props.footerLeft ?? `min：${formatRange(props.buckets[0])} / max：${formatRange(props.buckets[props.buckets.length - 1])}`}</div>
        <div>hover 柱子查看区间与计数</div>
      </div>
    </div>
  )
}

