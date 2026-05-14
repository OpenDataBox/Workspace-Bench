#!/usr/bin/env node

/**
 * Batch test runner for non-interactive testing.
 * Reads test tasks from a JSON config file and outputs detailed result records
 * whose structure is aligned with the reference report format.
 *
 * Usage:
 *   node batch-test.js <config.json> [options]
 *
 * Options:
 *   -o, --output <file>      Write JSON report to file
 *   -c, --concurrency <n>    Max parallel tasks (default: 1, sequential)
 *   --filter <pattern>       Only run tasks whose id matches the glob pattern
 *   -v, --verbose            Show real-time per-task output
 *   --dry-run                Preview tasks without executing
 *   --no-browser             Skip tasks that require browser (browser: true)
 *   -h, --help               Show help
 */

import fs from 'fs';
import path from 'path';
import { query } from '../../evaluation/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs';
import { fileURLToPath } from 'url';

// ─── CLI Argument Parsing ─────────────────────────────────────────────────────

function parseArgs(argv) {
  const args = argv.slice(2);
  const opts = {
    configFile: null,
    output: null,
    concurrency: 1,
    filter: null,
    verbose: false,
    dryRun: false,
    noBrowser: false,
    help: false,
  };

  let i = 0;
  while (i < args.length) {
    const arg = args[i];
    if (arg === '-h' || arg === '--help') {
      opts.help = true;
    } else if (arg === '-v' || arg === '--verbose') {
      opts.verbose = true;
    } else if (arg === '--dry-run') {
      opts.dryRun = true;
    } else if (arg === '--no-browser') {
      opts.noBrowser = true;
    } else if ((arg === '-o' || arg === '--output') && args[i + 1]) {
      opts.output = args[++i];
    } else if ((arg === '-c' || arg === '--concurrency') && args[i + 1]) {
      opts.concurrency = parseInt(args[++i], 10);
      if (isNaN(opts.concurrency) || opts.concurrency < 1) opts.concurrency = 1;
    } else if (arg === '--filter' && args[i + 1]) {
      opts.filter = args[++i];
    } else if (!arg.startsWith('-') && opts.configFile === null) {
      opts.configFile = arg;
    }
    i++;
  }
  return opts;
}

function printHelp() {
  console.log(`
Usage: node batch-test.js <config.json> [options]

Arguments:
  config.json              Path to the batch test configuration file

Options:
  -o, --output <file>      Write full JSON report to file
  -c, --concurrency <n>    Max tasks running in parallel (default: 1)
  --filter <pattern>       Only run tasks whose id matches the pattern (supports * wildcard)
  -v, --verbose            Print real-time per-task output
  --dry-run                List matching tasks without executing them
  --no-browser             Skip tasks that have browser: true
  -h, --help               Show this help message

Examples:
  node batch-test.js ./batch-test.json
  node batch-test.js ./tests.json -o ./report.json
  node batch-test.js ./tests.json -c 3
  node batch-test.js ./tests.json --filter "task-list-*"
  node batch-test.js ./tests.json -v
  node batch-test.js ./tests.json --dry-run
  node batch-test.js ./tests.json --no-browser
`);
}

// ─── Glob-style Pattern Matching ──────────────────────────────────────────────

function matchesPattern(str, pattern) {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
  return new RegExp(`^${escaped}$`).test(str);
}

// ─── Provider Environment Builder ────────────────────────────────────────────

function buildEnv(customProvider) {
  const env = { ...process.env };
  if (customProvider) {
    env.ANTHROPIC_AUTH_TOKEN = customProvider.apiKey;
    env.ANTHROPIC_API_KEY = customProvider.apiKey;
    env.ANTHROPIC_BASE_URL = customProvider.baseUrl;
    env.ANTHROPIC_MODEL = customProvider.modelName;
  }
  env.CLAUDE_DEBUG = 'false';
  env.ANTHROPIC_DEBUG = 'false';
  return env;
}

// ─── Claude Code CLI Path ─────────────────────────────────────────────────────

function getClaudeCodePath() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.join(here, '../../evaluation/node_modules/@anthropic-ai/claude-agent-sdk/cli.js');
}

// ─── Color Helpers (ANSI) ─────────────────────────────────────────────────────

const isTTY = process.stderr.isTTY;
const c = {
  reset:  isTTY ? '\x1b[0m'  : '',
  bold:   isTTY ? '\x1b[1m'  : '',
  dim:    isTTY ? '\x1b[2m'  : '',
  green:  isTTY ? '\x1b[32m' : '',
  red:    isTTY ? '\x1b[31m' : '',
  yellow: isTTY ? '\x1b[33m' : '',
  cyan:   isTTY ? '\x1b[36m' : '',
  blue:   isTTY ? '\x1b[34m' : '',
  grey:   isTTY ? '\x1b[90m' : '',
};

function statusColor(status) {
  switch (status) {
    case 'passed':  return c.green;
    case 'failed':  return c.red;
    case 'timeout': return c.yellow;
    case 'skipped': return c.cyan;
    default:        return c.grey;
  }
}

// ─── Core Task Runner ─────────────────────────────────────────────────────────

/**
 * Run one task and return a result record aligned with the reference report format.
 *
 * Report structure per task:
 *   id, name, prompt, status, exitCode, durationMs,
 *   provider, model, cwd, timeout, browser,
 *   claudeSessionId, messageCount,
 *   trajectory   – ordered interleaving of {type:"text"} and {type:"tool_call"} entries
 *   toolCalls    – flat list of all tool calls with full input/output detail
 *   textOutputs  – flat list of all assistant text strings
 *   errorMessage, stdout (raw JSON-lines stream), stderr,
 *   startedAt, finishedAt
 */
async function runTask(task, opts) {
  const startedAt = new Date();
  const startMs = startedAt.getTime();

  // Top-level safety net: no matter what goes wrong, always return a valid result
  // object so the caller (the concurrency runner) can continue with the next task.
  try {
    return await _runTaskImpl(task, opts, startedAt, startMs);
  } catch (fatalErr) {
    const finishedAt = new Date();
    const errMsg = fatalErr instanceof Error ? fatalErr.message : String(fatalErr);
    process.stderr.write(`[fatal] task ${task.id} threw unexpectedly: ${errMsg}\n`);
    return {
      id: task.id,
      name: task.name || task.id,
      prompt: task.prompt,
      status: 'failed',
      exitCode: 1,
      durationMs: finishedAt.getTime() - startMs,
      provider: task.provider ?? null,
      model: task.model ?? null,
      cwd: task.cwd ?? process.cwd(),
      timeout: task.timeout ?? 300,
      browser: task.browser ?? false,
      claudeSessionId: null,
      messageCount: 0,
      trajectory: [],
      toolCalls: [],
      textOutputs: [],
      errorMessage: errMsg,
      stdout: '',
      stderr: errMsg,
      startedAt: startedAt.toISOString(),
      finishedAt: finishedAt.toISOString(),
    };
  }
}

async function _runTaskImpl(task, opts, startedAt, startMs) {
  // Accumulators
  const trajectory = [];   // interleaved text + tool_call entries (reference format)
  const toolCalls = [];    // flat tool call list (same data, for easy lookup)
  const textOutputs = [];  // plain text strings
  const stdoutLines = [];  // raw JSON-lines stream (same as output.json stdout field)

  // Index: callID → toolCalls entry (for updating state when result arrives)
  const toolCallIndex = {};

  let claudeSessionId = null;
  let messageCount = 0;
  let status = 'failed';
  let exitCode = 1;
  let errorMessage = null;

  const log = (...args) => {
    if (opts.verbose) process.stderr.write(`  ${args.join(' ')}\n`);
  };

  const env = buildEnv(task.customProvider);
  if (task.cwd) env.HOME = task.cwd;
  const timeoutSec = task.timeout ?? 300;
  const abortController = new AbortController();

  // timeout: -1 means no limit
  const timeoutHandle = timeoutSec === -1 ? null : setTimeout(() => {
    status = 'timeout';
    log(`${c.yellow}[timeout]${c.reset} after ${timeoutSec}s`);
    abortController.abort();
  }, timeoutSec * 1000);

  try {
    const cwdRoot = path.resolve(task.cwd ?? process.cwd());
    const isUnderCwd = (p) => {
      if (typeof p !== 'string' || !p.trim()) return false;
      const abs = path.isAbsolute(p) ? path.resolve(p) : path.resolve(cwdRoot, p);
      return abs === cwdRoot || abs.startsWith(cwdRoot + path.sep);
    };
    const commandLooksSafe = (cmd) => {
      if (typeof cmd !== 'string') return false;
      const s = cmd;
      if (s.includes('..') || s.includes('~/') || s.includes('~\\')) return false;
      const allowed = ['/bin/', '/usr/', '/System/', '/Library/', '/Applications/'];
      const re = /(^|[\s"'])(\/[^\s"']+)/g;
      for (const m of s.matchAll(re)) {
        const p = m[2];
        if (allowed.some((pre) => p === pre.slice(0, -1) || p.startsWith(pre))) continue;
        if (!isUnderCwd(p)) return false;
      }
      return true;
    };

    const q = query({
      prompt: task.prompt,
      options: {
        cwd: task.cwd ?? process.cwd(),
        abortController,
        env,
        pathToClaudeCodeExecutable: getClaudeCodePath(),
        permissionMode: 'default',
        includePartialMessages: true,
        canUseTool: async (toolName, input, permissionOptions = {}) => {
          const tool = String(toolName || '');
          const inp = (input && typeof input === 'object') ? input : {};
          const toolUseID = typeof permissionOptions.toolUseID === 'string' ? permissionOptions.toolUseID : undefined;

          const fp = typeof inp.file_path === 'string' ? inp.file_path : null;
          const p = typeof inp.path === 'string' ? inp.path : null;
          const cmd = typeof inp.command === 'string' ? inp.command : null;

          const pathToCheck = fp ?? p;
          const isFileTool = ['Read', 'Write', 'Edit', 'NotebookEdit', 'Glob', 'Grep', 'LS', 'DeleteFile'].includes(tool);
          if (isFileTool && pathToCheck !== null && !isUnderCwd(pathToCheck)) {
            log(`${c.red}[deny]${c.reset} ${tool} ${pathToCheck}`);
            return {
              behavior: 'deny',
              message: `${tool} is not allowed outside the task working directory: ${pathToCheck}`,
              toolUseID,
            };
          }
          if (tool === 'Bash' && cmd !== null && !commandLooksSafe(cmd)) {
            log(`${c.red}[deny]${c.reset} Bash`);
            return {
              behavior: 'deny',
              message: `Bash command is not allowed because it references paths outside the task working directory.`,
              toolUseID,
            };
          }

          log(`${c.blue}[approve]${c.reset} ${tool}`);
          return { behavior: 'allow', updatedInput: inp, toolUseID };
        },
      },
    });

    for await (const msg of q) {
      messageCount++;

      // ── 1. Extract session ID from init message ───────────────────────────
      if (msg.type === 'system' && msg.subtype === 'init') {
        claudeSessionId = msg.session_id ?? null;
        log(`${c.grey}[session]${c.reset} ${claudeSessionId}`);
        // Emit raw line (type = system_init for clarity)
        stdoutLines.push(JSON.stringify({
          type: 'system_init',
          timestamp: Date.now(),
          sessionID: claudeSessionId,
          part: msg,
        }));
        continue;
      }

      // ── 2. Result message (task finished / error) ─────────────────────────
      if (msg.type === 'result') {
        if (status !== 'timeout') {
          status = msg.subtype === 'success' ? 'passed' : 'failed';
          exitCode = msg.subtype === 'success' ? 0 : 1;
        }
        log(`${statusColor(status)}[result]${c.reset} ${status}`);
        stdoutLines.push(JSON.stringify({
          type: 'result',
          timestamp: Date.now(),
          sessionID: claudeSessionId,
          subtype: msg.subtype,
          result: msg.result ?? null,
          isError: msg.is_error ?? false,
          durationMs: msg.duration_ms ?? null,
          usage: msg.usage ?? null,
          part: msg,
        }));
        continue;
      }

      // ── 3. step_start / step_finish pass-through ──────────────────────────
      if (msg.type === 'step_start' || msg.type === 'step_finish') {
        stdoutLines.push(JSON.stringify({
          type: msg.type,
          timestamp: Date.now(),
          sessionID: claudeSessionId,
          part: msg.part ?? msg,
        }));
        continue;
      }

      // ── 4. Assistant messages: text and tool_use blocks ───────────────────
      if (msg.type === 'assistant') {
        const content = Array.isArray(msg.message?.content) ? msg.message.content : [];
        const messageId = msg.message?.id ?? null;
        const ts = Date.now();

        for (const block of content) {
          if (block.type === 'text') {
            // ─ trajectory: text entry ──────────────────────────────────────
            trajectory.push({
              type: 'text',
              text: block.text,
              timestamp: ts,
              messageId,
            });
            textOutputs.push(block.text);
            log(`${c.dim}[text]${c.reset} ${block.text.substring(0, 80).replace(/\n/g, ' ')}…`);

            // ─ stdout line (text) ──────────────────────────────────────────
            stdoutLines.push(JSON.stringify({
              type: 'text',
              timestamp: ts,
              sessionID: claudeSessionId,
              part: {
                id: block.id ?? null,
                sessionID: claudeSessionId,
                messageID: messageId,
                type: 'text',
                text: block.text,
                time: { start: ts, end: ts },
              },
            }));

          } else if (block.type === 'tool_use') {
            const callID = block.id;
            const toolName = block.name;
            const toolInput = block.input ?? {};
            const toolState = block.state ?? {};
            const toolStatus = toolState?.status;
            const isCompleted = toolStatus === 'completed';
            const toolOutput = (toolState && Object.prototype.hasOwnProperty.call(toolState, 'output')) ? toolState.output : null;
            const toolExitCode = toolState?.metadata?.exit ?? toolState?.exit ?? null;
            const toolDurationMs = toolState?.time
              ? (toolState.time.end ?? 0) - (toolState.time.start ?? 0)
              : 0;

            if (callID && toolCallIndex[callID]) {
              const { trajectoryEntry, toolCallEntry } = toolCallIndex[callID];
              trajectoryEntry.input = toolInput;
              toolCallEntry.input = toolInput;
              if (isCompleted) {
                trajectoryEntry.state = 'completed';
                trajectoryEntry.output = toolOutput;
                trajectoryEntry.exitCode = toolExitCode;
                trajectoryEntry.durationMs = toolDurationMs;
                toolCallEntry.state = 'completed';
                toolCallEntry.output = toolOutput;
                toolCallEntry.exitCode = toolExitCode;
                toolCallEntry.durationMs = toolDurationMs;
              }
            } else {
              const trajectoryEntry = {
                type: 'tool_call',
                messageId,
                tool: toolName,
                callID,
                timestamp: ts,
                input: toolInput,
                state: isCompleted ? 'completed' : 'running',
                output: isCompleted ? toolOutput : null,
                exitCode: isCompleted ? toolExitCode : null,
                durationMs: isCompleted ? toolDurationMs : 0,
              };
              trajectory.push(trajectoryEntry);

              const toolCallEntry = {
                tool: toolName,
                callID,
                timestamp: ts,
                input: toolInput,
                state: isCompleted ? 'completed' : 'running',
                output: isCompleted ? toolOutput : null,
                exitCode: isCompleted ? toolExitCode : null,
                durationMs: isCompleted ? toolDurationMs : 0,
              };
              toolCalls.push(toolCallEntry);
              toolCallIndex[callID] = { trajectoryEntry, toolCallEntry };
            }

            log(`${c.blue}[tool]${c.reset} ${toolName} ${JSON.stringify(toolInput).substring(0, 120)}`);

            // ─ stdout line (tool_use, running state) ───────────────────────
            stdoutLines.push(JSON.stringify({
              type: 'tool_use',
              timestamp: ts,
              sessionID: claudeSessionId,
              part: {
                id: callID,
                sessionID: claudeSessionId,
                messageID: messageId,
                type: 'tool',
                callID,
                tool: toolName,
                state: {
                  status: isCompleted ? 'completed' : 'running',
                  input: toolInput,
                  ...(isCompleted ? { output: toolOutput, metadata: { exit: toolExitCode } } : {}),
                },
              },
            }));
          }
        }
        continue;
      }

      // ── 5. Tool result messages ───────────────────────────────────────────
      // The SDK can emit tool results as standalone messages with type 'tool'
      // OR they are embedded in the assistant stream as completed tool_use parts.
      // We handle both forms below.
      if (msg.type === 'tool') {
        const part = msg.part ?? msg;
        const callID = part.callID ?? part.call_id ?? msg.call_id ?? null;
        const toolState = part.state ?? {};
        const toolOutput = toolState.output ?? part.output ?? null;
        const toolInput = toolState.input ?? part.input ?? {};
        const toolName = part.tool ?? part.name ?? null;
        const toolExitCode = toolState.metadata?.exit ?? toolState.exit ?? part.exit_code ?? null;
        const toolDurationMs = part.time
          ? (part.time.end ?? 0) - (part.time.start ?? 0)
          : 0;
        const ts = Date.now();
        const messageId = part.messageID ?? null;

        if (callID && toolCallIndex[callID]) {
          // Update existing running entries
          const { trajectoryEntry, toolCallEntry } = toolCallIndex[callID];
          trajectoryEntry.state = 'completed';
          trajectoryEntry.output = toolOutput;
          trajectoryEntry.exitCode = toolExitCode;
          trajectoryEntry.durationMs = toolDurationMs;
          toolCallEntry.state = 'completed';
          toolCallEntry.output = toolOutput;
          toolCallEntry.exitCode = toolExitCode;
          toolCallEntry.durationMs = toolDurationMs;
          log(`${c.grey}[tool-done]${c.reset} ${toolCallEntry.tool} exit=${toolExitCode}`);
        } else if (callID && toolName) {
          // Tool result arrived without a prior tool_use block (some SDK versions)
          const trajectoryEntry = {
            type: 'tool_call',
            messageId,
            tool: toolName,
            callID,
            timestamp: ts,
            input: toolInput,
            state: 'completed',
            output: toolOutput,
            exitCode: toolExitCode,
            durationMs: toolDurationMs,
          };
          trajectory.push(trajectoryEntry);
          const toolCallEntry = {
            tool: toolName,
            callID,
            timestamp: ts,
            input: toolInput,
            state: 'completed',
            output: toolOutput,
            exitCode: toolExitCode,
            durationMs: toolDurationMs,
          };
          toolCalls.push(toolCallEntry);
          toolCallIndex[callID] = { trajectoryEntry, toolCallEntry };
          log(`${c.grey}[tool-done]${c.reset} ${toolName} exit=${toolExitCode}`);
        }

        // Emit complete tool_use stdout line (with full state)
        stdoutLines.push(JSON.stringify({
          type: 'tool_use',
          timestamp: ts,
          sessionID: claudeSessionId,
          part: {
            id: callID,
            sessionID: claudeSessionId,
            messageID: messageId,
            type: 'tool',
            callID,
            tool: toolName,
            state: toolState.status === 'completed' ? toolState : {
              ...toolState,
              status: 'completed',
              output: toolOutput,
              input: toolInput,
            },
          },
        }));
        continue;
      }

      // ── 6. Fallback: emit any other message type as-is ────────────────────
      stdoutLines.push(JSON.stringify({
        type: msg.type,
        timestamp: Date.now(),
        sessionID: claudeSessionId,
        ...(msg.subtype ? { subtype: msg.subtype } : {}),
        part: msg,
      }));
    }

    // If no explicit result message was received but no error, treat as passed
    if (status === 'failed' && !errorMessage && messageCount > 0) {
      status = 'passed';
      exitCode = 0;
    }

  } catch (err) {
    if (status !== 'timeout') {
      status = 'failed';
      exitCode = 1;
    }
    errorMessage = err instanceof Error ? err.message : String(err);
    log(`${c.red}[error]${c.reset} ${errorMessage}`);
  } finally {
    if (timeoutHandle !== null) clearTimeout(timeoutHandle);
  }

  // Normalize any still-running tool calls at end of task
  for (const entry of toolCalls) {
    if (status === 'timeout' && entry.state === 'running') {
      entry.state = 'failed';
      const idx = toolCallIndex[entry.callID];
      if (idx) idx.trajectoryEntry.state = 'failed';
    } else if (status !== 'timeout' && entry.state === 'running') {
      entry.state = 'completed';
      const idx = toolCallIndex[entry.callID];
      if (idx) idx.trajectoryEntry.state = 'completed';
    }
  }

  const finishedAt = new Date();

  return {
    id: task.id,
    name: task.name || task.id,
    prompt: task.prompt,
    status,
    exitCode,
    durationMs: finishedAt.getTime() - startMs,
    provider: task.provider ?? null,
    model: task.model ?? null,
    cwd: task.cwd ?? process.cwd(),
    timeout: task.timeout ?? 300,
    browser: task.browser ?? false,
    claudeSessionId,
    messageCount,
    trajectory,
    toolCalls,
    textOutputs,
    errorMessage: errorMessage ?? null,
    stdout: stdoutLines.join('\n'),
    stderr: '',
    startedAt: startedAt.toISOString(),
    finishedAt: finishedAt.toISOString(),
  };
}

// ─── Concurrency Pool ─────────────────────────────────────────────────────────

async function runWithConcurrency(tasks, concurrency, runner) {
  const results = new Array(tasks.length);
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < tasks.length) {
      const index = nextIndex++;
      results[index] = await runner(tasks[index], index);
    }
  }

  const workers = Array.from({ length: Math.min(concurrency, tasks.length) }, worker);
  await Promise.all(workers);
  return results;
}

// ─── Skipped Task Record ──────────────────────────────────────────────────────

function makeSkippedRecord(task, reason, timestamp) {
  return {
    id: task.id,
    name: task.name || task.id,
    prompt: task.prompt,
    status: 'skipped',
    exitCode: null,
    durationMs: 0,
    provider: task.provider ?? null,
    model: task.model ?? null,
    cwd: task.cwd ?? process.cwd(),
    timeout: task.timeout ?? 300,
    browser: task.browser ?? false,
    claudeSessionId: null,
    messageCount: 0,
    trajectory: [],
    toolCalls: [],
    textOutputs: [],
    errorMessage: reason,
    stdout: '',
    stderr: '',
    startedAt: timestamp,
    finishedAt: timestamp,
  };
}

// ─── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  const opts = parseArgs(process.argv);

  if (opts.help) {
    printHelp();
    process.exit(0);
  }

  if (!opts.configFile) {
    console.error('Error: config file argument is required.\nRun with --help for usage.');
    process.exit(1);
  }

  const configPath = path.resolve(process.cwd(), opts.configFile);
  if (!fs.existsSync(configPath)) {
    console.error(`Error: config file not found: ${configPath}`);
    process.exit(1);
  }

  let config;
  try {
    config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch (e) {
    console.error(`Error: failed to parse config file: ${e.message}`);
    process.exit(1);
  }

  if (!Array.isArray(config.tasks) || config.tasks.length === 0) {
    console.error('Error: config file must contain a non-empty "tasks" array.');
    process.exit(1);
  }

  const globalStartedAt = new Date().toISOString();
  const globalStartMs = Date.now();

  // ── Filter tasks ────────────────────────────────────────────────────────────
  let activeTasks = config.tasks;

  if (opts.filter) {
    activeTasks = activeTasks.filter(t => matchesPattern(t.id, opts.filter));
    if (activeTasks.length === 0) {
      console.error(`No tasks match filter pattern: ${opts.filter}`);
      process.exit(1);
    }
  }

  // Tasks skipped by --no-browser
  const skippedTasks = opts.noBrowser
    ? activeTasks.filter(t => t.browser)
    : [];
  if (opts.noBrowser) {
    activeTasks = activeTasks.filter(t => !t.browser);
    if (skippedTasks.length > 0) {
      console.log(`${c.cyan}[info]${c.reset} Skipped ${skippedTasks.length} browser task(s) due to --no-browser`);
    }
  }

  // ── Dry run ─────────────────────────────────────────────────────────────────
  if (opts.dryRun) {
    console.log(`${c.bold}Dry run – tasks that would be executed:${c.reset}\n`);
    for (const task of activeTasks) {
      console.log(`  ${c.cyan}${task.id}${c.reset}  ${task.name || ''}`);
      console.log(`    prompt   : ${task.prompt.substring(0, 80).replace(/\n/g, ' ')}${task.prompt.length > 80 ? '…' : ''}`);
      console.log(`    cwd      : ${task.cwd ?? process.cwd()}`);
      console.log(`    provider : ${task.provider ?? 'default'}`);
      console.log(`    model    : ${task.model ?? 'default'}`);
      console.log(`    timeout  : ${task.timeout ?? 300}s`);
      console.log(`    browser  : ${task.browser ?? false}`);
      console.log('');
    }
    console.log(`Total: ${activeTasks.length} task(s) (${skippedTasks.length} skipped)`);
    process.exit(0);
  }

  // ── Print header ──────────────────────────────────────────────────────────
  console.log(`\n${c.bold}Batch Test Runner${c.reset}`);
  console.log(`${'─'.repeat(60)}`);
  if (config.description) console.log(`Description : ${config.description}`);
  console.log(`Config      : ${configPath}`);
  console.log(`Tasks       : ${activeTasks.length} (${skippedTasks.length} skipped)`);
  console.log(`Concurrency : ${opts.concurrency}`);
  if (opts.output) console.log(`Report      : ${opts.output}`);
  console.log(`${'─'.repeat(60)}\n`);

  // ── Incremental report writer ────────────────────────────────────────────
  // Writes the report file after every completed task so partial results are
  // preserved even if the process is interrupted mid-run.
  const completedResults = [];   // grows as tasks finish
  const skippedResults = skippedTasks.map(t =>
    makeSkippedRecord(t, 'Skipped: --no-browser flag is set', globalStartedAt)
  );

  function buildReport(finishedAt, durationMs) {
    const allSoFar = [...completedResults, ...skippedResults];
    return {
      description: config.description ?? '',
      configFile: configPath,
      startedAt: globalStartedAt,
      finishedAt,
      totalDurationMs: durationMs,
      summary: {
        total: activeTasks.length + skippedResults.length,
        passed:  allSoFar.filter(r => r.status === 'passed').length,
        failed:  allSoFar.filter(r => r.status === 'failed').length,
        timeout: allSoFar.filter(r => r.status === 'timeout').length,
        skipped: allSoFar.filter(r => r.status === 'skipped').length,
      },
      tasks: allSoFar,
    };
  }

  function flushReport() {
    if (!opts.output) return;
    const now = new Date().toISOString();
    const ms = Date.now() - globalStartMs;
    const outPath = path.resolve(process.cwd(), opts.output);
    try {
      fs.writeFileSync(outPath, JSON.stringify(buildReport(now, ms), null, 2), 'utf8');
    } catch (e) {
      process.stderr.write(`[warn] failed to write report: ${e.message}\n`);
    }
  }

  // Ensure partial report is flushed on external termination (e.g. Python timeout handler)
  const terminateAndFlush = (sig) => {
    try { flushReport(); } catch (_) {}
    try { process.stderr.write(`[warn] received ${sig}, exiting\n`); } catch (_) {}
    process.exit(124);
  };
  process.once('SIGTERM', () => terminateAndFlush('SIGTERM'));
  process.once('SIGINT', () => terminateAndFlush('SIGINT'));

  // ── Execute tasks ────────────────────────────────────────────────────────
  const taskResults = await runWithConcurrency(activeTasks, opts.concurrency, async (task, idx) => {
    const total = activeTasks.length;
    try {
      process.stdout.write(
        `[${String(idx + 1).padStart(String(total).length)}/${total}] ${c.bold}${task.id}${c.reset} ${c.dim}${task.name || ''}${c.reset} … `
      );
      if (opts.verbose) process.stdout.write('\n');
    } catch (_) { /* ignore tty errors */ }

    // runTask itself never throws (top-level safety net inside)
    const result = await runTask(task, opts);

    try {
      const statusStr = `${statusColor(result.status)}${result.status.toUpperCase()}${c.reset}`;
      const dur = `${c.dim}(${(result.durationMs / 1000).toFixed(1)}s)${c.reset}`;
      if (opts.verbose) {
        process.stdout.write(`    → ${statusStr} ${dur}\n`);
      } else {
        process.stdout.write(`${statusStr} ${dur}\n`);
      }
    } catch (_) { /* ignore tty errors */ }

    // Save incrementally after each task completes
    completedResults.push(result);
    flushReport();

    return result;
  });

  const allResults = [...taskResults, ...skippedResults];

  // ── Summary ────────────────────────────────────────────────────────────────
  const globalFinishedAt = new Date().toISOString();
  const totalDurationMs = Date.now() - globalStartMs;

  const summary = {
    total: allResults.length,
    passed: allResults.filter(r => r.status === 'passed').length,
    failed: allResults.filter(r => r.status === 'failed').length,
    timeout: allResults.filter(r => r.status === 'timeout').length,
    skipped: allResults.filter(r => r.status === 'skipped').length,
  };

  console.log(`\n${'─'.repeat(60)}`);
  console.log(`${c.bold}Summary${c.reset}`);
  console.log(`${'─'.repeat(60)}`);
  console.log(`Total   : ${summary.total}`);
  console.log(`${c.green}Passed${c.reset}  : ${summary.passed}`);
  if (summary.failed > 0)  console.log(`${c.red}Failed${c.reset}  : ${summary.failed}`);
  if (summary.timeout > 0) console.log(`${c.yellow}Timeout${c.reset} : ${summary.timeout}`);
  if (summary.skipped > 0) console.log(`${c.cyan}Skipped${c.reset} : ${summary.skipped}`);
  console.log(`Duration: ${(totalDurationMs / 1000).toFixed(2)}s`);

  if (summary.failed > 0 || summary.timeout > 0) {
    console.log(`\n${c.red}Failed/Timeout tasks:${c.reset}`);
    for (const r of allResults.filter(x => x.status === 'failed' || x.status === 'timeout')) {
      console.log(`  • ${r.id}  ${c.dim}${r.name}${c.reset}`);
      if (r.errorMessage) console.log(`    ${c.grey}${r.errorMessage}${c.reset}`);
    }
  }

  // ── Write final JSON report ────────────────────────────────────────────────
  // (incremental flushes already happened after each task; this is the final save
  //  with the definitive finishedAt / totalDurationMs values)
  if (opts.output) {
    flushReport();
    const outPath = path.resolve(process.cwd(), opts.output);
    console.log(`\nReport written to: ${outPath}`);
  }

  console.log('');
  process.exit(summary.failed > 0 || summary.timeout > 0 ? 1 : 0);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});

// Suppress noisy SDK debug permission errors
process.on('uncaughtException', err => {
  if (err.code === 'EPERM' && err.path?.includes('.claude/debug')) return;
  console.error('Uncaught exception:', err);
  process.exit(1);
});
