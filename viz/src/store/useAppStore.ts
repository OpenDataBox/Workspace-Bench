import { create } from 'zustand'

type PreviewTab = 'metadata' | 'output' | 'file' | 'steps' | 'judge'

interface AppState {
  selectedRunId?: string
  setSelectedRunId: (runId?: string) => void

  selectedFilePath?: string
  setSelectedFilePath: (path?: string) => void

  previewTab: PreviewTab
  setPreviewTab: (tab: PreviewTab) => void

  autoScrollOutput: boolean
  setAutoScrollOutput: (v: boolean) => void
}

export const useAppStore = create<AppState>((set) => ({
  selectedRunId: undefined,
  setSelectedRunId: (runId) => set({ selectedRunId: runId }),
  selectedFilePath: undefined,
  setSelectedFilePath: (p) => set({ selectedFilePath: p }),
  previewTab: 'metadata',
  setPreviewTab: (tab) => set({ previewTab: tab }),
  autoScrollOutput: true,
  setAutoScrollOutput: (v) => set({ autoScrollOutput: v }),
}))
