import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/utils/api'
import { useMemo } from 'react'

export default function MetadataPreview({ metadataPath }: { metadataPath?: string }) {
  const metadataQ = useQuery({
    queryKey: ['fileContent', metadataPath],
    enabled: !!metadataPath,
    queryFn: () => apiGet<{ content: string }>(`/api/fs/file?path=${encodeURIComponent(metadataPath!)}`),
  })

  const parsed = useMemo(() => {
    if (!metadataQ.data?.content) return null
    try {
      return JSON.parse(metadataQ.data.content)
    } catch {
      return null
    }
  }, [metadataQ.data?.content])

  if (!metadataPath) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-[var(--app-text-muted)]">
        当前运行不存在 metadata.json
      </div>
    )
  }

  if (metadataQ.isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-[var(--app-text-muted)]">
        加载中...
      </div>
    )
  }

  if (metadataQ.isError) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-red-400">
        加载失败：{String(metadataQ.error)}
      </div>
    )
  }

  if (!parsed) {
    return (
      <div className="h-full overflow-auto bg-[var(--app-panel-strong)] p-4 text-[13px] text-[var(--app-text)] font-mono whitespace-pre-wrap">
        {metadataQ.data?.content}
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto bg-[var(--app-bg)] p-6 text-[14px] text-[var(--app-text)] leading-relaxed">
      <div className="mx-auto max-w-4xl space-y-8">
        {/* 顶部标题区 */}
        <div className="border-b border-[var(--app-border)] pb-4">
          <h2 className="text-xl font-semibold mb-2">任务详情: {parsed.id || 'Unknown'}</h2>
          <div className="flex flex-wrap gap-2 text-xs">
            {parsed.task_type && (
              <span className="bg-blue-500/10 text-blue-400 border border-blue-500/20 px-2 py-1 rounded">
                类型: {parsed.task_type}
              </span>
            )}
            {parsed.task_diff && (
              <span className="bg-orange-500/10 text-orange-400 border border-orange-500/20 px-2 py-1 rounded">
                难度: {parsed.task_diff}
              </span>
            )}
            {parsed.file_system && (
              <span className="bg-green-500/10 text-green-400 border border-green-500/20 px-2 py-1 rounded">
                场景: {parsed.file_system}
              </span>
            )}
          </div>
        </div>

        {/* 核心要求：任务提示词 */}
        <section>
          <h3 className="text-[15px] font-medium mb-3 flex items-center gap-2 text-[var(--app-text-strong)]">
            <span className="w-1.5 h-4 bg-indigo-500 rounded-sm"></span> 任务指令 (Task)
          </h3>
          <div className="bg-[var(--app-panel)] border border-[var(--app-border)] rounded-md p-4 whitespace-pre-wrap font-mono text-[13px]">
            {parsed.task || '无任务指令'}
          </div>
        </section>

        {/* 协同与评估指标 */}
        <section className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <h3 className="text-[15px] font-medium mb-3 flex items-center gap-2 text-[var(--app-text-strong)]">
              <span className="w-1.5 h-4 bg-teal-500 rounded-sm"></span> 协同类型
            </h3>
            {parsed.collaboration_type && parsed.collaboration_type.length > 0 ? (
              <ul className="list-disc list-inside pl-2 space-y-1 text-[13px] text-[var(--app-text-muted)]">
                {parsed.collaboration_type.map((ct: string, i: number) => (
                  <li key={i}>{ct}</li>
                ))}
              </ul>
            ) : (
              <span className="text-[13px] text-[var(--app-text-weak)]">无</span>
            )}
          </div>
          <div>
            <h3 className="text-[15px] font-medium mb-3 flex items-center gap-2 text-[var(--app-text-strong)]">
              <span className="w-1.5 h-4 bg-rose-500 rounded-sm"></span> 文件及输出
            </h3>
            <div className="text-[13px] text-[var(--app-text-muted)] space-y-2">
              <div>
                <span className="font-medium text-[var(--app-text)]">数据目录：</span>
                {parsed.data?.join(', ') || '-'}
              </div>
              <div>
                <span className="font-medium text-[var(--app-text)]">输出文件：</span>
                {parsed.output_files?.join(', ') || '-'}
              </div>
            </div>
          </div>
        </section>

        {/* 期望步骤 */}
        {parsed.steps && parsed.steps.length > 0 && (
          <section>
            <h3 className="text-[15px] font-medium mb-3 flex items-center gap-2 text-[var(--app-text-strong)]">
              <span className="w-1.5 h-4 bg-amber-500 rounded-sm"></span> 期望执行步骤 (Golden Steps)
            </h3>
            <div className="space-y-2">
              {parsed.steps.map((step: string, idx: number) => (
                <div key={idx} className="flex gap-3 text-[13px]">
                  <span className="shrink-0 w-6 h-6 flex items-center justify-center rounded-full bg-[var(--app-panel-strong)] text-[var(--app-text-muted)] text-[11px]">
                    {idx + 1}
                  </span>
                  <div className="flex-1 bg-[var(--app-panel)] border border-[var(--app-border)] rounded p-2.5">
                    {step}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Rubrics 评估点 */}
        {parsed.rubrics && parsed.rubrics.length > 0 && (
          <section>
            <h3 className="text-[15px] font-medium mb-3 flex items-center gap-2 text-[var(--app-text-strong)]">
              <span className="w-1.5 h-4 bg-purple-500 rounded-sm"></span> 评估点 (Rubrics)
            </h3>
            <div className="overflow-x-auto rounded-md border border-[var(--app-border)]">
              <table className="w-full text-left text-[13px] border-collapse">
                <thead className="bg-[var(--app-panel-strong)] text-[var(--app-text-muted)]">
                  <tr>
                    <th className="p-3 font-medium border-b border-[var(--app-border)] w-12 text-center">#</th>
                    <th className="p-3 font-medium border-b border-[var(--app-border)] min-w-[300px]">评估内容</th>
                    <th className="p-3 font-medium border-b border-[var(--app-border)] w-24">类型</th>
                    <th className="p-3 font-medium border-b border-[var(--app-border)] w-24">难度</th>
                    <th className="p-3 font-medium border-b border-[var(--app-border)] w-24 text-center">权重</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--app-border)] bg-[var(--app-panel)]">
                  {parsed.rubrics.map((rubric: string, idx: number) => (
                    <tr key={idx} className="hover:bg-[var(--app-bg)] transition-colors">
                      <td className="p-3 text-center text-[var(--app-text-weak)]">{idx + 1}</td>
                      <td className="p-3">{rubric}</td>
                      <td className="p-3">
                        {parsed.rubric_types?.[idx] && (
                          <span className="bg-gray-500/10 text-gray-400 px-2 py-0.5 rounded text-[11px]">
                            {parsed.rubric_types[idx]}
                          </span>
                        )}
                      </td>
                      <td className="p-3">
                        {parsed.rubric_diffs?.[idx] && (
                          <span className="text-[var(--app-text-muted)]">Level {parsed.rubric_diffs[idx]}</span>
                        )}
                      </td>
                      <td className="p-3 text-center">
                        {parsed.rubric_importance?.[idx] ? (
                          <span className="font-mono text-indigo-400">{parsed.rubric_importance[idx]}</span>
                        ) : (
                          '-'
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
