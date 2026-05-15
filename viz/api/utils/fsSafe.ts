import path from 'path'

export function safeResolve(rootAbsPath: string, relPath: string): string {
  const cleaned = relPath.replace(/\\/g, '/')
  const abs = path.resolve(rootAbsPath, cleaned)
  const root = path.resolve(rootAbsPath)
  const prefix = root.endsWith(path.sep) ? root : root + path.sep
  if (abs === root || abs.startsWith(prefix)) {
    return abs
  }
  throw new Error('Invalid path')
}

export function toPosixRelPath(fromAbsRoot: string, absPath: string): string {
  const rel = path.relative(fromAbsRoot, absPath)
  return rel.split(path.sep).join('/') || '.'
}

