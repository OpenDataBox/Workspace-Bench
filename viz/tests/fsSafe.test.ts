import assert from 'node:assert/strict'
import test from 'node:test'
import path from 'node:path'
import { safeResolve } from '../api/utils/fsSafe.ts'

test('safeResolve allows paths inside root', () => {
  const root = path.resolve('/tmp/root')
  const abs = safeResolve(root, 'a/b/c.txt')
  assert.equal(abs, path.resolve(root, 'a/b/c.txt'))
})

test('safeResolve rejects path traversal', () => {
  const root = path.resolve('/tmp/root')
  assert.throws(() => safeResolve(root, '../secret.txt'))
})

