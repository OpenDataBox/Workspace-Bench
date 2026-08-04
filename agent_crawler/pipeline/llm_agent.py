"""
llm_agent.py — LLM 交互层

负责:
  - build_instruction: 根据空文件名+目录上下文构造搜索 Instruction
  - call_llm_for_urls: 调用带联网能力的 LLM 获取文件 URL
  - parse_llm_url_response: 容错解析 LLM 返回的多种格式
"""

from typing import Any, Dict, List, Set
import re
import json
import time
import random
from openai import OpenAI
import os
import requests
from pipeline.config import (
    LLM_CONFIG,
    MAX_URLS_PER_FILE, MAX_LLM_RETRIES, BRAVE_API_KEY
)
from pipeline.utils import log_event

_client = OpenAI(
    api_key=LLM_CONFIG["api_key"],
    base_url=LLM_CONFIG["base_url"],
)


def build_instruction(file_info, parent_path_hint="", leaf_dir_name=""):
    """
    根据空文件的名称和上下文，构造搜索计划 Instruction。
    让 LLM 只返回搜索计划，不直接返回 URL。
    """
    file_stem = file_info["stem"]
    file_ext = file_info["ext"].lstrip(".").lower()
    normalized_ext = normalize_extension(file_ext)

    context_parts = []
    if parent_path_hint and leaf_dir_name:
        context_parts.append(f"{parent_path_hint}/{leaf_dir_name}")
    elif parent_path_hint:
        context_parts.append(parent_path_hint)
    elif leaf_dir_name:
        context_parts.append(leaf_dir_name)

    context_text = " / ".join(context_parts) if context_parts else ""

    same_family_map = {
        "docx": ["doc", "docx"],
        "xlsx": ["xls", "xlsx"],
        "pptx": ["ppt", "pptx"],
        "pdf": ["pdf"],
    }
    family_exts = same_family_map.get(normalized_ext, [normalized_ext])

    instruction = f"""你是一个专业的文档搜索规划助手。你的任务不是返回 URL，而是生成高召回、稳定、适合后续爬虫检索的搜索计划。

目标文件信息:
- 文件名: {file_stem}.{file_ext}
- 原始扩展名: {file_ext}
- 推荐扩展名族: {", ".join(family_exts)}
- 路径上下文: {context_text or "无"}

请完成以下任务：
1. 理解文件名「{file_stem}」可能对应的主题、文档类型和内容范围；
2. 生成多个适合网页搜索/文件搜索的高召回 query；
3. query 应尽量广泛、稳定，不要过于精准，不要过早过滤；
4. 可以使用：
   - 文件名原词与变体
   - 同义词
   - 引号和非引号形式
   - filetype 风格提示
   - 主题词 / 机构词 / 场景词扩写
5. 允许同族扩展名一起搜索；
6. 严禁返回任何 URL；
7. 严禁输出 JSON 之外的任何文字。

输出格式必须严格为以下 JSON 对象：
{{
  "queries": ["...", "..."],
  "preferred_extensions": {json.dumps(family_exts, ensure_ascii=False)},
  "search_hints": {{
    "keywords": ["...", "..."],
    "exclude_domains": ["...", "..."],
    "notes": "..."
  }}
}}

要求：
- queries 至少返回 3 条
- queries 必须偏高召回，不要过窄
- notes 中简要说明扩写策略
- 只输出 JSON 对象，不要加 markdown，不要加解释
"""
    return instruction


def _extract_json_object(raw_text: str) -> str:
    raw_text = raw_text.strip()

    # 去掉 ```json ... ``` 包裹
    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw_text, re.S)
    if fenced_match:
        return fenced_match.group(1).strip()

    # 尝试提取首个 JSON 对象
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw_text[start:end + 1].strip()

    return raw_text


def parse_llm_search_plan_response(raw_text: str, log_file=None) -> Dict[str, Any]:
    """
    解析 LLM 返回的搜索计划 JSON。
    """
    cleaned = _extract_json_object(raw_text)

    try:
        data = json.loads(cleaned)
    except Exception as e:
        raise ValueError(f"搜索计划 JSON 解析失败: {e}")

    if not isinstance(data, dict):
        raise ValueError("搜索计划必须是 JSON 对象")

    queries = data.get("queries", [])
    preferred_extensions = data.get("preferred_extensions", [])
    search_hints = data.get("search_hints", {})

    if not isinstance(queries, list):
        raise ValueError("queries 必须是数组")
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not queries:
        raise ValueError("queries 至少需要一个非空字符串")

    if not isinstance(preferred_extensions, list):
        preferred_extensions = []
    preferred_extensions = [
        str(ext).strip().lower().lstrip(".")
        for ext in preferred_extensions
        if str(ext).strip()
    ]

    if not isinstance(search_hints, dict):
        search_hints = {}

    keywords = search_hints.get("keywords", [])
    exclude_domains = search_hints.get("exclude_domains", [])
    notes = search_hints.get("notes", "")

    if not isinstance(keywords, list):
        keywords = []
    if not isinstance(exclude_domains, list):
        exclude_domains = []

    normalized = {
        "queries": queries,
        "preferred_extensions": preferred_extensions,
        "search_hints": {
            "keywords": [str(x).strip() for x in keywords if str(x).strip()],
            "exclude_domains": [str(x).strip() for x in exclude_domains if str(x).strip()],
            "notes": str(notes).strip(),
        },
    }
    return normalized


def call_llm_for_search_plan(instruction, log_file):
    """
    调用 LLM 获取搜索计划，而不是直接获取 URL。
    """
    for attempt in range(MAX_LLM_RETRIES + 1):
        try:
            api_params = {
                "model": LLM_CONFIG["model"],
                "messages": [{"role": "user", "content": instruction}],
                "temperature": 0.3,
            }

            resp = _client.chat.completions.create(**api_params)
            raw_text = resp.choices[0].message.content.strip()

            search_plan = parse_llm_search_plan_response(raw_text, log_file)
            if search_plan and search_plan.get("queries"):
                log_event(
                    f"      [LLM] 获取 search plan 成功，queries={len(search_plan['queries'])} (尝试 {attempt+1})",
                    log_file,
                )
                return search_plan
            else:
                log_event(
                    f"      [LLM] search plan 为空 (尝试 {attempt+1}): {raw_text[:150]}",
                    log_file,
                )
        except Exception as e:
            log_event(
                f"      [LLM] search plan 调用异常 (尝试 {attempt+1}): {str(e)[:120]}", log_file)

        if attempt < MAX_LLM_RETRIES:
            time.sleep(random.uniform(2.0, 4.0))

    return {
        "queries": [],
        "preferred_extensions": [],
        "search_hints": {
            "keywords": [],
            "exclude_domains": [],
            "notes": "",
        },
    }


def normalize_extension(ext: str) -> str:
    ext = (ext or "").strip().lower().lstrip(".")
    family_map = {
        "doc": "docx",
        "docx": "docx",
        "xls": "xlsx",
        "xlsx": "xlsx",
        "ppt": "pptx",
        "pptx": "pptx",
        "pdf": "pdf",
    }
    return family_map.get(ext, ext)


def _get_extension_family(ext: str) -> List[str]:
    normalized = normalize_extension(ext)
    family_map = {
        "docx": ["doc", "docx"],
        "xlsx": ["xls", "xlsx"],
        "pptx": ["ppt", "pptx"],
        "pdf": ["pdf"],
    }
    return family_map.get(normalized, [normalized])


def _build_fallback_queries(filename: str, ext: str, context: str = "") -> List[str]:
    """
    构造兜底 Brave Search 查询（当 LLM 未提供 queries 时使用）。
    """
    stem = (filename or "").strip()
    context = (context or "").strip()
    ext_family = _get_extension_family(ext)

    quoted = f"\"{stem}\"" if stem else ""
    ext_hint = " OR ".join([f"filetype:{x}" for x in ext_family])

    queries = []

    if quoted:
        queries.append(f"{quoted} {ext_hint}")

    if stem:
        queries.append(f"{stem} {ext_hint}")

    if stem and context:
        queries.append(f"{stem} {context} {ext_hint}")

    if quoted and context:
        queries.append(f"{quoted} {context}")

    if stem:
        queries.append(
            f"{stem} document OR report OR presentation OR spreadsheet {ext_hint}"
        )

    seen = set()
    deduped = []
    for q in queries:
        q = " ".join(q.split()).strip()
        if q and q not in seen:
            seen.add(q)
            deduped.append(q)

    return deduped


# Brave Search 单次请求最大重试次数（应对 SSL 偶发断连）
_BRAVE_REQUEST_MAX_RETRIES = 3

# 每个 query 最多翻几页（每页 20 条）
_BRAVE_MAX_PAGES_PER_QUERY = 3


def _brave_search_one_page(api_url, headers, params):
    """
    带重试的单次 Brave Search 请求。

    返回 (data_dict, error_str)。成功时 error_str 为 None。
    """
    for retry in range(_BRAVE_REQUEST_MAX_RETRIES):
        try:
            resp = requests.get(api_url, headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json(), None
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            # SSL/连接错误，可重试
            if retry < _BRAVE_REQUEST_MAX_RETRIES - 1:
                time.sleep(1.0 * (retry + 1))
                continue
            return None, f"连接失败 (重试{_BRAVE_REQUEST_MAX_RETRIES}次): {str(e)[:80]}"
        except requests.exceptions.HTTPError as e:
            return None, f"HTTP {e.response.status_code}"
        except Exception as e:
            return None, f"请求异常: {str(e)[:80]}"
    return None, "未知错误"


def search_downloadable_urls(filename, ext, context, max_results=20, llm_queries=None):
    """
    使用 Brave Search API 搜索候选 URL。

    改进点:
      - 优先使用 LLM 生成的 queries，再补充兜底 queries
      - 每个 query 支持翻页（最多 _BRAVE_MAX_PAGES_PER_QUERY 页）
      - 单次请求带重试（应对 SSL 偶发错误）

    Args:
        filename:     文件名（不含后缀）
        ext:          文件后缀
        context:      目录上下文
        max_results:  最大返回 URL 数
        llm_queries:  LLM 生成的搜索 queries 列表（优先使用）

    返回格式:
    {
        "filename": ..., "ext": ..., "context": ...,
        "results": [ {"url": "...", "title": "...", "source": "brave_search", "reason": "..."} ]
    }
    """
    normalized_ext = normalize_extension(ext)
    brave_api_key = BRAVE_API_KEY

    payload = {
        "filename": filename,
        "ext": normalized_ext,
        "context": context,
        "results": []
    }

    if not brave_api_key:
        return payload

    # === 合并查询: LLM queries 优先 + 兜底 queries 补充 ===
    all_queries = []
    seen_q = set()

    # 先加 LLM 生成的 queries
    if llm_queries:
        for q in llm_queries:
            q = q.strip() if isinstance(q, str) else ""
            if q and q not in seen_q:
                seen_q.add(q)
                all_queries.append(q)

    # 再加兜底 queries（去重）
    for q in _build_fallback_queries(filename, normalized_ext, context):
        if q not in seen_q:
            seen_q.add(q)
            all_queries.append(q)

    if not all_queries:
        return payload

    api_url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": brave_api_key,
    }

    collected: List[Dict] = []
    seen_urls: Set[str] = set()

    for query in all_queries:
        if len(collected) >= max_results:
            break

        # === 每个 query 翻页 ===
        for page in range(_BRAVE_MAX_PAGES_PER_QUERY):
            if len(collected) >= max_results:
                break

            offset = page * 20

            params = {
                "q": query,
                "count": 20,
                "offset": offset,
                "country": "ALL",
                "search_lang": "en",
                "ui_lang": "en-US",
            }

            data, err = _brave_search_one_page(api_url, headers, params)
            if data is None:
                break  # 这个 query 请求失败，换下一个 query

            web_results = data.get("web", {}).get("results", [])
            if not isinstance(web_results, list) or not web_results:
                break  # 没有更多结果，换下一个 query

            for item in web_results:
                result_url = (item.get("url") or "").strip()
                title = (item.get("title") or "").strip()
                description = (item.get("description") or "").strip()

                if not result_url or not result_url.startswith("http"):
                    continue

                # 去 fragment 做去重
                canonical = result_url.split("#")[0]
                if canonical in seen_urls:
                    continue
                seen_urls.add(canonical)

                collected.append({
                    "url": canonical,
                    "title": title or filename,
                    "source": "brave_search",
                    "reason": f"query: {query}; {description[:160]}".strip(),
                })

                if len(collected) >= max_results:
                    break

            # 检查是否还有下一页
            more = data.get("query", {}).get("more_results_available", False)
            if not more:
                break

            time.sleep(0.15)

        time.sleep(0.2)

    payload["results"] = collected[:max_results]
    return payload


def search_downloadable_url_candidates(
    filename, ext, context, max_results=20, llm_queries=None,
):
    """
    给 main.py 的接口：返回 [{"url": "...", "title": "..."}]

    Args:
        llm_queries: LLM 生成的搜索 queries（优先使用）
    """
    data = search_downloadable_urls(
        filename=filename,
        ext=ext,
        context=context,
        max_results=max_results,
        llm_queries=llm_queries,
    )

    return [
        {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
        }
        for item in data.get("results", [])
    ]