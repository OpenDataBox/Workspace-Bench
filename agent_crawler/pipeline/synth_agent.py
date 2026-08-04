"""
synth_agent.py — 协同文件生成模块

职责:
  - 读取已下载的源文件，用 LLM 规划应该生成哪些协同文件
  - 调用 Claude Agent SDK（底层走 MiniMax）执行代码生成协同文件

两步架构:
  Step 6a: MiniMax（OpenAI 兼容）生成协同文件规划 JSON（便宜、快速）
  Step 6b: Claude Agent SDK 执行代码生成文件（稳定、有完整工具链）

依赖:
  - claude-agent-sdk (pip install claude-agent-sdk)
  - Claude Code CLI 已安装且 ~/.claude/settings.json 已配置 MiniMax 端点
"""

import os
import json
import time
import random
import asyncio
from typing import List, Dict, Optional

from openai import OpenAI

from pipeline.config import LLM_CONFIG, MAX_LLM_RETRIES,MAX_SYNTH_FILES_PER_SOURCE,SYNTH_EXECUTION_TIMEOUT,SYNTH_MAX_TURNS
from pipeline.utils import log_event
from pipeline.validator import extract_text_preview


# ================= 规划阶段 LLM 客户端（复用 MiniMax） =================

_planner_client = OpenAI(
    api_key=LLM_CONFIG["api_key"],
    base_url=LLM_CONFIG["base_url"],
)


# ================= Step 6a: 协同文件规划 =================

def build_synth_plan_prompt(
    source_filename: str,
    source_ext: str,
    source_preview: str,
    parent_hint: str,
    leaf_name: str,
) -> str:
    """
    构造协同文件规划 prompt。

    让 LLM 根据源文件内容，规划应该生成哪些协同文件。
    """
    context = f"{parent_hint}/{leaf_name}" if parent_hint else leaf_name

    return f"""你是一个企业数据协同架构师。你的任务是分析一份已有的文档，基于数据协同理论，规划与之配套的协同文件。

## 源文件信息
- 文件名: {source_filename}
- 格式: {source_ext}
- 所在目录上下文: {context}

## 源文件内容预览（前 1500 字）
---
{source_preview[:1500]}
---

## 你的任务
请深入分析这份文档的主题、内容结构和业务场景，然后规划 1~{MAX_SYNTH_FILES_PER_SOURCE} 个与之有真实工作协同关系的文件。

## 协同关系定义

你必须从以下协同关系分类中选择合适的类型。每个生成的文件都必须明确标注属于哪种协同类型。

### 一、显式协同 (Explicit Collaboration)
指文件之间有直接、可见的数据关联关系：

1. **引用 (Referencing)**
   一个文件指向另一个文件的内容作为数据来源或依据。
   - Word 报告引用 Excel 表格中的销售数据作为论据
   - PPT 汇报中引用 PDF 研究报告的结论
   - 需求文档引用竞品分析报告的调研结果

2. **嵌入 (Embedding)**
   一个文件的核心内容被完整包含或内嵌到另一个文件中。
   - PPT 中嵌入 Excel 图表用于可视化展示
   - 报告中嵌入数据表格的关键统计摘要
   - 项目方案中嵌入甘特图或架构图

3. **链接 (Linking)**
   不同文件之间通过逻辑关系相互连接，在工作流中形成上下游。
   - 项目任务表链接到需求文档和设计稿
   - 测试用例链接到功能需求规格书
   - 周报链接到各子项目的进展文档

4. **聚合 (Aggregation)**
   将多源数据或分散信息汇集整合为统一视图。
   - BI 仪表盘汇总各业务线的数据报表
   - 项目总结报告聚合多个阶段的里程碑文档
   - 年度复盘 PPT 聚合各季度 OKR 完成情况

5. **转换 (Transformation)**
   数据在流转中改变形态、格式或抽象层次。
   - 原始数据表 → 数据分析报告（结构化→叙事化）
   - 会议录音纪要 → 待办事项清单（文本→结构化任务）
   - 用户调研问卷 → 用户画像文档（原始数据→洞察总结）

### 二、隐式协同 (Implicit Collaboration)
指基于内容语义、工作上下文和业务逻辑自动推断的关联关系：

1. **模态转换关联**
   数据在不同模态（文本、表格、演示、图表）之间转换并保持语义连贯。
   - 文字版竞品分析 → 结构化对比矩阵表格 → 可视化汇报幻灯片
   - 需求描述文档 → 功能优先级评估表 → 迭代排期甘特图
   - 用户访谈记录 → 需求归类表 → 产品路线图演示

2. **语义关联**
   基于内容主题和概念的语义相似性建立关联。
   - 产品 PRD 语义关联到技术方案文档（同一功能的不同视角）
   - 市场分析报告语义关联到营销策略方案（分析→行动）
   - 培训材料语义关联到考核评估表（学习→验证）

3. **上下文关联**
   根据时间、项目阶段、参与角色等上下文信息建立关联。
   - 同一项目不同阶段的文档（立项报告 → PRD → 测试报告 → 上线总结）
   - 同一会议产出的不同文档（会议纪要 + 决议跟踪表 + 会后邮件）
   - 同一业务流程的不同环节文档（申请表 → 审批单 → 执行报告）

4. **派生/血缘关联**
   追踪数据的来源、处理过程和下游影响，形成因果链。
   - 源数据表 → 清洗后数据 → 分析报告 → 决策建议书
   - 市场调研原始数据 → 统计分析结果 → 策略建议 PPT
   - 财务明细表 → 部门预算汇总 → 年度财务报告

5. **行为关联**
   基于企业实际工作流和岗位协作模式推断的关联。
   - 产品经理写 PRD 后，通常需要排期表给开发、汇报 PPT 给管理层
   - 数据分析师出报表后，通常需要可视化看板给业务方、摘要邮件给领导
   - 项目经理做完里程碑评审后，通常需要风险跟踪表和下阶段计划

## 协同场景示例

| 源文件 | 协同类型 | 生成文件 | 关系说明 |
|-------|---------|---------|---------|
| 竞品分析报告.pdf | 模态转换 | 竞品功能对比矩阵.xlsx | 叙事化分析 → 结构化数据表 |
| 竞品分析报告.pdf | 嵌入+转换 | 竞品分析汇报.pptx | 报告结论嵌入演示，面向管理层 |
| 产品需求文档.docx | 行为关联 | 功能迭代排期表.xlsx | PM 写完 PRD 后排期是标准下游动作 |
| 产品需求文档.docx | 派生关联 | 需求评审会议纪要.docx | PRD 派生出评审讨论记录 |
| Q3销售数据.xlsx | 转换 | Q3销售分析报告.pdf | 原始数据 → 洞察型叙事报告 |
| Q3销售数据.xlsx | 聚合+嵌入 | 季度业务回顾.pptx | 数据聚合为管理层可视化汇报 |
| 项目立项报告.pdf | 上下文关联 | 项目风险评估表.xlsx | 立项阶段的配套风险管理文档 |
| 会议纪要.docx | 转换 | 会议待办事项跟踪表.xlsx | 纪要中的 action item 结构化为任务表 |
| 年度预算明细.xlsx | 派生关联 | 预算执行偏差分析报告.pdf | 预算数据的下游分析产物 |

## 输出要求

1. 每个协同文件必须标注具体的协同类型（从上述分类中选择，可以是组合类型如"嵌入+转换"）
2. 文件格式必须在 pdf/xlsx/pptx/docx 中选择
3. 文件名必须是中文，符合真实企业文档命名习惯，且与源文件在命名风格上保持一致
4. 不要生成与源文件相同格式和相同内容的重复文件
5. 每个文件必须提供**具体且详细**的内容大纲（包含要写入的章节标题/表格列名/幻灯片页面结构等），而不是空泛描述
6. 内容大纲必须基于源文件的**实际内容**来填充，体现真实的数据流转关系，不要编造与源文件无关的信息
7. 优先选择不同的协同类型，使生成的文件覆盖多种协同关系，而不是全部都是同一种类型

## 输出格式

必须严格输出以下 JSON 格式，不要加任何解释或 markdown 包裹:
{{
  "source_summary": "源文件的一句话概要",
  "synth_files": [
    {{
      "filename": "文件名.后缀",
      "ext": "xlsx/pptx/pdf/docx 之一",
      "collab_type": "协同类型（如: 模态转换 / 引用+嵌入 / 派生关联 等）",
      "collab_category": "explicit 或 implicit",
      "relationship": "与源文件的具体协同关系说明（一句话）",
      "outline": "详细内容大纲，包含要写入的章节标题/表格列名与示例行/幻灯片每页结构等"
    }}
  ]
}}"""


def plan_synth_files(
    source_path: str,
    source_filename: str,
    parent_hint: str,
    leaf_name: str,
    log_file: str,
) -> Optional[Dict]:
    """
    Step 6a: 调用 MiniMax 生成协同文件规划。

    Returns:
        规划 dict，失败返回 None
    """
    source_ext = os.path.splitext(source_filename)[1].lower()

    # 提取源文件内容预览
    preview = extract_text_preview(source_path)
    if not preview or len(preview.strip()) < 30:
        log_event("    [Step 6a] 源文件文本预览过短，跳过协同生成。", log_file)
        return None

    prompt = build_synth_plan_prompt(
        source_filename=source_filename,
        source_ext=source_ext,
        source_preview=preview,
        parent_hint=parent_hint,
        leaf_name=leaf_name,
    )

    for attempt in range(MAX_LLM_RETRIES + 1):
        try:
            resp = _planner_client.chat.completions.create(
                model=LLM_CONFIG["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
            raw = resp.choices[0].message.content.strip()
            plan = _parse_synth_plan(raw)

            if plan and plan.get("synth_files"):
                log_event(
                    f"    [Step 6a] 规划成功: {len(plan['synth_files'])} 个协同文件 "
                    f"(尝试 {attempt + 1})",
                    log_file,
                )
                return plan
            else:
                log_event(
                    f"    [Step 6a] 规划为空 (尝试 {attempt + 1}): {raw[:120]}",
                    log_file,
                )
        except Exception as e:
            log_event(
                f"    [Step 6a] 规划调用异常 (尝试 {attempt + 1}): {str(e)[:100]}",
                log_file,
            )

        if attempt < MAX_LLM_RETRIES:
            time.sleep(random.uniform(1.5, 3.0))

    return None


def _parse_synth_plan(raw_text: str) -> Optional[Dict]:
    """解析规划 JSON，容错处理。"""
    import re

    cleaned = raw_text.strip()

    # 去 markdown 包裹
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    else:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start:end + 1]

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    synth_files = data.get("synth_files", [])
    if not isinstance(synth_files, list):
        return None

    # 验证并截断
    VALID_CATEGORIES = {"explicit", "implicit"}
    valid_files = []
    for item in synth_files[:MAX_SYNTH_FILES_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        if not (item.get("filename") and item.get("ext") and item.get("outline")):
            continue

        # 补全可能缺失的协同字段（容错）
        if not item.get("collab_type"):
            item["collab_type"] = "未标注"
        if not item.get("collab_category"):
            # 尝试根据 collab_type 推断
            implicit_keywords = {"模态转换", "语义关联", "上下文关联", "派生", "血缘", "行为关联"}
            if any(kw in item["collab_type"] for kw in implicit_keywords):
                item["collab_category"] = "implicit"
            else:
                item["collab_category"] = "explicit"
        else:
            item["collab_category"] = item["collab_category"].lower().strip()
            if item["collab_category"] not in VALID_CATEGORIES:
                item["collab_category"] = "explicit"

        valid_files.append(item)

    if not valid_files:
        return None

    return {
        "source_summary": data.get("source_summary", ""),
        "synth_files": valid_files,
    }


# ================= Step 6b: Claude Agent SDK 执行 =================

def _build_execution_prompt(
    source_path: str,
    source_filename: str,
    target_dir: str,
    plan: Dict,
) -> str:
    """
    构造给 Claude Agent SDK 的执行指令。

    告诉 Agent：读取源文件 → 根据规划用 Python 库生成协同文件。
    """
    files_desc = json.dumps(plan["synth_files"], ensure_ascii=False, indent=2)

    return f"""你是一个文档生成专家。请根据以下规划，生成与源文件有真实协同关系的文件。

## 源文件
- 路径: {source_path}
- 文件名: {source_filename}
- 概要: {plan.get('source_summary', '见源文件')}

## 要生成的协同文件
{files_desc}

## 输出目录
所有生成的文件必须保存到: {target_dir}

## 协同关系生成原则

每个文件都标注了 collab_type（协同类型），你必须理解这些关系并在生成内容时体现：

- **引用 (Referencing)**: 生成的文件中应出现对源文件的明确引用（如"根据《{source_filename}》中的数据..."）
- **嵌入 (Embedding)**: 将源文件的关键数据（表格、图表、结论）直接嵌入到生成文件中
- **链接 (Linking)**: 在生成文件中标注与源文件的逻辑上下游关系
- **聚合 (Aggregation)**: 将源文件中的分散信息汇总到统一的结构化视图
- **转换 (Transformation)**: 将源文件的数据形态做转换（如叙事→表格、数据→图表描述）
- **模态转换关联**: 保持语义一致但改变呈现模态（如文字报告→数据矩阵→演示幻灯片）
- **语义关联**: 从源文件的主题出发，生成不同视角但语义相关的文档
- **上下文关联**: 基于同一业务场景/项目阶段生成配套文档
- **派生/血缘关联**: 体现数据的因果链和处理过程
- **行为关联**: 模拟真实工作流中"做完 A 接着做 B"的岗位行为

## 执行要求

1. 首先读取源文件内容，理解其实际数据和主题
2. 根据每个协同文件的 outline 和 collab_type，用 Python 代码生成内容
3. **关键**: 生成的内容必须与源文件有真实的数据流转关系，不是独立的无关文件
4. 生成规则:
   - xlsx: 使用 openpyxl，创建有表头、数据行、适当列宽和格式的工作表
   - pptx: 使用 python-pptx，创建多页幻灯片，包含标题页和内容页，文字不要溢出
   - pdf:  使用 reportlab，创建有标题、段落、表格的文档，注意中文字体
   - docx: 使用 python-docx，创建有标题、正文、列表的文档
5. 文件内容必须基于源文件的实际信息，不要凭空编造数据
6. 如果某个库没有安装，先用 pip install 安装
7. 每个文件生成后，确认文件存在且大小 > 1KB
8. 如果某个文件生成失败，跳过它继续生成下一个，不要中断

生成完成后，列出所有成功生成的文件名。"""


# ================= SDK Monkey-patch =================

_sdk_patched = False

def _patch_sdk_message_parser():
    """
    修复 claude-code-sdk / claude-agent-sdk 的已知 bug:
    CLI 会发出 rate_limit_event 等未知消息类型，
    SDK 的 parse_message 遇到未知类型会 raise MessageParseError 导致整个流中断。

    此 patch 用 sys.modules 全局扫描，确保所有模块中的 parse_message 引用都被替换。
    只 patch 一次，重复调用安全。

    参考: https://github.com/anthropics/claude-agent-sdk-python/issues/583
    """
    global _sdk_patched
    if _sdk_patched:
        return
    _sdk_patched = True

    import sys

    # Step 1: 找到 parse_message 原始函数，并创建安全版本
    original_parse = None
    parser_module = None

    for pkg in ("claude_code_sdk", "claude_agent_sdk"):
        mod_name = f"{pkg}._internal.message_parser"
        if mod_name in sys.modules:
            parser_module = sys.modules[mod_name]
            if hasattr(parser_module, "parse_message"):
                original_parse = parser_module.parse_message
                break
        else:
            try:
                parser_module = __import__(mod_name, fromlist=["parse_message"])
                if hasattr(parser_module, "parse_message"):
                    original_parse = parser_module.parse_message
                    break
            except ImportError:
                continue

    if original_parse is None:
        return  # SDK 没装或结构变了，放弃 patch

    def _safe_parse(data, _original=original_parse):
        try:
            return _original(data)
        except Exception as e:
            if "Unknown message type" in str(e):
                return None
            raise

    # Step 2: 全局扫描 sys.modules，替换所有引用了 parse_message 的模块
    # 这是关键——from .message_parser import parse_message 会在目标模块里
    # 创建本地绑定，只 patch 源模块不够
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if "claude_code_sdk" not in mod_name and "claude_agent_sdk" not in mod_name:
            continue
        if hasattr(mod, "parse_message") and mod.parse_message is original_parse:
            mod.parse_message = _safe_parse


async def _execute_synth_async(
    source_path: str,
    source_filename: str,
    target_dir: str,
    plan: Dict,
    log_file: str,
) -> List[str]:
    """
    异步执行协同文件生成。

    使用 Claude Agent SDK 的 query()，底层通过 settings.json 走 MiniMax。

    Returns:
        成功生成的文件名列表
    """
    # 延迟导入，避免未安装时影响其他模块
    try:
        from claude_code_sdk import query, ClaudeCodeOptions, AssistantMessage, TextBlock
    except ImportError:
        log_event(
            "    [Step 6b] claude-code-sdk 未安装，跳过协同生成。"
            "请运行: pip install claude-code-sdk",
            log_file,
        )
        return []

    # ---- Monkey-patch: 修复 SDK 不识别 rate_limit_event 导致崩溃的已知 bug ----
    # https://github.com/anthropics/claude-agent-sdk-python/issues/583
    _patch_sdk_message_parser()

    prompt = _build_execution_prompt(source_path, source_filename, target_dir, plan)

    generated_files = []

    try:
        options = ClaudeCodeOptions(
            # system_prompt 补充约束
            system_prompt=(
                "你是一个文件生成 Agent。你只负责读取源文件、编写 Python 代码、"
                "执行代码来生成指定的文档文件。不要进行任何网络请求。"
                "如果需要安装 Python 包，使用 pip install --break-system-packages。"
                "工作完成后简要报告结果。"
            ),
            # 只允许必要的工具
            allowed_tools=["Read", "Write", "Bash", "Edit"],
            # 自动批准文件写入
            permission_mode="acceptEdits",
            # 工作目录设为目标文件夹
            cwd=target_dir,
            # 限制 agent loop 轮数
            max_turns=SYNTH_MAX_TURNS,
        )

        # 把流式消息收集封装成单个 coroutine，才能用 wait_for 做超时
        async def _run_agent():
            try:
                async for message in query(prompt=prompt, options=options):
                    if message is None:
                        continue  # monkey-patch 跳过的未知消息类型
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                log_event(
                                    f"      [Agent] {block.text.strip()[:200]}",
                                    log_file,
                                )
            except Exception as inner_err:
                err_str = str(inner_err)
                # rate_limit_event 是 SDK 已知 bug，不是真正的错误
                if "Unknown message type" in err_str:
                    log_event(
                        f"      [Agent] SDK 消息解析中断 (已知 bug)，"
                        f"检查已生成的文件...",
                        log_file,
                    )
                else:
                    raise  # 其他异常继续向上抛

        await asyncio.wait_for(_run_agent(), timeout=SYNTH_EXECUTION_TIMEOUT)

    except asyncio.TimeoutError:
        log_event(
            f"    [Step 6b] Agent 执行超时 ({SYNTH_EXECUTION_TIMEOUT}s)，终止。",
            log_file,
        )
    except Exception as e:
        log_event(
            f"    [Step 6b] Agent 执行异常: {str(e)[:150]}",
            log_file,
        )

    # 扫描目标目录，找出新生成的文件
    expected_names = {f["filename"] for f in plan["synth_files"]}
    for fname in os.listdir(target_dir):
        fpath = os.path.join(target_dir, fname)
        if (fname in expected_names
                and os.path.isfile(fpath)
                and os.path.getsize(fpath) > 1024):
            generated_files.append(fname)

    return generated_files



# ================= 对外接口（同步） =================

def generate_collaborative_files(
    source_path: str,
    source_filename: str,
    folder_path: str,
    parent_hint: str,
    leaf_name: str,
    log_file: str,
) -> List[str]:
    """
    协同文件生成入口（同步接口，供 main.py 直接调用）。

    流程:
      Step 6a: MiniMax 规划协同文件
      Step 6b: Claude Agent SDK 执行生成

    Args:
        source_path:     已替换的源文件完整路径
        source_filename: 源文件名（含后缀）
        folder_path:     目标目录（协同文件也放这里）
        parent_hint:     父目录路径提示
        leaf_name:       叶子目录名
        log_file:        日志文件路径

    Returns:
        成功生成的文件名列表，失败返回空列表
    """
    log_event(f"    [Step 6] 开始协同文件生成...", log_file)

    # --- Step 6a: 规划 ---
    plan = plan_synth_files(
        source_path=source_path,
        source_filename=source_filename,
        parent_hint=parent_hint,
        leaf_name=leaf_name,
        log_file=log_file,
    )

    if not plan:
        log_event(f"    [Step 6] 规划失败，跳过协同生成。", log_file)
        return []

    # 记录规划详情
    for i, f in enumerate(plan["synth_files"], 1):
        collab_info = f.get('collab_type', '未标注')
        category = f.get('collab_category', '')
        category_label = "显式" if category == "explicit" else "隐式"
        log_event(
            f"      [规划 {i}] {f['filename']} ({f['ext']}) "
            f"[{category_label}/{collab_info}] "
            f"- {f.get('relationship', '')}",
            log_file,
        )

    # --- Step 6b: 执行 ---
    log_event(f"    [Step 6b] 调用 Agent 执行生成...", log_file)

    try:
        generated = asyncio.run(
            _execute_synth_async(
                source_path=source_path,
                source_filename=source_filename,
                target_dir=folder_path,
                plan=plan,
                log_file=log_file,
            )
        )
    except Exception as e:
        log_event(f"    [Step 6b] asyncio.run 异常: {str(e)[:100]}", log_file)
        generated = []

    # --- 汇总 ---
    if generated:
        log_event(
            f"    [Step 6] 协同生成完成: {len(generated)} 个文件 "
            f"({', '.join(generated)})",
            log_file,
        )
    else:
        log_event(f"    [Step 6] 未成功生成协同文件。", log_file)

    return generated

