"""
main.py — 主流程入口（8 线程并发 + 断点续传）

并发策略:
  - 目录级别: 串行遍历
  - 文件级别: 8 线程并发处理同一目录下的空文件
  - 共享状态: ThreadSafeSet + threading.Lock 保护

用法:
  python -m pipeline.main           # 8 线程并发运行（自动续传）
  python -m pipeline.main --reset   # 清除进度，从头开始
"""

import os
import sys
import json
import time
import random
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from pipeline.config import (
    ROOT_DIR, LOG_DIR, MD_FILE_PATH,
    TARGET_FILE_TYPES, EMPTY_FILE_THRESHOLD,
)
from pipeline.utils import (
    log_event, setup_env,
    parse_leaf_directories, get_parent_path_hint,
    scan_empty_files,
    ThreadSafeSet,
)
from pipeline.llm_agent import (
    build_instruction,
    call_llm_for_search_plan,
    search_downloadable_url_candidates,
)
from pipeline.downloader import (
    pre_check_url, download_file, post_download_verify,
)
from pipeline.validator import (
    validate_content_relevance,
)
from pipeline.synth_agent import generate_collaborative_files


# ================= 并发配置 =================

NUM_WORKERS = 8

# ================= 线程锁 =================

_progress_lock = threading.Lock()


# ================= 进度管理 =================

PROGRESS_FILE = os.path.join(LOG_DIR, "progress.json")


def _make_file_key(folder_path, filename):
    return f"{folder_path}::{filename}"


def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return _empty_progress()

    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        progress = _empty_progress()
        progress["completed"] = data.get("completed", {})
        progress["seen_urls"] = ThreadSafeSet(data.get("seen_urls", []))
        progress["seen_hashes"] = ThreadSafeSet(data.get("seen_hashes", []))
        progress["stats"] = {**progress["stats"], **data.get("stats", {})}
        return progress

    except (json.JSONDecodeError, TypeError, KeyError) as e:
        print(f"[警告] 进度文件损坏 ({e})，将从头开始。")
        return _empty_progress()


def save_progress(progress):
    """调用方必须已持有 _progress_lock。"""
    setup_env(LOG_DIR)

    data = {
        "completed": dict(progress["completed"]),
        "seen_urls": list(progress["seen_urls"]),
        "seen_hashes": list(progress["seen_hashes"]),
        "stats": dict(progress["stats"]),
    }

    tmp_path = PROGRESS_FILE + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
        os.rename(tmp_path, PROGRESS_FILE)

    except Exception as e:
        print(f"[警告] 进度保存失败: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _empty_progress():
    return {
        "completed": {},
        "seen_urls": ThreadSafeSet(),
        "seen_hashes": ThreadSafeSet(),
        "stats": {
            'total_empty_files': 0,
            'total_llm_search_calls': 0,
            'total_urls_returned': 0,
            'total_precheck_passed': 0,
            'total_precheck_rejected': 0,
            'total_download_ok': 0,
            'total_download_fail': 0,
            'total_tech_verify_rejected': 0,
            'total_llm_verify_rejected': 0,
            'total_replaced': 0,
            'total_skipped': 0,
            'total_synth_attempted': 0,
            'total_synth_generated': 0,
        },
    }


def _update_stats(stats, key, delta=1):
    stats[key] = stats.get(key, 0) + delta


def clean_temp_files(folder_path):
    if not os.path.isdir(folder_path):
        return
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isdir(file_path):
            continue
        stem = os.path.splitext(filename)[0]
        if stem.startswith("doc_") and any(c.isdigit() for c in stem):
            try:
                size = os.path.getsize(file_path)
                if size > EMPTY_FILE_THRESHOLD:
                    os.remove(file_path)
                    print(f"  [清理] 删除残留临时文件: {filename}")
            except OSError:
                pass


# ================= 单文件处理（线程工作函数） =================

def _process_single_file(
    file_info,
    file_idx,
    total_files,
    folder_path,
    parent_hint,
    leaf_name,
    log_file,
    seen_urls,
    seen_hashes,
    stats,
    completed,
    progress,
):
    original_path = file_info["path"]
    original_filename = file_info["filename"]
    original_stem = file_info["stem"]
    original_ext = file_info["ext"]
    file_key = _make_file_key(folder_path, original_filename)

    log_event(f"", log_file)
    log_event(
        f"  [{file_idx+1}/{total_files}] "
        f"处理: {original_filename} ({file_info['size']}B)",
        log_file,
    )

    # --- Step 1: 构造 Instruction ---
    instruction = build_instruction(file_info, parent_hint, leaf_name)
    log_event(f"    [Step 1] Instruction 已构造", log_file)

    # --- Step 2: LLM 生成 Search Plan ---
    log_event(f"    [Step 2] 调用 LLM 生成 search plan...", log_file)
    with _progress_lock:
        _update_stats(stats, 'total_llm_search_calls')

    search_plan = call_llm_for_search_plan(instruction, log_file)
    queries = search_plan.get("queries", []) if isinstance(
        search_plan, dict) else []

    if not queries:
        log_event(
            f"    [Step 2] LLM 未返回有效 search plan，跳过此文件。", log_file)
        with _progress_lock:
            _update_stats(stats, 'total_skipped')
            completed[file_key] = "skipped"
            save_progress(progress)
        return

    log_event(f"    [Step 2] 获得 {len(queries)} 条查询", log_file)
    for idx, query in enumerate(queries[:5], 1):
        log_event(f"      [Query {idx}] {query}", log_file)

    # --- Step 3: Brave Search 获取 URL ---
    context_str = f"{parent_hint}/{leaf_name}" if parent_hint else leaf_name
    url_candidates = search_downloadable_url_candidates(
        filename=file_info["stem"],
        ext=file_info["ext"],
        context=context_str,
        max_results=20,
        llm_queries=queries,
    )

    with _progress_lock:
        _update_stats(stats, 'total_urls_returned', len(url_candidates))

    if not url_candidates:
        log_event(
            f"    [Step 3] Brave Search 未返回 URL，跳过此文件。", log_file)
        with _progress_lock:
            _update_stats(stats, 'total_skipped')
            completed[file_key] = "skipped"
            save_progress(progress)
        return

    log_event(f"    [Step 3] 获得 {len(url_candidates)} 个候选", log_file)

    # --- Step 4: 逐 URL 尝试 ---
    replaced = False
    for url_idx, item in enumerate(url_candidates):
        if replaced:
            break
        url = item.get("url", "").strip()
        title = item.get("title", "")

        if not url or not url.startswith("http"):
            continue

        log_event(
            f"    [{url_idx+1}/{len(url_candidates)}] {title or url[:60]}",
            log_file,
        )

        # === HEAD 预检 ===
        passed, reason, final_url = pre_check_url(
            url, seen_urls, log_file)
        if not passed:
            log_event(f"      [预检拒绝] {reason}", log_file)
            with _progress_lock:
                _update_stats(stats, 'total_precheck_rejected')
            time.sleep(0.3)
            continue

        with _progress_lock:
            _update_stats(stats, 'total_precheck_passed')
        if final_url != url:
            log_event(f"      [重定向] -> {final_url}", log_file)

        # === 下载 ===
        success, info = download_file(
            final_url, folder_path, file_idx + 1, log_file
        )
        if not success:
            with _progress_lock:
                _update_stats(stats, 'total_download_fail')
            log_event(f"      [下载失败] {info}", log_file)
            time.sleep(0.5)
            continue

        with _progress_lock:
            _update_stats(stats, 'total_download_ok')
        downloaded_path = os.path.join(folder_path, info)
        log_event(f"      [下载成功] {info}", log_file)

        # === 技术验证 (magic bytes + 哈希) ===
        tech_ok, tech_reason, verified_path = post_download_verify(
            downloaded_path, original_ext, seen_hashes, log_file
        )
        if not tech_ok:
            with _progress_lock:
                _update_stats(stats, 'total_tech_verify_rejected')
            log_event(f"      [技术验证拒绝] {tech_reason}", log_file)
            time.sleep(0.3)
            continue

        # === LLM 后验证（内容与标题相关性）===
        is_relevant, relevance_reason = validate_content_relevance(
            verified_path, original_stem, log_file
        )
        if not is_relevant:
            with _progress_lock:
                _update_stats(stats, 'total_llm_verify_rejected')
            log_event(f"      [内容不相关] {relevance_reason}", log_file)
            try:
                os.remove(verified_path)
            except OSError:
                pass
            time.sleep(0.3)
            continue

        # === Step 5: 替换空文件 ===
        try:
            if os.path.exists(original_path):
                os.remove(original_path)

            final_dest = os.path.join(folder_path, original_filename)

            if os.path.exists(final_dest):
                base, ext_part = os.path.splitext(original_filename)
                counter = 1
                while os.path.exists(final_dest):
                    final_dest = os.path.join(
                        folder_path, f"{base}_{counter}{ext_part}"
                    )
                    counter += 1

            shutil.move(verified_path, final_dest)

            replaced = True
            with _progress_lock:
                _update_stats(stats, 'total_replaced')
                completed[file_key] = "replaced"
            log_event(
                f"    【替换成功】{original_filename}",
                log_file,
            )

            # === Step 6: 协同文件生成 ===
            with _progress_lock:
                _update_stats(stats, 'total_synth_attempted')
            try:
                synth_results = generate_collaborative_files(
                    source_path=final_dest,
                    source_filename=original_filename,
                    folder_path=folder_path,
                    parent_hint=parent_hint,
                    leaf_name=leaf_name,
                    log_file=log_file,
                )
                with _progress_lock:
                    _update_stats(stats, 'total_synth_generated', len(synth_results))
                    if synth_results:
                        completed[file_key] = f"replaced+synth_{len(synth_results)}"

            except Exception as synth_err:
                log_event(
                    f"    [Step 6] 协同生成异常（不影响主流程）: "
                    f"{str(synth_err)[:100]}",
                    log_file,
                )

        except Exception as e:
            log_event(f"    【替换失败】{str(e)[:80]}", log_file)

        time.sleep(random.uniform(1.0, 2.0))

    if not replaced:
        with _progress_lock:
            _update_stats(stats, 'total_skipped')
            completed[file_key] = "skipped"
        log_event(f"    【未找到替代】{original_filename}", log_file)

    # ★ 每处理完一个文件就保存进度
    with _progress_lock:
        save_progress(progress)


# ================= 主流程 =================

def run_task():
    """主流程入口（8 线程并发 + 断点续传）"""

    if "--reset" in sys.argv:
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
            print("已清除进度文件，将从头开始。\n")

    # ====== 初始化 ======
    leaf_dirs = parse_leaf_directories(MD_FILE_PATH, ROOT_DIR)
    print(f"共解析出 {len(leaf_dirs)} 个叶子目录。")
    print(f"目标文件类型: {TARGET_FILE_TYPES}")
    print(f"并发线程数: {NUM_WORKERS}")

    progress = load_progress()
    completed = progress["completed"]
    seen_urls = progress["seen_urls"]
    seen_hashes = progress["seen_hashes"]
    stats = progress["stats"]

    if completed:
        print(f"从断点恢复: 已完成 {len(completed)} 个文件 "
              f"(替换 {stats['total_replaced']}, 跳过 {stats['total_skipped']})")
    print()

    # ====== 逐目录处理 ======
    for folder_path in leaf_dirs:
        leaf_name = os.path.basename(folder_path)
        parent_hint = get_parent_path_hint(folder_path, ROOT_DIR)
        log_file = os.path.join(LOG_DIR, f"{leaf_name}.txt")

        setup_env(folder_path, LOG_DIR)
        clean_temp_files(folder_path)

        empty_files = scan_empty_files(folder_path, TARGET_FILE_TYPES)
        if not empty_files:
            continue

        pending_files = []
        for fi in empty_files:
            key = _make_file_key(folder_path, fi["filename"])
            if key not in completed:
                pending_files.append(fi)

        if not pending_files:
            continue

        log_event(f"{'='*60}", log_file)
        log_event(f">>> 目录: {leaf_name}", log_file)
        log_event(f"    路径: {folder_path}", log_file)
        log_event(
            f"    空文件: {len(empty_files)} 个, "
            f"待处理: {len(pending_files)} 个, "
            f"已完成: {len(empty_files) - len(pending_files)} 个",
            log_file,
        )

        with _progress_lock:
            _update_stats(stats, 'total_empty_files', len(pending_files))

        # ------ 并发处理文件 ------
        total = len(pending_files)
        with ThreadPoolExecutor(max_workers=NUM_WORKERS, thread_name_prefix="worker") as executor:
            futures = {}
            for file_idx, file_info in enumerate(pending_files):
                future = executor.submit(
                    _process_single_file,
                    file_info=file_info,
                    file_idx=file_idx,
                    total_files=total,
                    folder_path=folder_path,
                    parent_hint=parent_hint,
                    leaf_name=leaf_name,
                    log_file=log_file,
                    seen_urls=seen_urls,
                    seen_hashes=seen_hashes,
                    stats=stats,
                    completed=completed,
                    progress=progress,
                )
                futures[future] = file_info["filename"]

            for future in as_completed(futures):
                fname = futures[future]
                try:
                    future.result()
                except Exception as e:
                    log_event(
                        f"  [线程异常] {fname}: {str(e)[:120]}",
                        log_file,
                    )

        log_event(f"{'='*60}\n", log_file)
        time.sleep(random.uniform(1.0, 3.0))

    # ====== 全局统计 ======
    print("\n" + "=" * 60)
    print("全局运行统计:")
    print(f"  并发线程数:          {NUM_WORKERS}")
    print(f"  扫描到的空文件:      {stats['total_empty_files']}")
    print(f"  LLM 搜索调用次数:    {stats['total_llm_search_calls']}")
    print(f"  LLM 返回 URL 总数:   {stats['total_urls_returned']}")
    print(f"  --- 预检 ---")
    print(f"  预检通过:            {stats['total_precheck_passed']}")
    print(f"  预检拒绝:            {stats['total_precheck_rejected']}")
    print(f"  --- 下载 ---")
    print(f"  下载成功:            {stats['total_download_ok']}")
    print(f"  下载失败:            {stats['total_download_fail']}")
    print(f"  --- 验证 ---")
    print(f"  技术验证拒绝:        {stats['total_tech_verify_rejected']}")
    print(f"  LLM 内容验证拒绝:    {stats['total_llm_verify_rejected']}")
    print(f"  --- 最终结果 ---")
    print(f"  成功替换:            {stats['total_replaced']}")
    print(f"  未找到替代:          {stats['total_skipped']}")
    print(f"  URL 去重池:          {len(seen_urls)}")
    print(f"  哈希去重池:          {len(seen_hashes)}")
    print(f"  --- 协同生成 ---")
    print(f"  协同生成尝试:        {stats['total_synth_attempted']}")
    print(f"  协同文件生成数:      {stats['total_synth_generated']}")
    print("=" * 60)

    with _progress_lock:
        save_progress(progress)
    print(f"\n进度已保存到: {PROGRESS_FILE}")


if __name__ == "__main__":
    run_task()