import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import TopBar from '@/components/TopBar'
import RunList from '@/components/RunList'
import FileTree from '@/components/FileTree'
import OutputPreview from '@/components/OutputPreview'
import FilePreview from '@/components/FilePreview'
import ToolStepsPanel from '@/components/ToolStepsPanel'
import JudgePanel from '@/components/JudgePanel'
import MetadataPreview from '@/components/MetadataPreview'
import { useAppStore } from '@/store/useAppStore'
import type { RunDetail } from '../../shared/types'
import { apiGet } from '@/utils/api'
import { cn } from '@/lib/utils'

export default function Workbench() {
  const navigate = useNavigate()
  const { selectedRunId, previewTab, setPreviewTab } = useAppStore()

  const runDetailQ = useQuery({
    queryKey: ['runDetail', selectedRunId],
    enabled: !!selectedRunId,
    queryFn: () => apiGet<RunDetail>(`/api/runs/${encodeURIComponent(selectedRunId!)}`),
  })

  const workspacePath = useMemo(() => {
    // workspacePath：运行目录下以 "_" 开头的工作区目录（如果存在）。
    // 某些 runner 会把可回放的“工作区快照”放在这里，便于浏览文件。
    if (!selectedRunId) return '.'
    return runDetailQ.data?.workspacePath ?? '.'
  }, [runDetailQ.data?.workspacePath, selectedRunId])

  const treeRoot = useMemo(() => {
    // 文件树根节点优先级：
    // 1) runPath：整个 run 目录（包含 agent.json/agent.log/metadata/output/raw 等）
    // 2) workspacePath：如果 runPath 缺失，则退回工作区目录
    if (!selectedRunId) return '.'
    return runDetailQ.data?.runPath ?? workspacePath
  }, [runDetailQ.data?.runPath, selectedRunId, workspacePath])

  useEffect(() => {
    if (previewTab !== 'metadata' && previewTab !== 'output' && previewTab !== 'file' && previewTab !== 'steps' && previewTab !== 'judge') setPreviewTab('metadata')
  }, [previewTab, setPreviewTab])

  return (
    <div className="min-h-screen bg-[var(--app-bg)] text-[var(--app-text)]">
      <TopBar
        rightSlot={
          <button
            type="button"
            className="rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]"
            onClick={() => navigate('/stats')}
          >
            实验评估统计
          </button>
        }
        subtitle={
          selectedRunId
            ? `当前运行：${runDetailQ.data?.name ?? selectedRunId} / Agent：${runDetailQ.data?.agent ?? '-'} / 模型：${runDetailQ.data?.model ?? runDetailQ.data?.modelLabel ?? runDetailQ.data?.backbone ?? '-'}`
            : '请选择运行记录以开始浏览'
        }
      />

      <div className="mx-auto flex h-[calc(100vh-52px)] max-w-[1600px] gap-4 p-4">
        <div className="w-[280px] shrink-0">
          <FileTree key={treeRoot} rootPath={treeRoot} />
        </div>

        <div className="flex min-w-0 flex-1 flex-col">
          <PanelGroup direction="vertical" className="min-h-0 flex-1">
            <Panel defaultSize={45} minSize={25} className="min-h-0">
              <RunList />
            </Panel>

            <PanelResizeHandle className="my-2 h-2 rounded-md bg-[var(--app-panel)] hover:bg-[var(--app-panel-strong)]" />

            <Panel defaultSize={55} minSize={25} className="min-h-0">
              <div className="flex h-full flex-col gap-2">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className={cn(
                      'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]',
                      previewTab === 'metadata' && 'bg-[var(--app-panel-strong)] text-[var(--app-text)]',
                    )}
                    onClick={() => setPreviewTab('metadata')}
                  >
                    任务详情
                  </button>
                  <button
                    type="button"
                    className={cn(
                      'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]',
                      previewTab === 'output' && 'bg-[var(--app-panel-strong)] text-[var(--app-text)]',
                    )}
                    onClick={() => setPreviewTab('output')}
                  >
                    输出
                  </button>
                  <button
                    type="button"
                    className={cn(
                      'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]',
                      previewTab === 'file' && 'bg-[var(--app-panel-strong)] text-[var(--app-text)]',
                    )}
                    onClick={() => setPreviewTab('file')}
                  >
                    文件
                  </button>
                  <button
                    type="button"
                    className={cn(
                      'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]',
                      previewTab === 'steps' && 'bg-[var(--app-panel-strong)] text-[var(--app-text)]',
                    )}
                    onClick={() => setPreviewTab('steps')}
                  >
                    工具调用
                  </button>
                  <button
                    type="button"
                    className={cn(
                      'rounded-md border border-[var(--app-border)] bg-[var(--app-panel)] px-3 py-1.5 text-xs text-[var(--app-text-muted)] hover:bg-[var(--app-panel-strong)]',
                      previewTab === 'judge' && 'bg-[var(--app-panel-strong)] text-[var(--app-text)]',
                    )}
                    onClick={() => setPreviewTab('judge')}
                  >
                    评估
                  </button>
                  {runDetailQ.isLoading ? <div className="text-[11px] text-[var(--app-text-weak)]">加载运行详情…</div> : null}
                  {runDetailQ.isError ? <div className="text-[11px] text-red-200">{String(runDetailQ.error)}</div> : null}
                </div>

                <div className="min-h-0 flex-1">
                  {previewTab === 'metadata' ? (
                    <MetadataPreview metadataPath={runDetailQ.data?.metadataPath} />
                  ) : previewTab === 'output' ? (
                    <OutputPreview />
                  ) : previewTab === 'file' ? (
                    <FilePreview />
                  ) : previewTab === 'steps' ? (
                    <ToolStepsPanel runId={selectedRunId} />
                  ) : (
                    <JudgePanel runId={selectedRunId} />
                  )}
                </div>
              </div>
            </Panel>
          </PanelGroup>
        </div>
      </div>
    </div>
  )
}
