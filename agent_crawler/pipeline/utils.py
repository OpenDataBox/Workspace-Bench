"""
utils.py — 通用工具函数（线程安全版）

变更:
  - log_event 加了 threading.Lock，多线程写同一日志文件不会交错
  - 新增 ThreadSafeSet，供 seen_urls / seen_hashes 使用
"""

import os
import re
import datetime
import threading

from pipeline.config import (
    FILE_TYPE_EXTENSION_MAP, EMPTY_FILE_THRESHOLD,
)

# ================= 线程安全日志 =================

_log_lock = threading.Lock()


def log_event(message, log_file):
    """记录日志到文件并打印到控制台（线程安全）。"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tid = threading.current_thread().name
    full_msg = f"[{timestamp}][{tid}] {message}"
    with _log_lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
        print(full_msg)


# ================= 线程安全 Set =================

class ThreadSafeSet:
    """
    线程安全的 set 包装。

    关键方法 try_add(): 原子性地检查+添加，解决 check-then-act 竞态。
    用于 seen_urls / seen_hashes 的跨线程去重。
    """

    def __init__(self, initial=None):
        self._set = set(initial) if initial else set()
        self._lock = threading.Lock()

    def __contains__(self, item):
        with self._lock:
            return item in self._set

    def add(self, item):
        with self._lock:
            self._set.add(item)

    def try_add(self, item):
        """
        原子性地 检查 + 添加。

        Returns:
            True  — item 是新的，已添加
            False — item 已存在，未添加
        """
        with self._lock:
            if item in self._set:
                return False
            self._set.add(item)
            return True

    def __len__(self):
        with self._lock:
            return len(self._set)

    def __iter__(self):
        """返回快照迭代器，供 list() / json 序列化使用。"""
        with self._lock:
            return iter(list(self._set))


# ================= 原有工具函数（不变） =================

def setup_env(*dirs):
    """确保指定的目录都存在。"""
    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)


def parse_leaf_directories(md_path, root_dir):
    """解析 folder_structure.md，提取所有叶子节点的绝对路径。"""
    if not os.path.exists(md_path):
        print(f"找不到结构文件: {md_path}")
        return []

    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    paths = []
    stack = []

    for line in lines:
        match = re.match(r'^(\s*)-\s*\*\*(.*?)/?\*\*', line)
        if match:
            indent = len(match.group(1))
            name = match.group(2).replace('/', '')
            if name == '[Root]':
                continue
            depth = indent // 2
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, name))
            current_path = os.path.join(root_dir, *[x[1] for x in stack])
            paths.append(current_path)

    leaves = []
    for p in paths:
        is_leaf = all(
            other == p or not other.startswith(p + os.sep)
            for other in paths
        )
        if is_leaf:
            leaves.append(p)
    return leaves


def get_parent_path_hint(folder_path, root_dir):
    """从完整路径中提取父目录链，供 LLM 理解上下文。"""
    try:
        rel = os.path.relpath(folder_path, root_dir)
        parts = rel.split(os.sep)
        if len(parts) > 1:
            return "/".join(parts[:-1])
    except Exception:
        pass
    return ""


def scan_empty_files(folder_path, target_types):
    """
    扫描叶子目录中的空文件，按用户选择的文件类型筛选。
    """
    target_extensions = set()
    for t in target_types:
        exts = FILE_TYPE_EXTENSION_MAP.get(t, [])
        target_extensions.update(exts)

    if not target_extensions:
        return []

    results = []

    if not os.path.isdir(folder_path):
        return []

    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)

        if os.path.isdir(file_path):
            continue

        stem, ext = os.path.splitext(filename)
        ext_lower = ext.lower()

        if ext_lower not in target_extensions:
            continue

        try:
            size = os.path.getsize(file_path)
        except OSError:
            continue

        if size > EMPTY_FILE_THRESHOLD:
            continue

        file_type = None
        for type_name, ext_list in FILE_TYPE_EXTENSION_MAP.items():
            if ext_lower in ext_list:
                file_type = type_name
                break

        results.append({
            "path": file_path,
            "filename": filename,
            "stem": stem,
            "ext": ext_lower,
            "type": file_type,
            "size": size,
        })

    return results