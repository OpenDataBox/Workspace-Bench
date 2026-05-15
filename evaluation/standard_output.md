# 单任务标准输出规范（standard_output.md）

本文档规范 Workspace-Bench evaluation 接入任意 Agent 后，每个任务（case）在输出目录中必须包含的文件、目录结构、字段格式，以及“如何从 Agent 输出中检索最终产出文件”的统一规则。

该规范的目标：
- 让不同 Agent 的输出可以被统一评测与对齐对比
- 让调试信息（推理/工具调用/LLM 用量/错误）可追溯
- 让最终产出文件的收集规则可复用、可解释

---

## 1. 目录结构（必需）

每次评测会生成一个 runsRoot（通常等于 outDir），其下按 agentId、caseId 组织：

```
<runsRoot>/
  <agentId>/
    <caseId>/
      agent.json
      agent.log
      metadata.json
      output/
        ... (本任务的“最终产出文件”集合，仅包含少量文件)
      raw/                       (可选，强烈建议)
        ... (agent 原始输出、SDK/batch/openclaw 原始报告等)
```

说明：
- `<agentId>`：Agent 唯一标识；允许带 `--<model_name>` 后缀用于区分模型/骨干。`--<model_name>` 后可能还有 `--<run_name>`来区分不同的运行。
- `<caseId>`：任务唯一标识（来自任务 metadata 的 id 或目录名）。

---

## 2. output/（必需）

`output/` 目录用于保存“该 Agent 完成任务后最终提交的产出文件集合”。评测系统会基于“输出检索逻辑”（见第 5 节）从工作目录中定位文件，并复制到本目录。

### 2.1 output/ 的约束

- `output/` **不得**拷贝整个工作目录所有文件。
- `output/` **只应**包含：
  1) 通过“输出检索逻辑”定位到的最终产出文件；以及  
  2) 必要的最小回退文件（例如：当没有找到任何产出时，可回退拷贝 `summary.json` 之类用于调试，但必须在 agent.json 中说明）。
- `output/` 文件名为原文件 basename，若同名冲突，应在拷贝时进行去重（例如加序号后缀），并在 `agent.json` 的 manifest 中记录真实来源。

### 2.2 output/ 推荐补充文件（可选）

如果 Agent 本身不会产出“业务文件”（如 docx/xlsx/html），仍建议至少输出一个简短的 `summary.json` 或 `report.md`，以便评测/调试。

---

## 3. agent.json（必需）

`agent.json` 是单任务最重要的结构化日志文件，必须包含：
- 任务基本信息
- 输出检索结果（最终拷贝到 output/ 的清单）
- 推理与工具调用轨迹（逐步）
- LLM 调用用量与细节（总量与分调用）
- 错误与异常（含 request_id 等可追踪信息）

你可以参考已有样例：
- OpenClaw 轨迹样例：[agent.json](file:///path/to/Workspace-Bench/evaluation/output/milestone20/OpenClaw--Seed-Code/%E4%BB%93%E6%95%8F_1/agent.json)
- Claude(batch-test) LLM 用量样例：[result.json](file:///path/to/Workspace-Bench/evaluation/output/milestone20/claude--Seed-Code/%E4%BB%93%E6%95%8F_1/result.json)（该文件较大，agent.json 应提取其中关键字段并汇总）

### 3.1 agent.json 顶层字段（必需）

```json
{
  "caseId": "string",
  "name": "string",
  "workDir": "string",
  "status": "passed | failed | error | timeout",
  "durationMs": 12345,
  "turns": 12,
  "promptTokens": 0,
  "completionTokens": 0,
  "totalTokens": 0,
  "checks": [
    {
      "type": "returned_paths_exist | ...",
      "passed": true,
      "detail": {}
    }
  ],
  "errorType": "string | null",
  "errorMessage": "string | null",
  "traceback": "string | null",
  "trace": {}
}
```

字段解释：
- `turns/promptTokens/completionTokens/totalTokens`：建议为**全任务汇总**。推荐从 `trace.executionTrace[]` 中聚合得到；若上游无法提供则为 `null` 或 `0`，并在 `trace.llm.usageTotal` 或 `trace.raw` 中说明原因。

### 3.2 trace 字段（必需）

`trace` 用于存放结构化轨迹与细节。推荐包含如下键：

```json
{
  "trace": {
    "prompt": {
      "system": "string",
      "user": "string",
      "promptTail": "string | null"
    },
    "executionTrace": [
      {
        "type": "text",
        "role": "system | user | assistant",
        "content": "string",
        "timestamp": "ISO8601 | null",
        "turn": "number | null",
        "llm": {
          "provider": "string | null",
          "baseUrl": "string | null",
          "model": "string | null",
          "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_read": 0,
            "cache_write": 0
          },
          "stopReason": "string | null",
          "errorMessage": "string | null"
        }
      },
      {
        "type": "tool",
        "role": "tool",
        "tool": "string",
        "callID": "string",
        "timestamp": "ISO8601 | null",
        "startedAt": "ISO8601 | null",
        "finishedAt": "ISO8601 | null",
        "durationMs": 0,
        "status": "completed | failed | null",
        "exitCode": 0,
        "input": {},
        "output": {}
      }
    ],
    "llm": {
      "provider": "string | null",
      "baseUrl": "string | null",
      "model": "string | null",
      "usageTotal": {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read": 0,
        "cache_write": 0
      }
    },
    "outputs": {
      "returnedPaths": ["relative/path.ext"],
      "retrievalMethod": "skipped | last_text_paths | expected_filenames_recent | returned_paths_recent | returned_paths | path_json | fallback",
      "outputManifest": [
        {
          "sourcePath": "string",
          "outputPath": "string",
          "sizeBytes": 0
        }
      ]
    },
    "raw": {
      "stdout": "string",
      "stderr": "string"
    }
  }
}
```

说明：
- `trace.executionTrace`：本规范下的**唯一权威轨迹源**，包含文本与工具事件；每条事件应尽量带上 `durationMs/exitCode/usage` 等信息，避免再用单独的 tools/calls 列表重复存储。
- `trace.llm.usageTotal`：可选的“全任务汇总”，用于快速统计；其来源应可由 `executionTrace[]` 聚合得到或说明无法获取。
- `trace.outputs.retrievalMethod`：必须标注本任务最终输出是用哪一种检索方式拿到的（见第 5 节）。

---

## 4. agent.log（必需）

`agent.log` 是纯文本日志，用于快速定位问题（例如调试时无需打开大型 JSON）。

建议至少包含：
- Runner 启动信息与参数摘要（baseUrl/model/timeout/workDir）
- 每轮调用的简要信息（turn、耗时、是否 tool call）
- 报错的 HTTP 状态码与 request_id（如果可获取）

---

## 5. 输出文件检索逻辑（必需遵循）

评测系统应按以下优先级收集输出文件路径，并最终只复制这些“明确输出”到 `output/`。

### 5.1 方式 1：根据模型最后一步文本输出解析路径列表（优先）

规则：
- 模型最后一步必须输出一个 Python 列表 `list[str]`（路径列表）。
- 路径必须是相对 `workDir` 的相对路径（不以 `/` 开头）。
- 评测系统解析列表后，对每个路径进行存在性校验（必须存在且位于 `workDir` 内），通过则加入输出集合。
- 如果该方式得到至少 1 个有效文件，则停止后续检索。

### 5.2 方式 2：根据任务 metadata 的 output_files 按文件名在 workDir 内检索（兜底）

规则：
- 从任务 metadata 中读取 `output_files`（或 `output_file`）。
- 取每个输出文件名的 basename。
- 在 `workDir` 下递归查找同名文件，找到则加入输出集合。
- 如果仍未找到任何文件，则进入方式 3（可选）或返回空集合。

### 5.3 方式 3：Runner 结构化产物（可选增强）

如果 runner 会生成结构化清单（例如 `path.json`），可作为补充信息：
- 仅当方式 1 与方式 2 都未命中时，才允许参考结构化清单；
- 仍必须对路径进行存在性校验与工作目录边界校验。

### 5.4 重要约束：禁止“扫描整个 workDir 当输出”

即便 workDir 很小，也必须遵循上述检索优先级；不得将“workDir 中除输入外的所有文件”当成输出集合。

---

## 6. raw/（可选但强烈建议）

`raw/` 目录用于保存 Agent 原始输出，以便未来重放与审计。常见来源：

- batch-test/claude runner：
  - `batch_test_config.json`
  - `batch_test_report.json`
  - `result.json`（包含 llmCalls、完整 prompt、stdout/stderr 等）
- openclaw runner：
  - openclaw 原始轨迹文件（如存在）
  - runner 自带的 session/trace 文件
- 自研 runner：
  - `conversation.json` / `summary.json` / `path.json`
  - 网络请求摘要（注意不要写入 API key）

建议：
- `agent.json` 应从 `raw/` 中提取并归一化关键字段（尤其是 LLM 用量），而不是只把 `raw` 全量塞进 agent.json。

---

## 7. 安全与合规要求（必需）

- 禁止在任何输出文件中写入明文 API key、AK/SK、OAuth token。
- `agent.json` 中允许写入 `baseUrl/model/requestId/httpStatus`，以及错误信息，但必须确保不含敏感凭证。
- 若 `trace.executionTrace` 中可能出现敏感信息，必须进行脱敏后写入。
