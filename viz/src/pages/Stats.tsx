import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import TopBar from '@/components/TopBar'
import { apiGet } from '@/utils/api'
import type { EvalStatsResponse } from '../../shared/types'
import Histogram from '@/components/Histogram'
import GroupRubricsBarChart from '@/components/GroupRubricsBarChart'

function pct(v: number) {
  if (!Number.isFinite(v)) return '0%'
  const x = Math.max(0, Math.min(1, v))
  return `${(x * 100).toFixed(1)}%`
}

function formatTokenRange(b: { start: number; end: number }) {
  return `${b.start}~${b.end}`
}

function formatDurationRangeSec(b: { start: number; end: number }) {
  return `${Math.round(b.start / 1000)}~${Math.round(b.end / 1000)}s`
}

function formatTokenCount(v: number) {
  if (!Number.isFinite(v)) return '0'
  if (v >= 1000000) return `${(v / 1000000).toFixed(2)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}

function formatTotalDuration(ms: number) {
  if (!Number.isFinite(ms) || ms <= 0) return '0 分钟'
  const totalMins = Math.floor(ms / 60000)
  if (totalMins === 0) return '< 1 分钟'
  const hours = Math.floor(totalMins / 60)
  const mins = totalMins % 60
  if (hours > 0) {
    return mins > 0 ? `${hours} 小时 ${mins} 分钟` : `${hours} 小时`
  }
  return `${mins} 分钟`
}

function StatCard(props: { title: string; value: string; subtitle?: string }) {
  return (
    <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
      <div className="text-xs text-[var(--app-text-muted)]">{props.title}</div>
      <div className="mt-1 text-lg font-semibold text-[var(--app-text)]">{props.value}</div>
      {props.subtitle ? <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">{props.subtitle}</div> : null}
    </div>
  )
}

export default function Stats() {
  const navigate = useNavigate()
  const [group, setGroup] = useState<string>('')
  const [experiment, setExperiment] = useState<string>('')

  const q = useQuery({
    queryKey: ['evalStats', experiment, group],
    queryFn: () => {
      const params = new URLSearchParams()
      if (experiment) params.set('experiment', experiment)
      if (group) params.set('group', group)
      const qs = params.toString()
      return apiGet<EvalStatsResponse>(`/api/stats/eval${qs ? `?${qs}` : ''}`)
    },
  })

  const data = q.data
  const experiments = data?.experiments ?? []
  const groups = data?.groups ?? []
  const effectiveExperiment = data?.experiment ?? experiment
  const effectiveGroup = data?.group ?? group

  useEffect(() => {
    if (!data) return
    if (!experiment && data.experiment) setExperiment(data.experiment)
    if (!group && data.group) setGroup(data.group)
  }, [data, experiment, group])

  const subtitle = useMemo(() => {
    if (!data) return '聚合运行 token 与评估结果，用于观察覆盖与分布'
    return `experiment：${effectiveExperiment ?? '-'} / group：${effectiveGroup ?? '-'} / runs：${data.totals.totalRuns} / tokensKnown：${data.totals.totalTokensKnown} / durationsKnown：${data.totals.totalDurationsKnown} / modelCallsKnown：${data.totals.totalModelCallsKnown} / totalTokensUsed：${formatTokenCount(data.totals.totalTokensUsed)} / totalDuration：${formatTotalDuration(data.totals.totalDurationMs)}`
  }, [data, effectiveGroup, effectiveExperiment])

  return (
    <div className="min-h-screen bg-[var(--app-bg)] text-[var(--app-text)]">
      <TopBar
        title="实验评估统计"
        subtitle={subtitle}
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

      <div className="mx-auto max-w-[1200px] p-4">
        <div className="mb-3 rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-xs text-[var(--app-text-muted)]">实验目录</div>
            <select
              className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-xs text-[var(--app-text)]"
              value={effectiveExperiment || ''}
              onChange={(e) => {
                setExperiment(e.target.value)
                setGroup('')
              }}
            >
              {experiments.length === 0 ? <option value="">（加载中）</option> : null}
              {experiments.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>

            <div className="ml-2 text-xs text-[var(--app-text-muted)]">实验组</div>
            <select
              className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-2 py-1 text-xs text-[var(--app-text)]"
              value={group || effectiveGroup || ''}
              onChange={(e) => setGroup(e.target.value)}
            >
              {groups.length === 0 ? <option value="">（加载中）</option> : null}
              {groups.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          </div>
        </div>

        {q.isLoading ? <div className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3 text-xs text-[var(--app-text-muted)]">加载统计中…</div> : null}
        {q.isError ? <div className="rounded-lg border border-red-400/20 bg-red-400/10 p-3 text-xs text-red-100">{String(q.error)}</div> : null}

        {data ? (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
              <StatCard title="已评估比例" value={pct(data.evaluatedRatio.ratio)} subtitle={`${data.evaluatedRatio.judgedRuns} / ${data.evaluatedRatio.totalRuns}`} />
              <StatCard title="评估全对比例" value={pct(data.perfectRatio.ratio)} subtitle={`${data.perfectRatio.perfectRuns} / ${data.perfectRatio.judgedRuns}`} />
              <StatCard title="Rubrics 正确率" value={pct(data.rubricsAccuracy.ratio)} subtitle={`${data.rubricsAccuracy.passedRubrics} / ${data.rubricsAccuracy.totalRubrics}`} />
              <StatCard title="组合覆盖率" value={pct(data.fullPairRatio.ratio)} subtitle={`${data.fullPairRatio.observedPairs} / ${data.fullPairRatio.expectedPairs}`} />
              <StatCard title="总模型调用次数" value={String(data.totals.totalModelCalls)} subtitle={`已统计 ${data.totals.totalModelCallsKnown} 次运行`} />
              <StatCard title="总计 Token 消耗" value={formatTokenCount(data.totals.totalTokensUsed)} subtitle={`已统计 ${data.totals.totalTokensKnown} 次运行`} />
              <StatCard title="总计耗时" value={formatTotalDuration(data.totals.totalDurationMs)} subtitle={`已统计 ${data.totals.totalDurationsKnown} 次运行`} />
            </div>

            <Histogram
              title="Token 分布直方图"
              subtitle="统计对象：每条运行的 totalTokens"
              buckets={data.tokenHistogram.buckets}
              heightPx={180}
              formatRange={formatTokenRange}
              footerLeft={`bucketSize：${data.tokenHistogram.bucketSize}`}
            />

            <Histogram
              title="任务耗时分布直方图"
              subtitle="统计对象：每条运行的 durationMs"
              buckets={data.durationHistogram.buckets}
              heightPx={180}
              formatRange={formatDurationRangeSec}
              footerLeft={`bucketSize：${Math.round(data.durationHistogram.bucketSizeMs / 1000)}s`}
            />

            <Histogram
              title="模型调用次数分布直方图"
              subtitle="统计对象：每条运行调用模型的次数"
              buckets={data.modelCallsHistogram.buckets}
              heightPx={180}
              formatRange={formatTokenRange}
              footerLeft={`bucketSize：${data.modelCallsHistogram.bucketSize}`}
            />

            <GroupRubricsBarChart items={data.groupRubricsAccuracy} />

            <details className="rounded-lg border border-[var(--app-border)] bg-[var(--app-panel)] p-3">
              <summary className="cursor-pointer list-none text-xs font-semibold text-[var(--app-text)]">数据口径</summary>
              <div className="mt-2 text-[11px] text-[var(--app-text-muted)]">
                Token：优先读取 group 目录下的 agent_runner_report.json；否则从 run 的 batch_test_report.json / agent.json 获取 totalTokens。
              </div>
              <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">已评估：run 目录下存在 rubrics_judge.json。</div>
              <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">评估全对：rubrics_judge.json 的 summary.passed === summary.total。</div>
              <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">Rubrics 正确率：sum(summary.passed) / sum(summary.total)。</div>
              <div className="mt-1 text-[11px] text-[var(--app-text-muted)]">组合覆盖率：observed (agent, model) / (agents × models)。</div>
            </details>
          </div>
        ) : null}
      </div>
    </div>
  )
}
