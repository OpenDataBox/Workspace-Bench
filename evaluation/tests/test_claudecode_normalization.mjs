import test from 'node:test';
import assert from 'node:assert/strict';
import { applyToolResultMessage } from '../baselines/ClaudeCode.js';

test('backfills ClaudeCode tool output from user tool_result messages', () => {
  const trajectoryEntry = {
    type: 'tool_call',
    callID: 'call-1',
    state: 'running',
    output: null,
  };
  const toolCallEntry = {
    callID: 'call-1',
    state: 'running',
    output: null,
  };
  const toolCallIndex = {
    'call-1': { trajectoryEntry, toolCallEntry },
  };

  const handled = applyToolResultMessage(
    {
      type: 'user',
      message: {
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'call-1',
            content: 'file list',
            is_error: false,
          },
        ],
      },
      tool_use_result: { durationMs: 12, exitCode: 0 },
    },
    toolCallIndex,
  );

  assert.equal(handled, 1);
  assert.equal(trajectoryEntry.state, 'completed');
  assert.equal(trajectoryEntry.output, 'file list');
  assert.equal(trajectoryEntry.durationMs, 12);
  assert.equal(trajectoryEntry.exitCode, 0);
  assert.equal(toolCallEntry.output, 'file list');
});
