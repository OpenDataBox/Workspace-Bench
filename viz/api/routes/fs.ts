import fs from 'fs/promises'
import path from 'path'
import { Router, type Request, type Response } from 'express'
import { safeResolve, toPosixRelPath } from '../utils/fsSafe.js'
import { getDefaultOutputRoot } from '../services/runsService.js'
import type { FileReadResponse, FsNode } from '../../shared/types.js'

const router = Router()

function asyncRoute(fn: (req: Request, res: Response) => Promise<void>) {
  return (req: Request, res: Response, next: (err?: unknown) => void) => {
    Promise.resolve(fn(req, res)).catch(next)
  }
}

router.get(
  '/tree',
  asyncRoute(async (req: Request, res: Response) => {
  const outputRoot = getDefaultOutputRoot()
  const relPath = typeof req.query.path === 'string' ? req.query.path : '.'

  const dirAbs = safeResolve(outputRoot, relPath)
  const stat = await fs.stat(dirAbs)
  if (!stat.isDirectory()) {
    res.status(400).json({ error: 'Not a directory' })
    return
  }

  const entries = await fs.readdir(dirAbs, { withFileTypes: true })
  const children = await Promise.all(
    entries
      .filter((e) => !e.name.startsWith('.'))
      .slice(0, 400)
      .map(async (e) => {
        const abs = path.join(dirAbs, e.name)
        const s = await fs.stat(abs)
        const type = e.isDirectory() ? 'dir' : 'file'
        return {
          path: toPosixRelPath(outputRoot, abs),
          name: e.name,
          type,
          sizeBytes: type === 'file' ? s.size : undefined,
          mtimeMs: s.mtimeMs,
        } satisfies FsNode
      }),
  )

  const node: FsNode = {
    path: toPosixRelPath(outputRoot, dirAbs),
    name: path.basename(dirAbs),
    type: 'dir',
    children: children.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
      return a.name.localeCompare(b.name)
    }),
  }

  res.json(node)
  }),
)

router.get(
  '/file',
  asyncRoute(async (req: Request, res: Response) => {
  const outputRoot = getDefaultOutputRoot()
  const relPath = typeof req.query.path === 'string' ? req.query.path : ''
  if (!relPath) {
    res.status(400).json({ error: 'Missing path' })
    return
  }

  const maxBytesRaw = typeof req.query.maxBytes === 'string' ? Number(req.query.maxBytes) : 262144
  const maxBytes = Number.isFinite(maxBytesRaw) && maxBytesRaw > 0 ? Math.min(maxBytesRaw, 5_000_000) : 262144

  const fileAbs = safeResolve(outputRoot, relPath)
  const stat = await fs.stat(fileAbs)
  if (!stat.isFile()) {
    res.status(400).json({ error: 'Not a file' })
    return
  }

  const size = stat.size
  const readLen = Math.min(size, maxBytes)
  const fh = await fs.open(fileAbs, 'r')
  const buf = Buffer.alloc(readLen)
  try {
    await fh.read(buf, 0, readLen, 0)
  } finally {
    await fh.close()
  }

  const resp: FileReadResponse = {
    path: toPosixRelPath(outputRoot, fileAbs),
    encoding: 'utf-8',
    truncated: size > maxBytes,
    totalBytes: size,
    content: buf.toString('utf-8'),
  }

  res.json(resp)
  }),
)

export default router
