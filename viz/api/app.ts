/**
 * This is a API server
 */

import express, {
  type Request,
  type NextFunction,
  type Response,
} from 'express'
import cors from 'cors'
import dotenv from 'dotenv'
import runsRoutes from './routes/runs.js'
import fsRoutes from './routes/fs.js'
import statsRoutes from './routes/stats.js'

// load env
dotenv.config()

const app: express.Application = express()

app.use(cors())
app.use(express.json({ limit: '10mb' }))
app.use(express.urlencoded({ extended: true, limit: '10mb' }))

/**
 * API Routes
 */
app.use('/api/runs', runsRoutes)
app.use('/api/fs', fsRoutes)
app.use('/api/stats', statsRoutes)

/**
 * health
 */
app.use(
  '/api/health',
  (req: Request, res: Response): void => {
    res.status(200).json({
      success: true,
      message: 'ok',
    })
  },
)

/**
 * error handler middleware
 */
app.use((error: Error, req: Request, res: Response, _next: NextFunction) => {
  res.status(500).json({ success: false, error: error.message || 'Server internal error' })
})


/**
 * 404 handler
 */
app.use((req: Request, res: Response) => {
  res.status(404).json({
    success: false,
    error: 'API not found',
  })
})

export default app
