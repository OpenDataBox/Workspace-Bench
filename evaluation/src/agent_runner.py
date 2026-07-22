from posixpath import basename
from tqdm import tqdm
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import yaml
import sys
from pathlib import Path

from filesys_utils import filesys_rollback
import agent_as_a_judge

Json = Any

LANGUAGE_ALIASES = {
    "en": "en",
    "cn": "cn",
    "zh": "cn",
}


def _normalize_language_value(value: Json) -> Optional[str]:
    key = str(value or "").strip().lower()
    if not key:
        return None
    return LANGUAGE_ALIASES.get(key)


def _normalize_prompt_language(value: Json) -> str:
    key = str(value or "auto").strip().lower()
    if not key or key == "auto":
        return "auto"
    lang = _normalize_language_value(key)
    if lang:
        return lang
    valid = ", ".join(["auto"] + sorted(LANGUAGE_ALIASES))
    raise SystemExit(f"unsupported prompt_language: {value!r}; choose one of: {valid}")


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
    )


def _flatten_language_values(value: Json) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(_flatten_language_values(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_flatten_language_values(item))
        return out
    return []


def _detect_language_from_text(*values: Json) -> str:
    text = "\n".join(part for value in values for part in _flatten_language_values(value))
    cjk = 0
    latin = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            latin += 1
    denom = cjk + latin
    if cjk >= 8 and denom > 0 and (cjk / denom) >= 0.08:
        return "cn"
    return "en"


def _language_signal_present(*values: Json) -> bool:
    text = "\n".join(part for value in values for part in _flatten_language_values(value))
    return any(_is_cjk(ch) or ("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in text)


def _meta_language_values(meta: Dict[str, Json]) -> List[Json]:
    return [meta.get("task"), meta.get("rubrics"), meta.get("rubric_types")]


def _language_marker_from_meta(meta: Dict[str, Json]) -> Optional[str]:
    mp = meta.get("__metadata_path")
    if not isinstance(mp, str) or not mp.strip():
        return None
    cur = Path(mp).resolve().parent
    for _ in range(3):
        marker = cur / ".workspace_bench_language"
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            value = ""
        except Exception:
            value = ""
        lang = _normalize_language_value(value)
        if lang:
            return lang
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def _language_warning(task_id: Json, message: str) -> str:
    prefix = f"task {task_id}: " if task_id else ""
    return f"[language-warning] {prefix}{message}"


def _resolve_language_info(meta: Dict[str, Json]) -> Dict[str, Json]:
    values = _meta_language_values(meta)
    detected = _detect_language_from_text(*values)
    has_signal = _language_signal_present(*values)
    raw_meta_language = meta.get("language")
    meta_language = _normalize_language_value(raw_meta_language)
    task_id = meta.get("id")

    if raw_meta_language not in (None, "") and meta_language is None:
        warning = _language_warning(
            task_id,
            f"unsupported metadata language {raw_meta_language!r}; falling back to content detection",
        )
        if has_signal:
            return {"language": detected, "source": "detected", "warning": warning}
        marker_language = _language_marker_from_meta(meta)
        if marker_language:
            return {"language": marker_language, "source": "marker", "warning": warning}
        return {"language": "en", "source": "default", "warning": warning}

    if meta_language:
        warning = None
        if has_signal and detected != meta_language:
            warning = _language_warning(
                task_id,
                f"metadata language {meta_language!r} conflicts with content-detected {detected!r}; using metadata",
            )
        return {"language": meta_language, "source": "metadata", "warning": warning}

    if has_signal:
        return {"language": detected, "source": "detected", "warning": None}

    marker_language = _language_marker_from_meta(meta)
    if marker_language:
        return {"language": marker_language, "source": "marker", "warning": None}
    return {"language": "en", "source": "default", "warning": None}


def _infer_language_from_meta(meta: Dict[str, Json]) -> str:
    return str(_resolve_language_info(meta).get("language") or "en")


def _language_text_map(value: Json) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for raw_key, raw_value in value.items():
        lang = _normalize_language_value(raw_key)
        if lang and raw_value is not None:
            out[lang] = str(raw_value)
    return out

def _repo_root() -> Path:
    return Path(__file__).resolve().parent

def _ensure_import_path() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _rmtree_retry(path: str, *, attempts: int = 5) -> None:
    last_error: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last_error = e
            if attempt + 1 >= attempts:
                break
            time.sleep(0.2 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_yaml(path: str) -> Dict[str, Json]:
    with open(path, "r", encoding="utf-8") as f:
        obj = yaml.safe_load(f)
    if not isinstance(obj, dict):
        raise ValueError("run yaml must be a mapping")
    return _expand_config_env(obj)


def _expand_env_string(value: str) -> str:
    fallback_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:-]-(\$\{[A-Za-z_][A-Za-z0-9_]*\}|[^}]*)\}")
    s = value
    while True:
        m = fallback_re.search(s)
        if not m:
            break
        primary = os.environ.get(m.group(1), "")
        fallback = m.group(2)
        repl = primary if primary else os.path.expandvars(fallback)
        s = s[: m.start()] + repl + s[m.end() :]
    return os.path.expandvars(s)


def _expand_config_env(value: Json) -> Json:
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, list):
        return [_expand_config_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_config_env(v) for k, v in value.items()}
    return value


def _inputs_dir_from_meta(meta: Dict[str, Json]) -> str:
    mp = meta.get("__metadata_path")
    if isinstance(mp, str) and mp.strip():
        base = os.path.dirname(os.path.abspath(mp))
        cand = os.path.join(base, "data")
        if os.path.isdir(cand):
            return cand
    return ""


def _with_env(overrides: Dict[str, str]):
    """
    Context manager-like helper without importing contextlib.
    """
    old: Dict[str, Optional[str]] = {}
    for k, v in overrides.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v

    def _restore():
        for k, prev in old.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev

    return _restore


def _safe_name(s: str) -> str:
    out = []
    for ch in str(s or ""):
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    return ("".join(out)[:120] or "item")


def _normalize_rel_path(p: str) -> str:
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    return s


def _iter_metadata_paths(root: str, *, limit: Optional[int] = None) -> List[str]:
    root = os.path.abspath(root)
    if os.path.isfile(root) and os.path.basename(root) == "metadata.json":
        return [root]
    if os.path.isfile(root):
        return []
    out: List[str] = []
    lim = None if limit is None else max(0, int(limit))
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return out
    for name in entries:
        task_dir = os.path.join(root, name)
        if not os.path.isdir(task_dir):
            continue
        meta_path = os.path.join(task_dir, "metadata.json")
        if os.path.isfile(meta_path):
            out.append(meta_path)
            if lim is not None and len(out) >= lim:
                return out
    if out:
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if "metadata.json" in filenames:
            out.append(os.path.join(dirpath, "metadata.json"))
            if lim is not None and len(out) >= lim:
                break
    return out


def _metadata_task_id(meta: Dict[str, Json], metadata_path: str) -> str:
    value = meta.get("id")
    if value in (None, ""):
        value = meta.get("absolute_id")
    if value in (None, ""):
        value = os.path.basename(os.path.dirname(metadata_path))
    return str(value).strip()


def _normalize_config_task_ids(value: Json) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("task_ids must be a list")
    task_ids = [str(item).strip() for item in value]
    if any(not task_id for task_id in task_ids):
        raise ValueError("task_ids must not contain empty values")
    duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate task id(s): {', '.join(duplicates)}")
    return task_ids


def _load_metadatas(
    tasks_root: str,
    *,
    limit: Optional[int] = None,
    task_ids: Json = None,
    persona: Json = None,
) -> List[Dict[str, Json]]:
    requested_ids = _normalize_config_task_ids(task_ids)
    persona_value = None if persona is None else str(persona).strip()
    if persona is not None and not persona_value:
        raise ValueError("persona must not be empty")
    selected = sum([limit is not None, bool(requested_ids), persona_value is not None])
    if selected > 1:
        raise ValueError("task_limit, task_ids, and persona are mutually exclusive")

    metas: List[Dict[str, Json]] = []
    for mp in _iter_metadata_paths(tasks_root):
        try:
            meta = _read_json(mp)
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        m = dict(meta)
        m["__metadata_path"] = mp
        metas.append(m)

    if requested_ids:
        by_id: Dict[str, Dict[str, Json]] = {}
        duplicate_metadata_ids: List[str] = []
        for meta in metas:
            metadata_path = str(meta["__metadata_path"])
            task_id = _metadata_task_id(meta, metadata_path)
            if task_id in by_id:
                duplicate_metadata_ids.append(task_id)
            else:
                by_id[task_id] = meta
        if duplicate_metadata_ids:
            duplicate_text = ", ".join(sorted(set(duplicate_metadata_ids)))
            raise ValueError(f"duplicate task id(s) in dataset: {duplicate_text}")
        missing = [task_id for task_id in requested_ids if task_id not in by_id]
        if missing:
            raise ValueError(f"task id(s) not found: {', '.join(missing)}")
        return [by_id[task_id] for task_id in requested_ids]

    if persona_value is not None:
        matched = [meta for meta in metas if str(meta.get("persona") or "").strip() == persona_value]
        if not matched:
            available = sorted({str(meta.get("persona") or "").strip() for meta in metas} - {""})
            suffix = f"; available personas: {', '.join(available)}" if available else ""
            raise ValueError(f"persona not found: {persona_value}{suffix}")
        return matched

    if limit is not None:
        return metas[: max(0, int(limit))]
    return metas


def _resolve_work_dir(meta: Dict[str, Json], fs_map: Dict[str, str]) -> str:
    fs = meta.get("file_system")
    if isinstance(fs, str) and fs in fs_map:
        return str(fs_map.get(fs))
    return str(fs_map.get("*") or "")


def _copy_from_manifest(meta: Dict[str, Json], *, work_dir: str) -> List[str]:
    created: List[str] = []
    mp = meta.get("__metadata_path")
    source_base = os.path.dirname(os.path.abspath(str(mp))) if isinstance(mp, str) and mp else None
    if not source_base:
        return created
    dm = meta.get("data_manifest")
    if not isinstance(dm, list):
        return created
    for it in dm:
        if not isinstance(it, dict):
            continue
        target_path = it.get("target_path")
        stored_relpath = it.get("stored_relpath")
        if not isinstance(target_path, str) or not isinstance(stored_relpath, str):
            continue
        src = os.path.abspath(os.path.join(source_base, stored_relpath))
        if not os.path.isfile(src):
            continue
        rel_target = _normalize_rel_path(target_path)
        dst = os.path.abspath(os.path.join(work_dir, rel_target))
        _ensure_dir(os.path.dirname(dst))
        shutil.copy2(src, dst)
        created.append(dst)
    return created


def _expected_output_files(meta: Dict[str, Json]) -> List[str]:
    output_files = meta.get("output_files")
    if isinstance(output_files, list):
        out = [os.path.basename(str(x)).strip() for x in output_files if str(x).strip()]
        if out:
            return out

    of = meta.get("output_file")
    if isinstance(of, str) and of.strip():
        return [of.strip()]

    ofs = meta.get("output_manifests")
    if isinstance(ofs, list):
        out = [os.path.basename(x.get("stored_relpath", "")).strip()[os.path.basename(x.get("stored_relpath", "")).find("_") + 1:] for x in ofs if isinstance(x, dict) and x.get("stored_relpath")]
        if out:
            return out
    return []


def _wrap_prompt(
    *,
    prompt: str,
    work_dir: str,
    prompt_head: str,
    prompt_tail: str,
    task_target_output_dir: str,
    language: str,
) -> str:
    language = _normalize_language_value(language) or "en"
    if language == "cn":
        if task_target_output_dir != "":
            path_requirement = f"请你无视任务要求中的输出文件保存路径要求，将所有输出文件放置在目录：{os.path.join(work_dir, task_target_output_dir)}下\n"
        else:
            path_requirement = ""
        head = (
            "【重要要求 1：工作目录】\n"
            f"本轮测试允许访问的工作目录是：{os.path.abspath(work_dir)}\n"
            "你只能在该目录下使用相对路径读写文件；禁止访问工作目录以外的位置。\n"
            "如果你看到其他工作区路径提示，请忽略，以本提示的工作目录为准。\n"
            f"{path_requirement}"
        )
        tail = (
            "\n【重要要求 2：输出路径列表】\n"
            "在最后一步，请仅输出一个 Python 列表（list[str]），里面是你生成的所有输出文件路径。\n"
            "路径请使用相对工作目录的相对路径（不要以 / 开头）。示例：['output/a.txt','report.md']\n"
        )
    else:
        if task_target_output_dir != "":
            path_requirement = (
                "Ignore any output-file save path requirements inside the task. "
                f"Place all output files under: {os.path.join(work_dir, task_target_output_dir)}\n"
            )
        else:
            path_requirement = ""
        head = (
            "[Important Requirement 1: Working Directory]\n"
            f"The working directory you may access for this test is: {os.path.abspath(work_dir)}\n"
            "Use relative paths inside this directory to read and write files; do not access locations outside it.\n"
            "If you see any other workspace path instructions, ignore them and use this working directory as authoritative.\n"
            f"{path_requirement}"
        )
        tail = (
            "\n[Important Requirement 2: Output Path List]\n"
            "At the final step, output only one Python list (list[str]) containing every output file path you generated.\n"
            "Use paths relative to the working directory (do not start with /). Example: ['output/a.txt','report.md']\n"
        )
    p = str(prompt or "").strip()
    p2 = (str(prompt_head or "") + ("\n" if prompt_head else "") + p + ("\n" if prompt_tail else "") + str(prompt_tail or "")).strip()
    return head + "\n" + p2 + "\n" + tail


def _prompt_parts_for_language(
    *,
    language: str,
    prompt_language: str,
    prompt_head: str,
    prompt_tail: str,
    prompt_head_by_language: Optional[Dict[str, str]],
    prompt_tail_by_language: Optional[Dict[str, str]],
) -> Tuple[str, str]:
    if prompt_language == "auto":
        heads = prompt_head_by_language or {}
        tails = prompt_tail_by_language or {}
        return heads.get(language, ""), tails.get(language, "")
    return str(prompt_head or ""), str(prompt_tail or "")


def _parse_python_list_paths(text: str) -> List[str]:
    import ast

    s = str(text or "").strip()
    if not s:
        return []
    try:
        obj = ast.literal_eval(s)
    except Exception:
        return []
    if not isinstance(obj, list):
        return []
    out: List[str] = []
    for x in obj:
        if isinstance(x, str) and x.strip() and not x.strip().startswith("/"):
            out.append(x.strip())
    return out


def _find_by_basename(root: str, basenames: List[str]) -> List[str]:
    want = set([os.path.basename(b) for b in basenames if isinstance(b, str) and b])
    if not want:
        return []
    found: List[str] = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn in want:
                found.append(os.path.abspath(os.path.join(dirpath, fn)))
    return sorted(set(found))

def _find_by_fullname(root: str, fullnames: List[str]) -> List[str]:
    want = set([b for b in fullnames if isinstance(b, str) and b])
    if not want:
        return []
    found: List[str] = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn in want:
                found.append(os.path.abspath(os.path.join(dirpath, fn)))
    return sorted(set(found))


def _pick_recent_by_basename(paths: List[str], *, min_mtime: Optional[float]) -> List[str]:
    if not paths:
        return []
    by_name: Dict[str, Tuple[float, str]] = {}
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except Exception:
            continue
        if min_mtime is not None and mt < float(min_mtime):
            continue
        bn = os.path.basename(p)
        cur = by_name.get(bn)
        if cur is None or mt > cur[0]:
            by_name[bn] = (mt, p)
    out = [v[1] for v in by_name.values()]
    return sorted(set(out))


def _resolve_under(root: str, p: str) -> str:
    rel = _normalize_rel_path(p)
    abs_p = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if abs_p != root_abs and not abs_p.startswith(root_abs + os.sep):
        raise ValueError("path escapes work dir")
    return abs_p


def _clean_target_output_dir(*, work_dir: str, task_target_output_dir: str) -> None:
    rel = _normalize_rel_path(task_target_output_dir)
    if not rel:
        return
    target = os.path.abspath(os.path.join(work_dir, rel))
    work_abs = os.path.abspath(work_dir)
    if target == work_abs or not target.startswith(work_abs + os.sep):
        raise ValueError("target output dir escapes work dir")
    if os.path.isdir(target):
        _rmtree_retry(target)
    elif os.path.exists(target):
        os.unlink(target)
    _ensure_dir(target)


OUTPUT_DIFF_DIR = "_misplaced_outputs"


def _target_output_root(work_dir: str, task_target_output_dir: str) -> str:
    rel = _normalize_rel_path(task_target_output_dir)
    return os.path.abspath(os.path.join(work_dir, rel)) if rel else os.path.abspath(work_dir)


def _is_under(path: str, root: str) -> bool:
    path_abs = os.path.abspath(path)
    root_abs = os.path.abspath(root)
    return path_abs == root_abs or path_abs.startswith(root_abs + os.sep)


def _snapshot_work_dir_files(*, work_dir: str, task_target_output_dir: str) -> Dict[str, Tuple[int, float]]:
    work_abs = os.path.abspath(work_dir)
    target_abs = _target_output_root(work_dir, task_target_output_dir)
    skipped_dir_names = {
        "model_output",
        ".workspace_data",
        ".git",
        ".cache",
        "__pycache__",
        "node_modules",
    }
    out: Dict[str, Tuple[int, float]] = {}
    for dirpath, dirnames, filenames in os.walk(work_abs):
        dir_abs = os.path.abspath(dirpath)
        if _is_under(dir_abs, target_abs):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skipped_dir_names and not d.startswith(".")
        ]
        for fn in filenames:
            path = os.path.abspath(os.path.join(dir_abs, fn))
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, work_abs).replace("\\", "/")
            out[rel] = (int(st.st_size), float(st.st_mtime))
    return out


def _should_mirror_workdir_file(path: str, *, expected_names: Optional[set] = None) -> bool:
    name = os.path.basename(path)
    if not name:
        return False
    if expected_names and name in expected_names:
        return True
    if name.startswith("."):
        return False
    lowered = name.lower()
    if lowered.endswith((".tmp", ".bak", ".log", ".trace", "~")):
        return False
    if lowered in {"agent.json", "metadata.json", "manifest.json", "trace.json", "trace.txt"}:
        return False
    if re.match(r"^(build|gen|generate|tmp|debug|scratch)[-_].*\.py$", lowered):
        return False
    return True


def _copy_misplaced_outputs_to_target(
    *,
    work_dir: str,
    task_target_output_dir: str,
    before: Dict[str, Tuple[int, float]],
    min_mtime: float,
    raw_dir: str,
    expected_files: List[str],
) -> List[Dict[str, Json]]:
    work_abs = os.path.abspath(work_dir)
    target_abs = _target_output_root(work_dir, task_target_output_dir)
    if target_abs == work_abs:
        _write_json(os.path.join(raw_dir, "misplaced_outputs.json"), [])
        return []

    after = _snapshot_work_dir_files(work_dir=work_dir, task_target_output_dir=task_target_output_dir)
    misplaced: List[Dict[str, Json]] = []
    misplaced_root = os.path.join(target_abs, OUTPUT_DIFF_DIR)
    expected_names = {os.path.basename(x) for x in expected_files if isinstance(x, str) and x}

    for rel, state in sorted(after.items()):
        src = os.path.abspath(os.path.join(work_abs, rel))
        if not os.path.isfile(src) or not _should_mirror_workdir_file(src, expected_names=expected_names):
            continue
        prev = before.get(rel)
        changed = prev is None or prev[0] != state[0] or abs(prev[1] - state[1]) > 1e-6
        if not changed:
            continue
        if prev is not None and state[1] < min_mtime:
            continue

        safe_rel = _normalize_rel_path(rel)
        if not safe_rel:
            continue
        dst = os.path.abspath(os.path.join(misplaced_root, safe_rel))
        if not _is_under(dst, misplaced_root):
            continue
        if os.path.abspath(dst) == os.path.abspath(src):
            continue
        _ensure_dir(os.path.dirname(dst))
        shutil.copy2(src, dst)
        misplaced.append(
            {
                "sourcePath": safe_rel,
                "targetPath": os.path.relpath(dst, target_abs).replace("\\", "/"),
                "reason": "new" if prev is None else "modified",
                "sizeBytes": int(state[0]),
            }
        )

    _write_json(os.path.join(raw_dir, "misplaced_outputs.json"), misplaced)
    return misplaced


def _collect_output_paths(
    *,
    task_target_output_dir: str,
    work_dir: str,
    expected_files: List[str],
    returned_paths: List[str],
    last_text: str,
    min_mtime: Optional[float],
) -> Tuple[List[str], List[str]]:
    out: List[str] = []
    retrieval_method = []
    skipped_output_names = {
        "trace.txt",
        "trace.json",
        "phase1_file_discovery.md",
        "phase1_files.json",
        "phase2_data_summary.md",
    }
    expected_name_set = {os.path.basename(x) for x in expected_files}

    def prefer_target_output_matches(paths: List[str]) -> List[str]:
        if task_target_output_dir == "":
            return paths
        target_abs = _target_output_root(work_dir, task_target_output_dir)
        by_name: Dict[str, List[str]] = {}
        for p in paths:
            by_name.setdefault(os.path.basename(p), []).append(p)
        out_paths: List[str] = []
        for name in sorted(by_name):
            candidates = by_name[name]
            target_candidates = [p for p in candidates if _is_under(p, target_abs)]
            out_paths.extend(target_candidates or candidates)
        return sorted(set(out_paths))

    def is_internal_output_name(name: str) -> bool:
        if name in skipped_output_names:
            return True
        if name in expected_name_set:
            return False
        lowered = name.lower()
        if lowered.endswith(".bak") or lowered.endswith("~"):
            return True
        return bool(re.match(r"^(build|gen|generate|tmp|debug|scratch)[-_].*\.py$", lowered))

    for rp in _parse_python_list_paths(last_text):
        try:
            ap = _resolve_under(work_dir, rp)
        except Exception:
            continue
        if os.path.isfile(ap) and not is_internal_output_name(os.path.basename(ap)):
            out.append(ap)
    if out:
        retrieval_method.append("last_text_paths")
        # return (sorted(set(out)), "last_text_paths")

    found = _find_by_fullname(work_dir, expected_files)
    found = prefer_target_output_matches(found)
    # picked = _pick_recent_by_basename(found, min_mtime=min_mtime)
    if found:
        retrieval_method.append("expected_filenames_recent")
        out.extend(found)
        # return (sorted(set(found)), "expected_filenames_recent")

    found2 = []
    for p in returned_paths:
        if not isinstance(p, str) or not p:
            continue
        try:
            ap = _resolve_under(work_dir, p)
        except Exception:
            continue
        if os.path.isfile(ap) and not is_internal_output_name(os.path.basename(ap)):
            found2.append(ap)
    # picked2 = _pick_recent_by_basename(out, min_mtime=min_mtime)
    if found2:
        retrieval_method.append("returned_paths_recent")
        out.extend(found2)
        # return (sorted(set(found2)), "returned_paths_recent")
    
    if task_target_output_dir != "":
        found3 = []
        # 获取task_target_output_dir目录下所有文件的路径
        for root, dirs, files in os.walk(os.path.join(work_dir, task_target_output_dir)):
            # print(files)
            for file in files:
                if is_internal_output_name(file):
                    continue
                file_path = os.path.abspath(os.path.join(root, file))
                found3.append(file_path)
        if found3:
            retrieval_method.append("task_target_output_dir")
            out.extend(found3)

    # print(sorted(set(out)))
    return (sorted(set(out)), retrieval_method)

def _read_text_limited(path: str, *, limit: int = 200000) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            s = f.read(limit + 1)
        if len(s) > limit:
            return s[:limit]
        return s
    except Exception:
        return None


def _output_relpath(src: str, *, preserve_root: Optional[str]) -> str:
    if preserve_root:
        try:
            rel = os.path.relpath(os.path.abspath(src), os.path.abspath(preserve_root)).replace("\\", "/")
            if rel != ".." and not rel.startswith("../"):
                rel = _normalize_rel_path(rel)
                if rel:
                    return rel
        except Exception:
            pass
    return os.path.basename(src) or "output"


def _copy_outputs(*, output_paths: List[str], out_dir: str, preserve_root: Optional[str] = None) -> List[Dict[str, Json]]:
    _ensure_dir(out_dir)
    manifest: List[Dict[str, Json]] = []
    preserve_abs = os.path.abspath(preserve_root) if preserve_root else ""

    def output_priority(src: str) -> Tuple[int, str]:
        src_abs = os.path.abspath(src)
        if preserve_abs and (src_abs == preserve_abs or src_abs.startswith(preserve_abs + os.sep)):
            return (0, src_abs)
        return (1, src_abs)

    for src in sorted(output_paths, key=output_priority):
        if not src or not os.path.isfile(src):
            continue
        rel = _output_relpath(src, preserve_root=preserve_root)
        dst = os.path.abspath(os.path.join(out_dir, rel))
        out_abs = os.path.abspath(out_dir)
        if dst != out_abs and not dst.startswith(out_abs + os.sep):
            rel = os.path.basename(src) or "output"
            dst = os.path.join(out_abs, rel)
        if os.path.abspath(dst) == os.path.abspath(src):
            continue
        if os.path.exists(dst):
            i = 1
            rel_dir = os.path.dirname(rel)
            base = os.path.basename(rel)
            b, ext = os.path.splitext(base)
            while os.path.exists(dst):
                dst = os.path.join(out_abs, rel_dir, f"{b}_{i}{ext}")
                i += 1
        _ensure_dir(os.path.dirname(dst))
        shutil.copy2(src, dst)
        manifest.append(
            {"sourcePath": rel, "outputPath": os.path.relpath(dst, out_dir).replace("\\", "/"), "sizeBytes": os.path.getsize(dst)}
        )
    return manifest


def _load_agent_run(agent_name: str):
    here = os.path.abspath(os.path.dirname(__file__))
    agent_path = os.path.join(here, "agents", f"{agent_name.lower()}.py")
    if not os.path.exists(agent_path):
        raise FileNotFoundError(f"missing agent file: {agent_path}")
    spec = importlib.util.spec_from_file_location(f"evaluation_sys.agents.{agent_name.lower()}", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load agent module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "run", None)
    if not callable(fn):
        raise RuntimeError("agent module missing run()")
    return fn


def _new_summary() -> Dict[str, int]:
    return {"total": 0, "passed": 0, "failed": 0, "error": 0, "timeout": 0}


def _merge_summary(dst: Dict[str, int], src: Dict[str, int]) -> None:
    for key in ("total", "passed", "failed", "error", "timeout"):
        dst[key] = int(dst.get(key, 0)) + int(src.get(key, 0))


def _as_bool(value: Json, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _as_int(value: Json, *, default: int, minimum: int = 1) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(int(minimum), out)


def _cleanup_policy(value: Json) -> str:
    s = str(value or "failed").strip().lower()
    if s in {"failed", "failure", "failures"}:
        return "failed"
    if s in {"always", "all", "true", "1"}:
        return "always"
    if s in {"never", "none", "false", "0"}:
        return "never"
    return "failed"


def _map_value_for_meta(meta: Dict[str, Json], mapping: Dict[str, str]) -> str:
    fs = str(meta.get("file_system") or "*")
    if fs in mapping:
        return str(mapping.get(fs) or "")
    return str(mapping.get("*") or "")


def _copytree_fast(src: str, dst: str) -> Dict[str, Json]:
    started = time.time()
    method = "cp-reflink-auto"
    error = None
    try:
        _ensure_dir(dst)
        proc = subprocess.run(
            ["cp", "-a", "--reflink=auto", os.path.join(src, "."), dst],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3600,
        )
        if proc.returncode == 0:
            return {"method": method, "durationMs": int((time.time() - started) * 1000)}
        error = (proc.stderr or proc.stdout or "").strip()[:1000]
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    if os.path.exists(dst):
        _rmtree_retry(dst)
    fallback_started = time.time()
    shutil.copytree(src, dst)
    return {
        "method": "shutil.copytree",
        "durationMs": int((time.time() - started) * 1000),
        "fallbackDurationMs": int((time.time() - fallback_started) * 1000),
        "fallbackFrom": method,
        "fallbackError": error,
    }


def _prepare_isolated_work_dir(*, case_dir: str, meta: Dict[str, Json], standard_work_dir_map: Dict[str, str]) -> Tuple[str, Dict[str, Json]]:
    standard_work_dir = _map_value_for_meta(meta, {str(k): str(v) for k, v in standard_work_dir_map.items()})
    if not standard_work_dir:
        raise RuntimeError("cannot resolve standard work dir")
    if not os.path.isdir(standard_work_dir):
        raise FileNotFoundError(f"standard work dir not found: {standard_work_dir}")

    work_dir = os.path.join(case_dir, "workdir")
    if os.path.exists(work_dir):
        _rmtree_retry(work_dir)
    copy_info = _copytree_fast(standard_work_dir, work_dir)
    copy_info["source"] = os.path.abspath(standard_work_dir)
    copy_info["destination"] = os.path.abspath(work_dir)
    return work_dir, copy_info


def _cleanup_isolated_work_dir(*, work_dir: str, final_status: str, policy: str) -> bool:
    if not work_dir or not os.path.isdir(work_dir):
        return False
    should_remove = False
    if policy == "always":
        should_remove = True
    elif policy == "failed" and final_status == "passed":
        should_remove = True
    elif policy == "never":
        should_remove = False
    if should_remove:
        _rmtree_retry(work_dir)
        return False
    return True


def _group_metas_by_file_system(metas: List[Dict[str, Json]]) -> Dict[str, List[Tuple[int, Dict[str, Json]]]]:
    grouped: Dict[str, List[Tuple[int, Dict[str, Json]]]] = {}
    for idx, meta in enumerate(metas):
        fs_name = str(meta.get("file_system") or "*")
        grouped.setdefault(fs_name, []).append((idx, meta))
    return grouped


def _run_one_case(
    *,
    idx: int,
    meta: Dict[str, Json],
    runs_root: str,
    run_fn,
    prompt_head: str,
    prompt_tail: str,
    task_target_output_dir: str,
    timeout_sec: float,
    api_provider: Dict[str, Json],
    eval_while_running: bool,
    eval_yaml: str,
    work_dir_map: Dict[str, str],
    standard_work_dir_map: Dict[str, str],
    agent_name: str,
    model_name: str,
    isolated_workdir: bool,
    task_workdir_cleanup: str,
    prompt_language: str = "auto",
    prompt_head_by_language: Optional[Dict[str, str]] = None,
    prompt_tail_by_language: Optional[Dict[str, str]] = None,
) -> Dict[str, Json]:
    print(f"run task: {meta.get('id') or ''}")
    summary = _new_summary()
    summary["total"] = 1

    case_id = str(meta.get("id") or os.path.basename(os.path.dirname(str(meta.get("__metadata_path") or ""))) or "case_001")
    case_id_safe = _safe_name(case_id)
    case_dir = os.path.join(runs_root, case_id_safe)

    if os.path.exists(case_dir) and os.path.exists(os.path.join(case_dir, "output")) and os.listdir(os.path.join(case_dir, "output")):
        existing_agent = _read_json(os.path.join(case_dir, "agent.json"))
        status_existing = "passed"
        duration_existing = None
        output_files_existing: List[Dict[str, Json]] = []
        if isinstance(existing_agent, dict):
            status_existing = str(existing_agent.get("status") or "passed")
            duration_existing = existing_agent.get("durationMs") if isinstance(existing_agent.get("durationMs"), int) else None
            trace = existing_agent.get("trace") if isinstance(existing_agent.get("trace"), dict) else {}
            outputs = trace.get("outputs") if isinstance(trace.get("outputs"), dict) else {}
            manifest = outputs.get("outputManifest") if isinstance(outputs.get("outputManifest"), list) else []
            output_files_existing = [x for x in manifest if isinstance(x, dict)]
        if status_existing not in {"passed", "failed", "error", "timeout"}:
            status_existing = "passed"
        if not output_files_existing:
            out_dir = os.path.join(case_dir, "output")
            for root, _, files in os.walk(out_dir):
                for name in files:
                    path = os.path.join(root, name)
                    rel = os.path.relpath(path, out_dir).replace("\\", "/")
                    output_files_existing.append({"sourcePath": rel, "outputPath": rel, "sizeBytes": os.path.getsize(path)})
        summary[status_existing] += 1
        return {
            "index": idx,
            "summary": summary,
            "case": {
                "caseId": case_id,
                "outputDir": case_dir,
                "status": status_existing,
                "durationMs": duration_existing,
                "outputFiles": output_files_existing,
                "resumed": True,
            },
        }
    elif os.path.exists(case_dir):
        _rmtree_retry(case_dir)
    _ensure_dir(case_dir)

    _write_json(os.path.join(case_dir, "metadata.json"), meta)

    shared_work_dir = _resolve_work_dir(meta, {str(k): str(v) for k, v in work_dir_map.items()})
    if not shared_work_dir:
        raise RuntimeError("cannot resolve work dir")
    workdir_copy_info: Dict[str, Json] = {}
    if isolated_workdir:
        work_dir, workdir_copy_info = _prepare_isolated_work_dir(
            case_dir=case_dir,
            meta=meta,
            standard_work_dir_map=standard_work_dir_map,
        )
    else:
        work_dir = shared_work_dir
    _ensure_dir(work_dir)

    _copy_from_manifest(meta, work_dir=work_dir)
    _clean_target_output_dir(work_dir=work_dir, task_target_output_dir=task_target_output_dir)
    expected_files = _expected_output_files(meta)

    raw_dir = os.path.join(case_dir, "raw")
    _ensure_dir(raw_dir)

    output_diff_before = _snapshot_work_dir_files(
        work_dir=work_dir,
        task_target_output_dir=task_target_output_dir,
    )

    prompt_language = _normalize_prompt_language(prompt_language)
    language_info = _resolve_language_info(meta)
    language = str(language_info.get("language") or "en")
    if prompt_language != "auto":
        inferred_language = language
        language = prompt_language
        if inferred_language != language:
            language_info["warning"] = _language_warning(
                meta.get("id"),
                f"prompt_language forces {language!r}; inferred task language is {inferred_language!r}",
            )
        language_info["source"] = "config"
        language_info["language"] = language
    language_warning = language_info.get("warning")
    if isinstance(language_warning, str) and language_warning:
        print(language_warning, flush=True)

    effective_prompt_head, effective_prompt_tail = _prompt_parts_for_language(
        language=language,
        prompt_language=prompt_language,
        prompt_head=prompt_head,
        prompt_tail=prompt_tail,
        prompt_head_by_language=prompt_head_by_language,
        prompt_tail_by_language=prompt_tail_by_language,
    )

    prompt = _wrap_prompt(
        prompt=str(meta.get("task") or ""),
        work_dir=work_dir,
        prompt_head=effective_prompt_head,
        prompt_tail=effective_prompt_tail,
        task_target_output_dir=task_target_output_dir,
        language=language,
    )

    case_started = time.time()
    api_provider2 = dict(api_provider) if isinstance(api_provider, dict) else {}
    api_provider2["__expected_output_files__"] = expected_files
    run_res = run_fn(
        prompt=prompt,
        work_dir=work_dir,
        sandbox_dir=case_dir,
        timeout_s=timeout_sec,
        api_provider=api_provider2,
    )
    duration_ms = int((time.time() - case_started) * 1000)

    status_raw = str(run_res.get("status") or "").strip().lower()
    if status_raw not in {"ok", "timeout", "error"}:
        status_raw = "error"

    returned_paths_abs = run_res.get("paths") if isinstance(run_res.get("paths"), list) else []
    returned_paths_rel: List[str] = []
    for apath in returned_paths_abs:
        if not isinstance(apath, str) or not apath:
            continue
        try:
            rel = os.path.relpath(os.path.abspath(apath), os.path.abspath(work_dir))
        except Exception:
            continue
        if rel.startswith(".."):
            continue
        returned_paths_rel.append(rel.replace("\\", "/"))

    trace_obj = run_res.get("trace") if isinstance(run_res.get("trace"), dict) else {}
    last_text = str(trace_obj.get("lastText")) if isinstance(trace_obj.get("lastText"), str) else ""

    misplaced_outputs = _copy_misplaced_outputs_to_target(
        work_dir=work_dir,
        task_target_output_dir=task_target_output_dir,
        before=output_diff_before,
        min_mtime=case_started - 1.0,
        raw_dir=raw_dir,
        expected_files=expected_files,
    )

    output_paths, retrieval_method = _collect_output_paths(
        work_dir=work_dir,
        expected_files=expected_files,
        task_target_output_dir=task_target_output_dir,
        returned_paths=returned_paths_rel,
        last_text=last_text,
        min_mtime=case_started - 1.0,
    )
    if misplaced_outputs:
        retrieval_method = list(retrieval_method) + ["misplaced_output_diff"]
    if status_raw != "ok":
        if output_paths:
            retrieval_method = list(retrieval_method) + [f"partial_due_to_status:{status_raw}"]
        else:
            retrieval_method = ["skipped", f"status:{status_raw}"]

    preserve_root = (
        os.path.join(work_dir, task_target_output_dir)
        if task_target_output_dir != ""
        else work_dir
    )
    manifest = _copy_outputs(
        output_paths=output_paths,
        out_dir=os.path.join(case_dir, "output"),
        preserve_root=preserve_root,
    )

    checks = []
    if status_raw != "ok":
        checks.append(
            {
                "type": "returned_paths_exist",
                "passed": bool(output_paths),
                "detail": {
                    "status": status_raw,
                    "count": len(output_paths),
                    "partialOutputCollected": bool(output_paths),
                },
            }
        )
    elif output_paths:
        checks.append({"type": "returned_paths_exist", "passed": True, "detail": {"count": len(output_paths)}})
    else:
        checks.append({"type": "returned_paths_exist", "passed": False, "detail": "Agent returned empty path list"})

    if output_paths:
        final_status = "passed"
        summary["passed"] += 1
    elif status_raw == "timeout":
        final_status = "timeout"
        summary["timeout"] += 1
    elif status_raw == "error":
        final_status = "error"
        summary["error"] += 1
    else:
        final_status = "failed"
        summary["failed"] += 1

    metrics_obj = run_res.get("metrics") if isinstance(run_res.get("metrics"), dict) else {}
    turns = metrics_obj.get("turns") if isinstance(metrics_obj.get("turns"), int) else None
    prompt_tokens = metrics_obj.get("promptTokens") if isinstance(metrics_obj.get("promptTokens"), int) else None
    completion_tokens = metrics_obj.get("completionTokens") if isinstance(metrics_obj.get("completionTokens"), int) else None
    total_tokens = metrics_obj.get("totalTokens") if isinstance(metrics_obj.get("totalTokens"), int) else None

    stdout_txt = _read_text_limited(os.path.join(raw_dir, "stdout.txt"))
    stderr_txt = _read_text_limited(os.path.join(raw_dir, "stderr.txt"))

    exec_from_agent = trace_obj.get("executionTrace") if isinstance(trace_obj.get("executionTrace"), list) else None
    llm_from_agent = trace_obj.get("llm") if isinstance(trace_obj.get("llm"), dict) else None
    usage_total_from_agent = trace_obj.get("usageTotal") if isinstance(trace_obj.get("usageTotal"), dict) else None

    with open(os.path.join(case_dir, "agent.log"), "w", encoding="utf-8") as f:
        f.write(f"agent={agent_name} model={model_name}\n")
        f.write(f"workDir={os.path.abspath(work_dir)}\n")
        if workdir_copy_info:
            f.write(
                "workDirCopy="
                f"{workdir_copy_info.get('method')} "
                f"durationMs={workdir_copy_info.get('durationMs')}\n"
            )
        f.write(f"baseUrl={api_provider.get('baseUrl') if isinstance(api_provider, dict) else ''}\n")
        f.write(f"llmModel={api_provider.get('model') if isinstance(api_provider, dict) else ''}\n")
        f.write(f"timeoutSec={timeout_sec}\n")
        f.write(f"status={final_status} runnerStatus={status_raw} durationMs={duration_ms}\n")
        f.write(f"turns={turns} promptTokens={prompt_tokens} completionTokens={completion_tokens} totalTokens={total_tokens}\n")
        f.write(f"retrievalMethod={retrieval_method} outputs={len(output_paths)} returnedPaths={len(returned_paths_rel)}\n")
        if misplaced_outputs:
            f.write(f"misplacedOutputs={len(misplaced_outputs)}\n")
        if isinstance(exec_from_agent, list) and exec_from_agent:
            f.write(f"executionTrace={len(exec_from_agent)}\n")

    agent_json = {
        "caseId": case_id,
        "name": str(meta.get("id_prefix") or meta.get("name") or ""),
        "workDir": os.path.abspath(work_dir),
        "status": final_status,
        "runnerStatus": status_raw,
        "partialOutputCollected": status_raw != "ok" and bool(output_paths),
        "durationMs": duration_ms,
        "turns": turns,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
        "checks": checks,
        "errorType": ("Timeout" if status_raw == "timeout" else ("RunnerError" if status_raw == "error" else None)),
        "errorMessage": run_res.get("errorMessage") if isinstance(run_res.get("errorMessage"), str) else None,
        "traceback": None,
        "workDirCopy": workdir_copy_info or None,
        "trace": {
            "prompt": {
                "system": None,
                "user": prompt,
                "promptTail": effective_prompt_tail or None,
                "language": language,
                "languageSource": language_info.get("source") or "default",
                **({"languageDetectionWarning": language_warning} if isinstance(language_warning, str) and language_warning else {}),
            },
            "executionTrace": exec_from_agent or [],
            "llm": {
                "provider": (llm_from_agent.get("provider") if isinstance(llm_from_agent, dict) else (str(api_provider.get("provider_type") or "") if isinstance(api_provider, dict) else None)),
                "baseUrl": (llm_from_agent.get("baseUrl") if isinstance(llm_from_agent, dict) else (api_provider.get("baseUrl") if isinstance(api_provider, dict) else None)),
                "model": (llm_from_agent.get("model") if isinstance(llm_from_agent, dict) else (api_provider.get("model") if isinstance(api_provider, dict) else None)),
                "usageTotal": usage_total_from_agent or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
            "outputs": {
                "returnedPaths": returned_paths_rel,
                "retrievalMethod": retrieval_method,
                "outputManifest": manifest,
                "misplacedOutputs": misplaced_outputs,
            },
            "raw": {"stdout": stdout_txt, "stderr": stderr_txt},
        },
    }
    if isinstance(trace_obj, dict):
        agent_json["trace"]["raw"]["runner"] = {
            k: v
            for k, v in trace_obj.items()
            if k not in {"apiKey", "api_key", "openaiApiKey", "arkApiKey", "token", "auth", "authorization"}
        }

    _write_json(os.path.join(case_dir, "agent.json"), agent_json)
    case_out = {
        "caseId": case_id,
        "outputDir": case_dir,
        "status": final_status,
        "durationMs": duration_ms,
        "outputFiles": manifest,
    }

    if eval_while_running:
        try:
            judge_res = agent_as_a_judge.evaluate_task(
                task_dir=case_dir,
                eval_yaml_path=eval_yaml,
                overwrite=True,
                max_retries=3,
            )
            if not (isinstance(judge_res, dict) and judge_res.get("success") is True):
                judge_err = None
                if isinstance(judge_res, dict):
                    judge_err = judge_res.get("error") or judge_res.get("message")
                with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
                    f.write(f"\njudge_error=JudgeFailed: {judge_err or 'unknown error'}\n")
        except Exception as e:
            with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
                f.write(f"\njudge_error={type(e).__name__}: {e}\n")

    work_dir_retained = os.path.isdir(work_dir) if isolated_workdir else True
    if isolated_workdir:
        try:
            work_dir_retained = _cleanup_isolated_work_dir(
                work_dir=work_dir,
                final_status=final_status,
                policy=task_workdir_cleanup,
            )
        except Exception as e:
            work_dir_retained = os.path.isdir(work_dir)
            with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
                f.write(f"\nworkdir_cleanup_error={type(e).__name__}: {e}\n")
    else:
        try:
            filesys_rollback(
                standard_work_dir=standard_work_dir_map[meta["file_system"]],
                work_dir=work_dir_map[meta["file_system"]],
            )
        except Exception as e:
            with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
                f.write(f"\nrollback_error={type(e).__name__}: {e}\n")

    agent_json["workDirRetained"] = work_dir_retained
    agent_json["sharedWorkDir"] = os.path.abspath(shared_work_dir)
    _write_json(os.path.join(case_dir, "agent.json"), agent_json)
    case_out["workDirRetained"] = work_dir_retained

    return {"index": idx, "summary": summary, "case": case_out}


def _run_group(
    *,
    group_items: List[Tuple[int, Dict[str, Json]]],
    runs_root: str,
    run_fn,
    prompt_head: str,
    prompt_tail: str,
    task_target_output_dir: str,
    timeout_sec: float,
    api_provider: Dict[str, Json],
    eval_while_running: bool,
    eval_yaml: str,
    work_dir_map: Dict[str, str],
    standard_work_dir_map: Dict[str, str],
    agent_name: str,
    model_name: str,
    isolated_workdir: bool,
    task_workdir_cleanup: str,
    prompt_language: str = "auto",
    prompt_head_by_language: Optional[Dict[str, str]] = None,
    prompt_tail_by_language: Optional[Dict[str, str]] = None,
) -> Dict[str, Json]:
    group_summary = _new_summary()
    group_cases: List[Tuple[int, Dict[str, Json]]] = []
    for idx, meta in group_items:
        res = _run_one_case(
            idx=idx,
            meta=meta,
            runs_root=runs_root,
            run_fn=run_fn,
            prompt_head=prompt_head,
            prompt_tail=prompt_tail,
            task_target_output_dir=task_target_output_dir,
            timeout_sec=timeout_sec,
            api_provider=api_provider,
            eval_while_running=eval_while_running,
            eval_yaml=eval_yaml,
            work_dir_map=work_dir_map,
            standard_work_dir_map=standard_work_dir_map,
            agent_name=agent_name,
            model_name=model_name,
            isolated_workdir=isolated_workdir,
            task_workdir_cleanup=task_workdir_cleanup,
            prompt_language=prompt_language,
            prompt_head_by_language=prompt_head_by_language,
            prompt_tail_by_language=prompt_tail_by_language,
        )
        _merge_summary(group_summary, res["summary"])
        if isinstance(res.get("case"), dict):
            group_cases.append((int(res["index"]), res["case"]))
    return {"summary": group_summary, "cases": group_cases, "processed": len(group_items)}


def main() -> None:
    _ensure_import_path()

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-config", required=True)
    args = ap.parse_args()

    workspace_root = str(Path(__file__).resolve().parents[2])
    eval_root = str(Path(__file__).resolve().parents[1])
    os.environ.setdefault("WORKSPACE_BENCH_ROOT", os.environ.get("RIP_BENCH_ROOT", workspace_root))
    os.environ.setdefault("WORKSPACE_BENCH_EVAL_ROOT", os.environ.get("RIP_BENCH_EVAL_ROOT", eval_root))
    os.environ.setdefault("RIP_BENCH_ROOT", os.environ["WORKSPACE_BENCH_ROOT"])
    os.environ.setdefault("RIP_BENCH_EVAL_ROOT", os.environ["WORKSPACE_BENCH_EVAL_ROOT"])

    cfg = _read_yaml(args.run_config)
    agent_name = str(cfg.get("agent_name") or "").strip()
    model_name = str(cfg.get("model_name") or "").strip()
    run_name = str(cfg.get("run_name") or "").strip()
    task_path = str(cfg.get("task_path") or "").strip()
    task_target_output_dir = str(cfg.get("task_target_output_dir") or "").strip()
    output_dir = str(cfg.get("output_dir") or "").strip()
    fs_map_file = str(cfg.get("fs_map_file") or "").strip()
    prompt_head = str(cfg.get("prompt_head") or "")
    prompt_tail = str(cfg.get("prompt_tail") or "")
    prompt_language = _normalize_prompt_language(cfg.get("prompt_language", "auto"))
    prompt_head_by_language = _language_text_map(cfg.get("prompt_head_by_language"))
    prompt_tail_by_language = _language_text_map(cfg.get("prompt_tail_by_language"))
    task_limit = cfg.get("task_limit")
    task_ids = cfg.get("task_ids")
    persona = cfg.get("persona")
    timeout_sec = float(cfg.get("timeout_sec") or 300.0)
    api_provider = cfg.get("api_provider") if isinstance(cfg.get("api_provider"), dict) else {}

    eval_while_running = cfg.get("eval_while_running") or False
    eval_yaml = str(cfg.get("eval_yaml") or "").strip()
    workdir_parallel = _as_bool(cfg.get("workdir_parallel"), default=True)
    task_parallel = _as_bool(cfg.get("task_parallel"), default=True)
    task_parallel_workers = _as_int(cfg.get("task_parallel_workers"), default=10, minimum=1)
    task_workdir_cleanup = _cleanup_policy(cfg.get("task_workdir_cleanup"))
    cfg["task_parallel"] = task_parallel
    cfg["task_parallel_workers"] = task_parallel_workers
    cfg["task_workdir_cleanup"] = task_workdir_cleanup

    assert agent_name and model_name and run_name and task_path and output_dir and fs_map_file

    runs_root = os.path.abspath(os.path.join(output_dir, f"{agent_name}--{model_name}--{run_name}"))
    _ensure_dir(runs_root)

    fs_map_all = _read_json(os.path.abspath(fs_map_file))
    assert isinstance(fs_map_all, dict) and fs_map_all

    work_dir_map = fs_map_all.get("work_dir", {})
    raw_work_dir_map = fs_map_all.get("raw_work_dir", {})
    standard_work_dir_map = fs_map_all.get("standard_work_dir", {})

    try:
        metas = _load_metadatas(
            task_path,
            limit=int(task_limit) if task_limit is not None else None,
            task_ids=task_ids,
            persona=persona,
        )
    except (TypeError, ValueError) as e:
        raise SystemExit(f"invalid task selection: {e}") from e
    if not metas:
        raise SystemExit("no tasks found")

    run_fn = _load_agent_run(agent_name)

    summary = _new_summary()
    cases_by_index: Dict[int, Dict[str, Json]] = {}
    started = time.time()
    if task_parallel:
        grouped = {}
    elif workdir_parallel:
        grouped = _group_metas_by_file_system(metas)
    else:
        grouped = {"__all__": list(enumerate(metas))}

    with tqdm(total=len(metas), desc=f"{agent_name}--{model_name}--{run_name}") as pbar:
        if task_parallel:
            max_workers = min(task_parallel_workers, max(1, len(metas)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_task: Dict[Any, Dict[str, Json]] = {}
                for idx, meta in enumerate(metas):
                    fut = executor.submit(
                        _run_one_case,
                        idx=idx,
                        meta=meta,
                        runs_root=runs_root,
                        run_fn=run_fn,
                        prompt_head=prompt_head,
                        prompt_tail=prompt_tail,
                        task_target_output_dir=task_target_output_dir,
                        timeout_sec=timeout_sec,
                        api_provider=api_provider,
                        eval_while_running=eval_while_running,
                        eval_yaml=eval_yaml,
                        work_dir_map=work_dir_map,
                        standard_work_dir_map=standard_work_dir_map,
                        agent_name=agent_name,
                        model_name=model_name,
                        isolated_workdir=True,
                        task_workdir_cleanup=task_workdir_cleanup,
                        prompt_language=prompt_language,
                        prompt_head_by_language=prompt_head_by_language,
                        prompt_tail_by_language=prompt_tail_by_language,
                    )
                    future_to_task[fut] = {
                        "caseId": str(meta.get("id") or ""),
                        "fileSystem": str(meta.get("file_system") or "*"),
                    }
                for fut in as_completed(future_to_task):
                    task_info = future_to_task[fut]
                    try:
                        res = fut.result()
                    except Exception:
                        print(
                            "[parallel-error] "
                            f"case={task_info.get('caseId')} "
                            f"file_system={task_info.get('fileSystem')}",
                            flush=True,
                        )
                        print(traceback.format_exc(), flush=True)
                        raise
                    _merge_summary(summary, res["summary"])
                    if isinstance(res.get("case"), dict):
                        cases_by_index[int(res["index"])] = res["case"]
                    pbar.update(1)
        elif workdir_parallel:
            max_workers = min(5, max(1, len(grouped)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_group: Dict[Any, Dict[str, Json]] = {}
                for group_name, group_items in grouped.items():
                    fut = executor.submit(
                        _run_group,
                        group_items=group_items,
                        runs_root=runs_root,
                        run_fn=run_fn,
                        prompt_head=prompt_head,
                        prompt_tail=prompt_tail,
                        task_target_output_dir=task_target_output_dir,
                        timeout_sec=timeout_sec,
                        api_provider=api_provider,
                        eval_while_running=eval_while_running,
                        eval_yaml=eval_yaml,
                        work_dir_map=work_dir_map,
                        standard_work_dir_map=standard_work_dir_map,
                        agent_name=agent_name,
                        model_name=model_name,
                        isolated_workdir=False,
                        task_workdir_cleanup=task_workdir_cleanup,
                        prompt_language=prompt_language,
                        prompt_head_by_language=prompt_head_by_language,
                        prompt_tail_by_language=prompt_tail_by_language,
                    )
                    future_to_group[fut] = {
                        "groupName": group_name,
                        "caseIds": [str(meta.get("id") or "") for _, meta in group_items],
                    }
                for fut in as_completed(future_to_group):
                    group_info = future_to_group[fut]
                    try:
                        res = fut.result()
                    except Exception:
                        print(
                            "[parallel-error] "
                            f"group={group_info.get('groupName')} "
                            f"cases={group_info.get('caseIds')}",
                            flush=True,
                        )
                        print(traceback.format_exc(), flush=True)
                        raise
                    _merge_summary(summary, res["summary"])
                    for idx, case_out in res["cases"]:
                        cases_by_index[int(idx)] = case_out
                    pbar.update(int(res.get("processed", 0)))
        else:
            for _, group_items in grouped.items():
                res = _run_group(
                    group_items=group_items,
                    runs_root=runs_root,
                    run_fn=run_fn,
                    prompt_head=prompt_head,
                    prompt_tail=prompt_tail,
                    task_target_output_dir=task_target_output_dir,
                    timeout_sec=timeout_sec,
                    api_provider=api_provider,
                    eval_while_running=eval_while_running,
                    eval_yaml=eval_yaml,
                    work_dir_map=work_dir_map,
                    standard_work_dir_map=standard_work_dir_map,
                    agent_name=agent_name,
                    model_name=model_name,
                    isolated_workdir=False,
                    task_workdir_cleanup=task_workdir_cleanup,
                    prompt_language=prompt_language,
                    prompt_head_by_language=prompt_head_by_language,
                    prompt_tail_by_language=prompt_tail_by_language,
                )
                _merge_summary(summary, res["summary"])
                for idx, case_out in res["cases"]:
                    cases_by_index[int(idx)] = case_out
                pbar.update(int(res.get("processed", 0)))

    cases_out = [cases_by_index[idx] for idx in sorted(cases_by_index.keys())]

    finished = time.time()
    cfg2 = json.loads(json.dumps(cfg, ensure_ascii=False, default=str))
    if isinstance(cfg2, dict) and isinstance(cfg2.get("api_provider"), dict):
        cfg2["api_provider"].pop("apiKey", None)
    report = {
        "runsRoot": runs_root,
        "agentId": agent_name,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)),
        "totalDurationMs": int((finished - started) * 1000),
        "summary": summary,
        "cases": cases_out,
        "config": cfg2,
    }
    _write_json(os.path.join(runs_root, "agent_runner_report.json"), report)


if __name__ == "__main__":
    main()
