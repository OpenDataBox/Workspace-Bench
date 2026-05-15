import type { EvalStatsResponse } from '../../shared/types'

function pct(v: number) {
  if (!Number.isFinite(v)) return '0%'
  const x = Math.max(0, Math.min(1, v))
  return `${(x * 100).toFixed(1)}%`
}

export default function GroupRubricsBarChart(props: { items: EvalStatsResponse['groupRubricsAccuracy'] }) {
  if (!props.items || props.items.length === 0) {
    return <div className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">暂无组间对比数据</div>
  }

  const sorted = [...props.items].sort((a, b) => b.ratio - a.ratio)

  const split = (group: string) => {
    const parts = group.split('--')
    const agent = parts[0] ?? group
    const model = parts.length >= 2 ? parts[1] : '-'
    const run = parts.length >= 3 ? parts.slice(2).join('--') : '-'
    return { agent, model, run }
  }

  return (
    <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold text-[var(--app-text)]">组间 Rubrics 准确率对比</div>
        <div className="text-[11px] text-[var(--app-text-muted)]">统计对象：存在 rubrics_judge--*.json 的运行</div>
      </div>

      <div className="mt-3 grid gap-2">
        {sorted.map((g) => {
          const s = split(g.group)
          return (
            <div key={`${g.group}@@${g.judgeModel}`} className="grid grid-cols-[520px_1fr_300px] items-center gap-2">
              <div className="grid grid-cols-4 gap-2" title={`${g.group} / judgeModel=${g.judgeModel}`}>
                <div className="truncate text-[11px] text-[var(--app-text)]">{s.agent}</div>
                <div className="truncate text-[11px] text-[var(--app-text-muted)]">{s.model}</div>
                <div className="truncate text-[11px] text-[var(--app-text-muted)]">{s.run}</div>
                <div className="truncate text-[11px] text-[var(--app-text-muted)]">{g.judgeModel}</div>
              </div>
              <div className="h-3 overflow-hidden rounded bg-[var(--app-panel-strong)]">
                <div className="h-3 bg-emerald-500" style={{ width: pct(g.ratio) }} />
              </div>
              <div className="flex items-center justify-end gap-3 text-[11px] text-[var(--app-text-muted)]">
                <span title="准确率 (已通过项数/总项数)">{pct(g.ratio)} ({g.passedRubrics}/{g.totalRubrics})</span>
                <span title="已评估任务数量 / 已完成任务总数">({g.judgedRuns}/{g.totalRuns})</span>
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-2 text-[11px] text-[var(--app-text-muted)]">
        hover 可查看详细信息；左侧为 准确率 (已通过项数/总项数)，右侧为 (已评估任务数量/已完成任务总数)
      </div>
    </div>
  )
}
