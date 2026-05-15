import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Workbench from '@/pages/Workbench'
import RunDetail from '@/pages/RunDetail'
import FileView from '@/pages/FileView'
import Stats from '@/pages/Stats'

const queryClient = new QueryClient()

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <Routes>
          <Route path="/" element={<Workbench />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/files" element={<FileView />} />
          <Route path="/stats" element={<Stats />} />
        </Routes>
      </Router>
    </QueryClientProvider>
  )
}
