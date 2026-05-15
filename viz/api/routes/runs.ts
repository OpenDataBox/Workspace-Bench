import { Router, type Request, type Response } from 'express'
import { getDefaultOutputRoot, getRunDetail, getRunOutput, listRuns } from '../services/runsService.js'
import { getRunSteps } from '../services/stepsService.js'
import { getRunJudge } from '../services/judgeService.js'

const router = Router()

function asyncRoute(fn: (req: Request, res: Response) => Promise<void>) {
  // express 对 async handler 的错误不会自动捕获，需要手动 catch(next)。
  return (req: Request, res: Response, next: (err?: unknown) => void) => {
    Promise.resolve(fn(req, res)).catch(next)
  }
}

router.get(
  '/',
  asyncRoute(async (req: Request, res: Response) => {
  // 列表接口：扫描 outputRoot 下的 agent/run 目录，拼成给前端展示的 RunSummary。
  const outputRoot = getDefaultOutputRoot()
  const runs = await listRuns(outputRoot)
  res.json(runs)
  }),
)

router.get(
  '/:runId',
  asyncRoute(async (req: Request, res: Response) => {
  // 详情接口：返回 run 的路径、配置、日志等“文件指针”，供前端进一步请求。
  const outputRoot = getDefaultOutputRoot()
  const detail = await getRunDetail(outputRoot, req.params.runId)
  res.json(detail)
  }),
)

router.get(
  '/:runId/output',
  asyncRoute(async (req: Request, res: Response) => {
  // 输出接口：format=text 返回可读文本；format=raw 返回原始 JSONL/日志。
  const outputRoot = getDefaultOutputRoot()
  const format = req.query.format === 'raw' ? 'raw' : 'text'
  const out = await getRunOutput(outputRoot, req.params.runId, format)
  res.json(out)
  }),
)

router.get(
  '/:runId/steps',
  asyncRoute(async (req: Request, res: Response) => {
  // 步骤接口：把 agent.log 的 JSONL 解析成 RunStep 列表（工具调用/文本/系统/结果）。
  const outputRoot = getDefaultOutputRoot()
  const steps = await getRunSteps(outputRoot, req.params.runId)
  res.json(steps)
  }),
)

router.get(
  '/:runId/judge',
  asyncRoute(async (req: Request, res: Response) => {
  const outputRoot = getDefaultOutputRoot()
  const judgeModel = typeof req.query.judgeModel === 'string' ? req.query.judgeModel : undefined
  const judge = await getRunJudge(outputRoot, req.params.runId, judgeModel)
  res.json(judge)
  }),
)

router.get(
  '/:runId/output/stream',
  asyncRoute(async (req: Request, res: Response) => {
  const outputRoot = getDefaultOutputRoot()
  res.setHeader('Content-Type', 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache')
  res.setHeader('Connection', 'keep-alive')
  res.flushHeaders()

  const out = await getRunOutput(outputRoot, req.params.runId, 'text')
  const payload = {
    runId: out.runId,
    seq: 0,
    ts: new Date().toISOString(),
    level: 'INFO',
    text: out.content,
  }
  res.write(`event: message\n`)
  res.write(`data: ${JSON.stringify(payload)}\n\n`)
  res.write(`event: done\n`)
  res.write(`data: {}\n\n`)
  res.end()
  }),
)

export default router
