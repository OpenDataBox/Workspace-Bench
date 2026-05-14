import { Router, type Request, type Response } from 'express'
import { getDefaultOutputRoot } from '../services/runsService.js'
import { getEvalStats } from '../services/statsService.js'

const router = Router()

function asyncRoute(fn: (req: Request, res: Response) => Promise<void>) {
  return (req: Request, res: Response, next: (err?: unknown) => void) => {
    Promise.resolve(fn(req, res)).catch(next)
  }
}

router.get(
  '/eval',
  asyncRoute(async (req: Request, res: Response) => {
    const outputRoot = getDefaultOutputRoot()
    const experiment = typeof req.query.experiment === 'string' ? req.query.experiment : undefined
    const group = typeof req.query.group === 'string' ? req.query.group : undefined
    const bucketSize = typeof req.query.bucketSize === 'string' ? Number(req.query.bucketSize) : undefined
    const data = await getEvalStats(outputRoot, experiment, group, typeof bucketSize === 'number' && Number.isFinite(bucketSize) ? bucketSize : undefined)
    res.json(data)
  }),
)

export default router
