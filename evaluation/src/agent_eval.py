from tqdm import tqdm
import json
import sys
import os
import time
import random
import urllib.error
import urllib.request
import yaml
import base64
import zipfile
import xml.etree.ElementTree as ET
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


Json = Any

"""
agent_eval.py

本文件用于对“单个任务执行结果目录”进行离线评测与结构化产物生成，主要做两件事：

1) rubric 评测（LLM-as-a-judge）
   - 输入：task_dir 下的 metadata.json（包含 rubrics）、执行 trace（agent.json/result.json/session.jsonl 等）、以及工作目录/输出文件摘录
   - 输出：rubrics_judge--<model_name>.json（每条 rubric 的 passed/evidence/confidence）

2) 依赖图（I/O Dependency Graph）构建
   - 从工具调用/命令执行中抽取“读哪些文件、写哪些文件”，生成一个简化的有向图
   - 输出：dependency_graph--<model_name>.json（nodes/edges）

设计要点：
- 对不同 agent runner 产物做兼容解析（openclaw / batch-test / evaluation_sys agent.json）。
- 评测 prompt 做截断与去噪，避免把海量 trace/文件内容塞进 judge 模型。
- judge 调用带重试与总时间上限，避免 429/限流导致评测卡死。
"""


# -----------------------------------------------------------------------------
# 基础工具：时间/读写文件/配置加载
# -----------------------------------------------------------------------------
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj: Json) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _safe_load_json(path: str) -> Optional[Json]:
    try:
        if not os.path.exists(path) or not os.path.isfile(path):
            return None
        return _read_json(path)
    except Exception:
        return None


def _read_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# -----------------------------------------------------------------------------
# 任务目录探测：判断 runner 类型、猜测工作目录、读取 metadata
# -----------------------------------------------------------------------------
def _detect_agent_kind(task_dir: str) -> str:
    """根据 task_dir 中已有的产物文件，粗略判断该任务是由哪类 runner 生成的。"""
    rj = _safe_load_json(os.path.join(task_dir, "result.json"))
    if isinstance(rj, dict) and isinstance(rj.get("runner"), str):
        return str(rj.get("runner"))
    if os.path.exists(os.path.join(task_dir, "batch_test_report.json")):
        return "batch-test"
    if os.path.exists(os.path.join(task_dir, "session.jsonl")):
        return "openclaw"
    return "unknown"


def _guess_work_dir(task_dir: str) -> str:
    """在 task_dir 下尝试寻找常见的工作目录命名（work/workspace 等），找不到就退化为 task_dir。"""
    for name in ["input_gt", "_work", "work", "workspace"]:
        p = os.path.join(task_dir, name)
        if os.path.isdir(p):
            return p
    return task_dir


def _list_files_under(root: str) -> List[str]:
    """递归列出 root 下所有文件（返回绝对路径）。"""
    out: List[str] = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            out.append(os.path.join(dp, fn))
    return out


def _read_text_excerpt(path: str, max_bytes: int = 80_000) -> Optional[str]:
    """
    尝试读取文本文件前 max_bytes 字节作为摘录（用于评测 prompt 的证据）。
    对图片/Office/压缩包等二进制文件直接返回 None。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".pdf", ".xlsx", ".xls", ".doc", ".docx", ".ppt", ".pptx", ".zip"}:
        return None
    try:
        with open(path, "rb") as f:
            b = f.read(max_bytes)
        return b.decode("utf-8", errors="ignore")
    except Exception:
        return None


def _guess_mime(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return "image/png"
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if ext == ".zip":
        return "application/zip"
    return None


def _read_image_data_url(path: str, *, max_bytes: int = 2_000_000) -> Optional[str]:
    """
    读取图片并编码为 data URL，供多模态 chat/completions 使用。
    - 为防止 prompt 过大，限制最大字节数（默认 2MB）。
    - 若图片过大或读取失败，返回 None。
    """
    mime = _guess_mime(path)
    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return None
    try:
        st = os.stat(path)
        if int(getattr(st, "st_size", 0) or 0) > max_bytes:
            return None
        with open(path, "rb") as f:
            b = f.read(max_bytes + 1)
        if len(b) > max_bytes:
            return None
        enc = base64.b64encode(b).decode("ascii")
        return f"data:{mime};base64,{enc}"
    except Exception:
        return None


def _xml_text_from_docx(docx_path: str, *, max_chars: int = 120_000) -> Optional[str]:
    """
    从 .docx（本质是 zip）里抽取纯文本（尽量不依赖第三方包）。
    只读取 word/document.xml，拼接所有 <w:t> 节点文本。
    """
    try:
        with zipfile.ZipFile(docx_path) as zf:
            with zf.open("word/document.xml") as f:
                data = f.read()
    except Exception:
        return None
    try:
        root = ET.fromstring(data)
    except Exception:
        return None

    # docx 的 XML 带 namespace；这里用 “endswith” 做弱匹配，避免硬编码 ns
    parts: List[str] = []
    for el in root.iter():
        tag = str(getattr(el, "tag", "") or "")
        if tag.endswith("}t") or tag == "t":
            if el.text:
                parts.append(str(el.text))
                if sum(len(x) for x in parts) > max_chars:
                    break
        # 段落换行（w:p）
        if tag.endswith("}p") or tag == "p":
            parts.append("\n")
            if sum(len(x) for x in parts) > max_chars:
                break
    txt = "".join(parts).strip()
    return txt[:max_chars] if txt else None


def _xml_text_from_xlsx(xlsx_path: str, *, max_chars: int = 120_000) -> Optional[str]:
    """
    从 .xlsx（zip + sheet xml）里抽取可读文本。
    这里采用轻量策略：
    - 尝试读取 xl/sharedStrings.xml 作为字符串池
    - 读取前几个 sheet，提取 cell 的字符串/数值，输出为 TSV 风格文本

    目标是“给评测模型提供内容线索”，不追求完全还原 Excel。
    """
    try:
        zf = zipfile.ZipFile(xlsx_path)
    except Exception:
        return None

    shared: List[str] = []
    try:
        with zf.open("xl/sharedStrings.xml") as f:
            root = ET.fromstring(f.read())
        for si in root.iter():
            tag = str(getattr(si, "tag", "") or "")
            if tag.endswith("}t") or tag == "t":
                if si.text:
                    shared.append(str(si.text))
    except Exception:
        shared = []

    # 收集 sheet 路径（按名字排序，控制数量）
    sheet_paths = sorted([n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")])[:3]
    out_lines: List[str] = []
    try:
        for sp in sheet_paths:
            out_lines.append(f"# SHEET {os.path.basename(sp)}")
            with zf.open(sp) as f:
                root = ET.fromstring(f.read())

            row_vals: Dict[str, List[str]] = {}
            for c in root.iter():
                tag = str(getattr(c, "tag", "") or "")
                if not (tag.endswith("}c") or tag == "c"):
                    continue
                cell_ref = c.attrib.get("r") or ""
                cell_type = c.attrib.get("t") or ""
                v_text = None
                for child in list(c):
                    t2 = str(getattr(child, "tag", "") or "")
                    if t2.endswith("}v") or t2 == "v":
                        v_text = child.text
                        break
                if v_text is None:
                    continue
                val = str(v_text)
                if cell_type == "s":
                    try:
                        idx = int(val)
                        val2 = shared[idx] if 0 <= idx < len(shared) else ""
                    except Exception:
                        val2 = ""
                    val = val2
                # 简单按行聚合：取 A1 中数字部分作为 row key（比如 A12 -> 12）
                row_key = "".join([ch for ch in cell_ref if ch.isdigit()]) or "0"
                row_vals.setdefault(row_key, []).append(val)

            # 输出前 N 行
            for rk in sorted(row_vals.keys(), key=lambda x: int(x) if x.isdigit() else 10**9)[:50]:
                out_lines.append("\t".join([x for x in row_vals[rk] if x is not None]))
                if sum(len(x) for x in out_lines) > max_chars:
                    break
            if sum(len(x) for x in out_lines) > max_chars:
                break
    finally:
        try:
            zf.close()
        except Exception:
            pass

    txt = "\n".join(out_lines).strip()
    return txt[:max_chars] if txt else None


def _text_from_xls(xls_path: str, *, max_chars: int = 120_000) -> Optional[str]:
    """
    .xls 抽取策略（依赖第三方库 xlrd）：
    - 使用 xlrd 读取工作簿/工作表
    - 逐行逐列拼接为 TSV 风格文本（限制 sheet 数、行数、总字符）

    说明：xlrd >= 2.0 仅支持 .xls（这正好符合需求），不支持 .xlsx。
    """
    try:
        import xlrd  # type: ignore
    except Exception:
        return None
    try:
        wb = xlrd.open_workbook(xls_path, on_demand=True)
    except Exception:
        return None

    out_lines: List[str] = []
    try:
        for si in range(min(getattr(wb, "nsheets", 0) or 0, 3)):
            try:
                sh = wb.sheet_by_index(si)
            except Exception:
                continue
            out_lines.append(f"# SHEET {getattr(sh, 'name', f'sheet{si}')}")
            nrows = int(getattr(sh, "nrows", 0) or 0)
            ncols = int(getattr(sh, "ncols", 0) or 0)
            for r in range(min(nrows, 50)):
                row = []
                for c in range(min(ncols, 30)):
                    try:
                        v = sh.cell_value(r, c)
                    except Exception:
                        v = ""
                    s = str(v) if v is not None else ""
                    row.append(s)
                out_lines.append("\t".join(row).rstrip())
                if sum(len(x) for x in out_lines) > max_chars:
                    break
            if sum(len(x) for x in out_lines) > max_chars:
                break
    finally:
        try:
            wb.release_resources()
        except Exception:
            pass

    txt = "\n".join(out_lines).strip()
    return txt[:max_chars] if txt else None


def _text_from_pptx(pptx_path: str, *, max_chars: int = 120_000) -> Optional[str]:
    """
    .pptx 抽取策略（依赖第三方库 python-pptx）：
    - 用 Presentation 解析每个 slide
    - 收集 shape.text_frame 中的文字
    """
    try:
        from pptx import Presentation  # type: ignore
    except Exception:
        return None
    try:
        prs = Presentation(pptx_path)
    except Exception:
        return None

    out: List[str] = []
    for i, slide in enumerate(getattr(prs, "slides", []) or []):
        out.append(f"# SLIDE {i + 1}")
        for shape in getattr(slide, "shapes", []) or []:
            try:
                if not getattr(shape, "has_text_frame", False):
                    continue
                tf = shape.text_frame
                if tf is None:
                    continue
                t = tf.text or ""
                if t.strip():
                    out.append(t.strip())
            except Exception:
                continue
        if sum(len(x) for x in out) > max_chars:
            break
    txt = "\n".join(out).strip()
    return txt[:max_chars] if txt else None


def _text_from_pdf(pdf_path: str, *, max_chars: int = 120_000) -> Optional[str]:
    """
    .pdf 抽取策略：
    1) 优先用 pypdf（纯 Python，轻量）
    2) 若不可用再尝试 pdfminer.six（文本抽取更强，但更重）
    """
    # pypdf
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        PdfReader = None  # type: ignore
    if PdfReader is not None:
        try:
            reader = PdfReader(pdf_path)
            parts: List[str] = []
            for i, page in enumerate(getattr(reader, "pages", []) or []):
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t.strip():
                    parts.append(f"# PAGE {i + 1}\n{t.strip()}")
                if sum(len(x) for x in parts) > max_chars:
                    break
            txt = "\n\n".join(parts).strip()
            if txt:
                return txt[:max_chars]
        except Exception:
            pass

    # pdfminer.six
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:
        return None
    try:
        txt = extract_text(pdf_path) or ""
        txt = txt.strip()
        return txt[:max_chars] if txt else None
    except Exception:
        return None


def _text_from_doc(doc_path: str, *, max_chars: int = 120_000) -> Optional[str]:
    """
    .doc 抽取策略（依赖第三方库 textract）：
    - textract.process(...) 会在不同平台调用可用后端提取文本（可能需要系统依赖）
    - 若环境缺依赖/提取失败，返回 None

    说明：.doc 是二进制格式，不建议自己手写解析器。
    """
    try:
        import textract  # type: ignore
    except Exception:
        return None
    try:
        b = textract.process(doc_path)
        if isinstance(b, (bytes, bytearray)):
            s = b.decode("utf-8", errors="ignore")
        else:
            s = str(b)
        s = s.strip()
        return s[:max_chars] if s else None
    except Exception:
        return None


def _zip_tree_and_excerpts(
    zip_path: str,
    *,
    max_files: int = 40,
    max_entry_bytes: int = 200_000,
    max_total_chars: int = 120_000,
) -> Optional[str]:
    """
    .zip 抽取策略（内置 zipfile）：
    - 输出文件树（前 max_files 个条目）
    - 对其中“可读文本类/可解析 office/pdf”的小文件，递归抽取 excerpt（限制单文件大小与总字符）

    注意：为了安全与性能，这里只做“读取压缩包内容”并不会解压到工作目录；
    对需要解析的条目，会写入临时文件后复用现有解析器（docx/xlsx/pptx/pdf）。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception:
        return None

    def is_dir_name(n: str) -> bool:
        return n.endswith("/") or n.endswith("\\")

    lines: List[str] = []
    tmp_paths: List[str] = []
    try:
        names = [n for n in zf.namelist() if n and not is_dir_name(n)]
        names = sorted(names)[:max_files]
        lines.append("# ZIP TREE")
        for n in names:
            lines.append(n)
        lines.append("")
        lines.append("# ZIP EXCERPTS")

        for n in names:
            try:
                info = zf.getinfo(n)
                if int(getattr(info, "file_size", 0) or 0) > max_entry_bytes:
                    continue
            except Exception:
                continue

            ext = os.path.splitext(n)[1].lower()
            # 先处理“可直接文本读取”的条目
            if ext in {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}:
                try:
                    data = zf.read(n)
                    s = data.decode("utf-8", errors="ignore").strip()
                    if s:
                        lines.append(f"## {n}")
                        lines.append(s[:4000])
                        lines.append("")
                except Exception:
                    continue
            # 对 office/pdf：写临时文件后复用解析器（限制大小）
            elif ext in {".docx", ".xlsx", ".xls", ".pptx", ".pdf", ".doc"}:
                try:
                    data = zf.read(n)
                except Exception:
                    continue
                try:
                    suf = ext if ext else ".bin"
                    fd, tp = tempfile.mkstemp(prefix="agent_eval_zip_", suffix=suf)
                    os.close(fd)
                    with open(tp, "wb") as f:
                        f.write(data)
                    tmp_paths.append(tp)
                except Exception:
                    continue

                txt = None
                note = None
                if ext == ".docx":
                    txt = _xml_text_from_docx(tp, max_chars=20_000)
                    note = "docx(zip+xml)"
                elif ext == ".xlsx":
                    txt = _xml_text_from_xlsx(tp, max_chars=20_000)
                    note = "xlsx(zip+xml)"
                elif ext == ".xls":
                    txt = _text_from_xls(tp, max_chars=20_000)
                    note = "xls(xlrd)"
                elif ext == ".pptx":
                    txt = _text_from_pptx(tp, max_chars=20_000)
                    note = "pptx(python-pptx)"
                elif ext == ".pdf":
                    txt = _text_from_pdf(tp, max_chars=20_000)
                    note = "pdf(pypdf/pdfminer)"
                elif ext == ".doc":
                    txt = _text_from_doc(tp, max_chars=20_000)
                    note = "doc(textract)"

                if txt:
                    lines.append(f"## {n} ({note})")
                    lines.append(txt[:4000])
                    lines.append("")

            if sum(len(x) for x in lines) > max_total_chars:
                break

        txt2 = "\n".join(lines).strip()
        return txt2[:max_total_chars] if txt2 else None
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass
        try:
            zf.close()
        except Exception:
            pass


def _read_rich_excerpt(path: str, *, max_bytes: int = 80_000, max_chars: int = 120_000) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    读取“可用于评测”的内容证据：
    - 对纯文本：返回 excerpt
    - 对 docx/xlsx：解析后返回 excerpt
    - 对 xls/doc/pdf/pptx/zip：尝试用第三方库或轻量解析器提取文本
    - 对图片：返回 image_data_url（excerpt 为 None）

    返回 (excerpt_text, image_data_url, note)
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        url = _read_image_data_url(path)
        if url:
            return None, url, None
        return None, None, "image too large or unreadable"
    if ext == ".docx":
        txt = _xml_text_from_docx(path, max_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "docx parse failed")
    if ext == ".xlsx":
        txt = _xml_text_from_xlsx(path, max_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "xlsx parse failed")
    if ext == ".xls":
        txt = _text_from_xls(path, max_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "xls parse failed (need xlrd)")
    if ext == ".pptx":
        txt = _text_from_pptx(path, max_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "pptx parse failed (need python-pptx)")
    if ext == ".pdf":
        txt = _text_from_pdf(path, max_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "pdf parse failed (need pypdf/pdfminer.six)")
    if ext == ".doc":
        txt = _text_from_doc(path, max_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "doc parse failed (need textract + deps)")
    if ext == ".zip":
        txt = _zip_tree_and_excerpts(path, max_total_chars=max_chars)
        return (txt, None, None) if txt else (None, None, "zip parse failed")
    # 默认走文本摘录
    return _read_text_excerpt(path, max_bytes=max_bytes), None, None


def _normalize_filename_key(name: str) -> str:
    """用于弱匹配文件名：去空白/引号/括号等噪声后转小写。"""
    s = str(name or "")
    s = s.replace(" ", "").replace("　", "").replace("\t", "").replace("\n", "")
    s = s.replace("’", "").replace("'", "").replace(""", "").replace(""", "").replace('"', "")
    s = s.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return s.lower()


def _extract_expected_outputs(meta: Dict[str, Json]) -> List[str]:
    outs: List[str] = []
    of = meta.get("output_files")
    if isinstance(of, list):
        outs.extend([str(x).strip() for x in of if isinstance(x, str) and str(x).strip()])
    if isinstance(meta.get("output_file"), str) and str(meta.get("output_file")).strip():
        outs.append(str(meta.get("output_file")).strip())
    return sorted(set([x for x in outs if x]))


def _load_task_metadata(task_dir: str) -> Optional[Dict[str, Json]]:
    """
    读取任务 metadata.json（优先 input_gt/metadata.json），并要求包含 rubrics 字段。
    注意：评测逻辑强依赖 rubrics，否则无法进行 judge。
    """
    candidates = [
        os.path.join(task_dir, "input_gt", "metadata.json"),
        os.path.join(task_dir, "metadata.json"),
    ]
    for p in candidates:
        obj = _safe_load_json(p)
        if isinstance(obj, dict) and isinstance(obj.get("rubrics"), list):
            return obj
    return None


def _collect_outputs(task_dir: str, meta: Dict[str, Json]) -> Dict[str, Json]:
    """
    收集任务“输出证据”：
    - workDir：猜测的工作目录
    - expectedOutputs：metadata 中声明的期望输出文件名
    - files：从 task_dir/output 以及 workDir 中收集“疑似输出”的文件列表，并附带 size/excerpt

    这里的策略偏“召回优先”：
    - 文件名命中 expectedOutputs 的一定收集
    - 不是输入文件（data_manifest）的一般也会被视为候选输出
    - 但会做数量上限（最多 50）与文本摘录大小限制，避免 prompt 过大
    """
    work_dir = _guess_work_dir(task_dir)
    expected_outputs = _extract_expected_outputs(meta)

    inputs: List[str] = []
    dm = meta.get("data_manifest")
    if isinstance(dm, list):
        for it in dm:
            if not isinstance(it, dict):
                continue
            fn = it.get("filename")
            if isinstance(fn, str) and fn.strip():
                inputs.append(fn.strip())

    input_name_keys = set([_normalize_filename_key(x) for x in inputs])
    expected_name_keys = set([_normalize_filename_key(x) for x in expected_outputs])

    output_candidates: List[str] = []
    output_dir = os.path.join(task_dir, "output")
    if os.path.isdir(output_dir):
        output_candidates.extend(_list_files_under(output_dir))

    if os.path.isdir(work_dir):
        for p in _list_files_under(work_dir):
            if os.path.basename(p) == "metadata.json":
                continue
            k = _normalize_filename_key(os.path.basename(p))
            if k in expected_name_keys:
                output_candidates.append(p)
            elif k not in input_name_keys:
                output_candidates.append(p)

    uniq = []
    seen = set()
    for p in output_candidates:
        ap = os.path.abspath(p)
        if ap not in seen and os.path.isfile(ap):
            seen.add(ap)
            uniq.append(ap)

    files: List[Json] = []
    for ap in sorted(uniq)[:50]:
        try:
            st = os.stat(ap)
            excerpt, image_data_url, note = _read_rich_excerpt(ap)
            files.append(
                {
                    "path": ap,
                    "relToWorkDir": os.path.relpath(ap, work_dir) if os.path.commonpath([work_dir, ap]) == os.path.abspath(work_dir) else None,
                    "sizeBytes": int(st.st_size),
                    "excerpt": excerpt,
                    "mime": _guess_mime(ap),
                    "hasImage": True if image_data_url else False,
                    # image data 不直接放进 JSON prompt（太大）；仅在构建 messages 时附加
                    "_imageDataUrl": image_data_url,
                    "note": note,
                }
            )
        except Exception:
            continue

    return {
        "workDir": work_dir,
        "expectedOutputs": expected_outputs,
        "files": files,
    }


def _extract_trace(task_dir: str, kind: str) -> Dict[str, Json]:
    """
    兼容多种 runner 的 trace 解析入口。
    优先读取 evaluation_sys 的 agent.json（trace.executionTrace / trace.llm / trace.outputs 等），
    否则根据 kind 退化到 openclaw/batch-test 的产物格式。
    """
    agent_json = _safe_load_json(os.path.join(task_dir, "agent.json"))
    if isinstance(agent_json, dict):
        trace = agent_json.get("trace")
        if isinstance(trace, dict):
            execution_trace = trace.get("executionTrace") if isinstance(trace.get("executionTrace"), list) else []
            trajectory = trace.get("trajectory") if isinstance(trace.get("trajectory"), list) else []
            llm_info = trace.get("llm") if isinstance(trace.get("llm"), dict) else {}
            llm_calls = llm_info.get("calls") if isinstance(llm_info.get("calls"), list) else []
            outputs = trace.get("outputs") if isinstance(trace.get("outputs"), dict) else {}
            prompt_info = trace.get("prompt") if isinstance(trace.get("prompt"), dict) else {}
            return {
                "executionTrace": execution_trace,
                "trajectory": trajectory,
                "llmCalls": llm_calls,
                "outputs": outputs,
                "prompt": prompt_info,
                "llm": llm_info,
            }
    rj = _safe_load_json(os.path.join(task_dir, "result.json"))
    if kind == "openclaw" and isinstance(rj, dict):
        trace = rj.get("trace")
        if isinstance(trace, dict):
            tc = trace.get("toolCalls")
            tos = trace.get("textOutputs")
            return {
                "toolCalls": tc if isinstance(tc, list) else [],
                "textOutputs": tos if isinstance(tos, list) else [],
                "metrics": rj.get("metrics") if isinstance(rj.get("metrics"), dict) else None,
            }
    if kind == "batch-test" and isinstance(rj, dict):
        tr = rj.get("taskResult")
        if isinstance(tr, dict):
            return {
                "toolCalls": tr.get("toolCalls") if isinstance(tr.get("toolCalls"), list) else [],
                "textOutputs": tr.get("textOutputs") if isinstance(tr.get("textOutputs"), list) else [],
                "llmCalls": tr.get("llmCalls") if isinstance(tr.get("llmCalls"), list) else [],
            }
    return {"toolCalls": [], "textOutputs": []}


def _json_first_object(text: str) -> Optional[Json]:
    """从一段文本中抓取第一个 JSON 值（对象或数组），用于容错解析模型输出。"""
    s = str(text or "").lstrip()
    if not s:
        return None
    pos1 = s.find("{")
    pos2 = s.find("[")
    pos = pos1 if pos2 == -1 else (pos2 if pos1 == -1 else min(pos1, pos2))
    if pos == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[pos:])
        return obj
    except Exception:
        return None


def _is_rate_limited(err_text: str) -> bool:
    """粗略判断错误信息是否是限流/429 类问题（用于触发重试逻辑）。"""
    s = str(err_text or "")
    if not s:
        return False
    s_low = s.lower()
    return ("429" in s_low) or ("too many requests" in s_low) or ("tokens per minute" in s_low) or ("tpm" in s_low)


def _chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Json]],
    timeout_s: int = 120,
    max_retries: int = 10,
    total_timeout_s: float = 200.0,
) -> Tuple[Optional[Dict[str, Json]], Optional[Dict[str, Json]], str]:
    """
    调用 OpenAI 兼容的 /chat/completions（使用 urllib，避免额外依赖）。

    返回：(full_response_json, usage_dict_or_none, assistant_content_str)

    重试策略：
    - 若 HTTPError 且判断为限流（429/TPM），使用指数退避 + jitter 重试
    - 总重试窗口由 total_timeout_s 控制，超过则直接返回失败
    """
    url = str(base_url or "").rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    
    start_time = time.time()
    last_err = ""
    
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            last_err = f"HTTPError {getattr(e, 'code', '')}: {body[:4000]}"
            if _is_rate_limited(last_err):
                elapsed = time.time() - start_time
                if elapsed >= total_timeout_s:
                    return None, None, f"Rate limit retry timeout after {elapsed:.1f}s: {last_err}"
                base_delay = min(10.0, 1.0 * (2 ** attempt))
                jitter = random.random() * 0.5
                delay = base_delay + jitter
                remaining = total_timeout_s - elapsed
                if remaining <= 0:
                    return None, None, f"Rate limit retry timeout: {last_err}"
                time.sleep(min(delay, remaining))
                continue
            return None, None, last_err
        except Exception as e:
            last_err = str(e)[:4000]
            elapsed = time.time() - start_time
            if elapsed >= total_timeout_s:
                return None, None, f"Retry timeout after {elapsed:.1f}s: {last_err}"
            base_delay = min(10.0, 1.0 * (2 ** attempt))
            jitter = random.random() * 0.5
            delay = base_delay + jitter
            remaining = total_timeout_s - elapsed
            if remaining <= 0:
                return None, None, f"Retry timeout: {last_err}"
            time.sleep(min(delay, remaining))
            continue

        obj = _json_first_object(body)
        if not isinstance(obj, dict):
            return None, None, body[:4000]
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
        choice0 = None
        cs = obj.get("choices")
        if isinstance(cs, list) and cs and isinstance(cs[0], dict):
            choice0 = cs[0]
        msg = None
        if isinstance(choice0, dict):
            m = choice0.get("message")
            if isinstance(m, dict):
                msg = m
        return obj, usage, (msg.get("content") if isinstance(msg, dict) and isinstance(msg.get("content"), str) else "")
    
    return None, None, f"Max retries exceeded: {last_err}"


def _truncate_str(s: str, max_len: int = 2000) -> str:
    """对长字符串做截断，避免评测 prompt/产物文件过大。"""
    if not isinstance(s, str):
        return s
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...[truncated]"


def _truncate_dict_values(d: Dict[str, Json], max_str_len: int = 2000, max_depth: int = 3, current_depth: int = 0) -> Dict[str, Json]:
    """递归截断 dict 内的字符串/嵌套结构，限制深度，避免 prompt 爆炸。"""
    if current_depth >= max_depth:
        return "[max depth reached]"
    if not isinstance(d, dict):
        return d
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _truncate_str(v, max_str_len)
        elif isinstance(v, dict):
            result[k] = _truncate_dict_values(v, max_str_len, max_depth, current_depth + 1)
        elif isinstance(v, list):
            result[k] = _truncate_list_items(v, max_str_len, max_depth, current_depth + 1)
        else:
            result[k] = v
    return result


def _truncate_list_items(lst: List[Json], max_str_len: int = 2000, max_depth: int = 3, current_depth: int = 0) -> List[Json]:
    """递归截断 list 内的字符串/嵌套结构，限制深度。"""
    if current_depth >= max_depth:
        return ["[max depth reached]"]
    if not isinstance(lst, list):
        return lst
    result = []
    for i, item in enumerate(lst):
        if isinstance(item, str):
            result.append(_truncate_str(item, max_str_len))
        elif isinstance(item, dict):
            result.append(_truncate_dict_values(item, max_str_len, max_depth, current_depth + 1))
        elif isinstance(item, list):
            result.append(_truncate_list_items(item, max_str_len, max_depth, current_depth + 1))
        else:
            result.append(item)
    return result


def _truncate_trace_item(item: Dict[str, Json], max_str_len: int = 2000) -> Dict[str, Json]:
    """
    对 trace 单条事件做“字段级”截断。
    仅对可能很长的字段（stdout/stderr/prompt/tool output 等）做截断。
    """
    if not isinstance(item, dict):
        return item
    result = {}
    for k, v in item.items():
        if k in {"content", "text", "output", "input", "excerpt", "arguments", "result", "response", "stdout", "stderr", "body", "prompt"}:
            if isinstance(v, str):
                result[k] = _truncate_str(v, max_str_len)
            elif isinstance(v, dict):
                result[k] = _truncate_dict_values(v, max_str_len, max_depth=2)
            elif isinstance(v, list):
                result[k] = _truncate_list_items(v, max_str_len, max_depth=2)
            else:
                result[k] = v
        else:
            result[k] = v
    return result


def _truncate_trace(trace: Dict[str, Json], max_items: int = 50, max_str_len: int = 2000) -> Dict[str, Json]:
    """对 trace 的每个列表字段做数量截断，并对内容字段做长度截断。"""
    if not isinstance(trace, dict):
        return trace
    result = {}
    for k, v in trace.items():
        if isinstance(v, list):
            truncated_list = []
            for i, item in enumerate(v[:max_items]):
                if isinstance(item, dict):
                    truncated_list.append(_truncate_trace_item(item, max_str_len))
                elif isinstance(item, str):
                    truncated_list.append(_truncate_str(item, max_str_len))
                else:
                    truncated_list.append(item)
            if len(v) > max_items:
                truncated_list.append(f"...[truncated {len(v) - max_items} more items]")
            result[k] = truncated_list
        elif isinstance(v, dict):
            result[k] = _truncate_dict_values(v, max_str_len, max_depth=2)
        elif isinstance(v, str):
            result[k] = _truncate_str(v, max_str_len)
        else:
            result[k] = v
    return result


def _truncate_outputs(outputs: List[Json], max_files: int = 10, max_str_len: int = 2000) -> List[Json]:
    """对输出文件列表做截断（数量上限 + excerpt 字段截断）。"""
    if not isinstance(outputs, list):
        return outputs
    result = []
    for i, f in enumerate(outputs[:max_files]):
        if not isinstance(f, dict):
            result.append(f)
            continue
        truncated_file = {}
        for k, v in f.items():
            if k == "excerpt":
                truncated_file[k] = _truncate_str(v, max_str_len)
            else:
                truncated_file[k] = v
        result.append(truncated_file)
    if len(outputs) > max_files:
        result.append({"note": f"...[truncated {len(outputs) - max_files} more files]"})
    return result


def _build_grading_prompt(
    *,
    task_id: str,
    meta: Dict[str, Json],
    outputs: Dict[str, Json],
    trace: Dict[str, Json],
    max_trace_items: int = 30,
    max_str_len: int = 2000,
    max_output_files: int = 10,
) -> str:
    """
    构造“评测模型”的 user prompt。

    输入包含：
    - 任务描述、步骤、expectedOutputs、工作目录
    - outputs（候选输出文件的 size/excerpt）
    - traceSummary（截断后的 trace/llmCalls/toolCalls 等）
    - rubrics（逐条 rubric + 类型/难度信息）

    输出要求评测模型返回固定 JSON 格式，便于后续解析落盘。
    """
    rubrics = meta.get("rubrics") if isinstance(meta.get("rubrics"), list) else []
    rubric_types = meta.get("rubric_types") if isinstance(meta.get("rubric_types"), list) else []
    rubric_diffs = meta.get("rubric_diffs") if isinstance(meta.get("rubric_diffs"), list) else []

    items = []
    for i, r in enumerate(rubrics):
        if not isinstance(r, str):
            continue
        items.append(
            {
                "index": i,
                "rubric": r,
                "rubricType": rubric_types[i] if i < len(rubric_types) and isinstance(rubric_types[i], str) else None,
                "rubricDiff": rubric_diffs[i] if i < len(rubric_diffs) and isinstance(rubric_diffs[i], str) else None,
            }
        )

    truncated_outputs = _truncate_outputs(outputs.get("files", []), max_output_files, max_str_len)
    truncated_trace = _truncate_trace(trace, max_trace_items, max_str_len)

    prompt = {
        "taskId": task_id,
        "task": meta.get("task"),
        "steps": meta.get("steps"),
        "expectedOutputs": outputs.get("expectedOutputs"),
        "workDir": outputs.get("workDir"),
        "outputs": truncated_outputs,
        "traceSummary": truncated_trace,
        "rubrics": items,
    }

    return (
        "请你作为严格评测员，基于给定 JSON 中的 task/outputs/traceSummary 来判断每条 rubrics 是否满足。\n"
        "要求：\n"
        "1) 只能依据给定证据，不要凭空假设。\n"
        "2) 对于文件命名、寻找文件过程等细微错误可以适当放松判断标准。\n"
        "3) 每条 rubric 输出 passed(true/false) + evidence(字符串，引用到具体文件/片段或工具调用) + confidence(0-1)。\n"
        "4) 输出必须是 JSON 对象，格式：{ \"rubrics\": [ {\"index\":0,\"passed\":true,\"confidence\":0.8,\"evidence\":\"...\"}, ... ] }\n"
        "5) 如果证据不足，请 passed=false 且 evidence 写明缺失证据。\n\n"
        + json.dumps(prompt, ensure_ascii=False)
    )


def _build_grading_messages(
    *,
    task_id: str,
    meta: Dict[str, Json],
    outputs: Dict[str, Json],
    trace: Dict[str, Json],
    max_trace_items: int = 30,
    max_str_len: int = 2000,
    max_output_files: int = 10,
    max_images: int = 3,
) -> List[Dict[str, Json]]:
    """
    构造用于 /chat/completions 的 messages。
    - 第一块仍然是纯文本（包含 JSON 证据）。
    - 若 outputs.files 中包含图片，会以 OpenAI 兼容的 image_url block 追加到 user content 中。
      这要求评测模型支持多模态；若模型不支持，调用可能失败（可在上层做降级）。
    """
    user_text = _build_grading_prompt(
        task_id=task_id,
        meta=meta,
        outputs=outputs,
        trace=trace,
        max_trace_items=max_trace_items,
        max_str_len=max_str_len,
        max_output_files=max_output_files,
    )

    user_blocks: List[Dict[str, Json]] = [{"type": "text", "text": user_text}]
    imgs: List[str] = []
    for f in outputs.get("files", []) if isinstance(outputs.get("files"), list) else []:
        if not isinstance(f, dict):
            continue
        u = f.get("_imageDataUrl")
        if isinstance(u, str) and u.startswith("data:image/"):
            imgs.append(u)
            if len(imgs) >= max_images:
                break
    for u in imgs:
        user_blocks.append({"type": "image_url", "image_url": {"url": u}})

    return [
        {"role": "system", "content": "你是一个严格的任务评测员。"},
        {"role": "user", "content": user_blocks},
    ]


def _resolve_path(work_dir: str, p: str) -> str:
    """把可能是相对路径的 p 解析到 work_dir 下（用于依赖图节点归一化）。"""
    s = str(p or "").strip()
    if not s:
        return ""
    if os.path.isabs(s):
        return os.path.abspath(s)
    return os.path.abspath(os.path.join(work_dir, s))


def _node_id(work_dir: str, abs_path: str) -> str:
    """把绝对路径映射为依赖图节点 id：优先用相对 work_dir 的相对路径，否则退化为 basename。"""
    ap = os.path.abspath(abs_path)
    wd = os.path.abspath(work_dir)
    try:
        if os.path.commonpath([wd, ap]) == wd:
            rel = os.path.relpath(ap, wd)
            rel = rel.replace("\\", "/")
            return rel
    except Exception:
        pass
    return os.path.basename(ap)


def _extract_openclaw_toolcalls(task_dir: str) -> List[Dict[str, Json]]:
    """
    从 openclaw 的产物中抽取工具调用列表。
    优先 result.json.trace.toolCalls，否则解析 session.jsonl（assistant/toolResult）。
    """
    rj = _safe_load_json(os.path.join(task_dir, "result.json"))
    if isinstance(rj, dict):
        tr = rj.get("trace")
        if isinstance(tr, dict) and isinstance(tr.get("toolCalls"), list):
            out = []
            for tc in tr.get("toolCalls"):
                if isinstance(tc, dict) and isinstance(tc.get("tool"), str):
                    out.append(tc)
            return out
    sess = os.path.join(task_dir, "session.jsonl")
    if not os.path.exists(sess) or not os.path.isfile(sess):
        return []
    tool_calls: List[Dict[str, Json]] = []
    idx: Dict[str, Dict[str, Json]] = {}
    try:
        with open(sess, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if not isinstance(evt, dict) or evt.get("type") != "message":
                    continue
                msg = evt.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                ts = evt.get("timestamp")
                if role == "assistant":
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "toolCall":
                            continue
                        call_id = part.get("id")
                        name = part.get("name")
                        args = part.get("arguments")
                        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                            continue
                        entry: Dict[str, Json] = {
                            "tool": name,
                            "callID": call_id,
                            "timestamp": ts,
                            "input": args if isinstance(args, dict) else {},
                            "state": "running",
                            "output": None,
                        }
                        tool_calls.append(entry)
                        idx[call_id] = entry
                elif role == "toolResult":
                    call_id = msg.get("toolCallId")
                    if not isinstance(call_id, str) or not call_id:
                        continue
                    entry = idx.get(call_id)
                    if entry is None:
                        continue
                    entry["state"] = "completed"
                    entry["output"] = msg.get("details") if msg.get("details") is not None else msg.get("content")
    except Exception:
        return []
    return tool_calls


def _extract_batch_toolcalls(task_dir: str) -> List[Dict[str, Json]]:
    """从 batch-test 产物中抽取 toolCalls（result.json 或 batch_test_report.json）。"""
    rj = _safe_load_json(os.path.join(task_dir, "result.json"))
    if isinstance(rj, dict):
        tr = rj.get("taskResult")
        if isinstance(tr, dict) and isinstance(tr.get("toolCalls"), list):
            out = []
            for tc in tr.get("toolCalls"):
                if isinstance(tc, dict) and isinstance(tc.get("tool"), str):
                    out.append(tc)
            return out
    rep = _safe_load_json(os.path.join(task_dir, "batch_test_report.json"))
    if isinstance(rep, dict) and isinstance(rep.get("tasks"), list) and rep.get("tasks"):
        tr = rep.get("tasks")[0]
        if isinstance(tr, dict) and isinstance(tr.get("toolCalls"), list):
            out = []
            for tc in tr.get("toolCalls"):
                if isinstance(tc, dict) and isinstance(tc.get("tool"), str):
                    out.append(tc)
            return out
    return []


def _extract_execution_trace_toolcalls(task_dir: str) -> List[Dict[str, Json]]:
    """
    从 evaluation_sys 的 agent.json.trace.executionTrace 中抽取 tool 事件。

    注意：这里假设 tool 事件使用 tool_name/tool_input/tool_output 字段。
    如果你的 executionTrace 使用的是另一套字段命名（如 tool/callID/input/output），
    这里可能抽取不到 toolCalls，从而影响依赖图构建的召回率。
    """
    agent_json = _safe_load_json(os.path.join(task_dir, "agent.json"))
    if not isinstance(agent_json, dict):
        return []
    trace = agent_json.get("trace")
    if not isinstance(trace, dict):
        return []
    execution_trace = trace.get("executionTrace")
    if not isinstance(execution_trace, list):
        return []
    
    tool_calls: List[Dict[str, Json]] = []
    for item in execution_trace:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "tool":
            continue
        
        tool_name = item.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        
        tool_input = item.get("tool_input") if isinstance(item.get("tool_input"), dict) else {}
        tool_output = item.get("tool_output") if isinstance(item.get("tool_output"), dict) else {}
        
        tool_calls.append({
            "tool": tool_name,
            "input": tool_input,
            "output": tool_output,
        })
    
    return tool_calls


def _build_dependency_graph(task_dir: str) -> Dict[str, Json]:
    """
    从工具调用抽取文件读写依赖，构建简化的 I/O 依赖图：
    - node：文件（相对 workDir 的路径）
    - edge：读 -> 写 的依赖关系

    抽取策略：
    - read/read_file/image 认为是“读”
    - write/write_file 认为是“写”
    - exec 尝试用简单规则从命令行参数中推断读写文件（包含重定向 > / >>）
    """
    kind = _detect_agent_kind(task_dir)
    work_dir = _guess_work_dir(task_dir)
    
    tool_calls = _extract_execution_trace_toolcalls(task_dir)
    if not tool_calls:
        if kind == "openclaw":
            tool_calls = _extract_openclaw_toolcalls(task_dir)
        elif kind == "batch-test":
            tool_calls = _extract_batch_toolcalls(task_dir)
        else:
            tool_calls = _extract_batch_toolcalls(task_dir) or _extract_openclaw_toolcalls(task_dir)

    nodes: set = set()
    edges: set = set()
    active_inputs: set = set()

    def add_edge(a: str, b: str) -> None:
        if not a or not b or a == b:
            return
        edges.add((a, b))

    for tc in tool_calls:
        tool = tc.get("tool")
        inp = tc.get("input") if isinstance(tc.get("input"), dict) else {}
        out = tc.get("output") if isinstance(tc.get("output"), dict) else {}

        if tool in {"read", "read_file", "image"}:
            p = inp.get("path") if tool != "image" else inp.get("image")
            if isinstance(p, str) and p.strip():
                ap = _resolve_path(work_dir, p)
                nid = _node_id(work_dir, ap)
                nodes.add(nid)
                active_inputs.add(nid)
            continue

        if tool in {"write", "write_file"}:
            p = None
            if isinstance(out.get("path"), str) and out.get("path"):
                p = out.get("path")
            elif isinstance(inp.get("path"), str) and inp.get("path"):
                p = inp.get("path")
            if isinstance(p, str) and p.strip():
                ap = _resolve_path(work_dir, p)
                nid = _node_id(work_dir, ap)
                nodes.add(nid)
                for src in sorted(active_inputs):
                    add_edge(src, nid)
                active_inputs = set([nid])
            continue

        if tool == "exec":
            import re
            import shlex
            import glob as glob_module
            
            cmd = inp.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            
            s = str(cmd or "").replace("\r", " ").replace("\n", " ")
            try:
                toks = shlex.split(s, posix=True)
            except Exception:
                toks = [x for x in re.findall(r'"([^"]+)"|\'([^\']+)\''|r'(\S+)', s) if x]
            
            reads: List[str] = []
            writes: List[str] = []
            cmd_names = {"ls", "cat", "grep", "awk", "sed", "xargs", "find", "python", "python3", "node", "mv", "cp", "rm", "mkdir", "unzip", "zip", "head", "tail"}
            
            i = 0
            while i < len(toks):
                t = toks[i]
                if t in {">", ">>"} and i + 1 < len(toks):
                    writes.append(_resolve_path(work_dir, toks[i + 1]))
                    i += 2
                    continue
                if t.startswith(">") and len(t) > 1:
                    writes.append(_resolve_path(work_dir, t[1:]))
                    i += 1
                    continue
                i += 1
            
            for t in toks:
                if not isinstance(t, str):
                    continue
                if t in cmd_names:
                    continue
                if not any(ch in t for ch in ["/", "\\", "."]):
                    continue
                if t.startswith("-"):
                    continue
                if t in {">", ">>", "|"}:
                    continue
                if any(ch in t for ch in ["*", "?", "["]):
                    pat = _resolve_path(work_dir, t)
                    matches = glob_module.glob(pat)
                    if matches:
                        for m in matches:
                            reads.append(os.path.abspath(m))
                    else:
                        reads.append(pat)
                    continue
                ap = _resolve_path(work_dir, t)
                reads.append(ap)
            
            read_nodes = []
            for rp in reads:
                nid = _node_id(work_dir, rp)
                nodes.add(nid)
                read_nodes.append(nid)
            write_nodes = []
            for wp in writes:
                nid = _node_id(work_dir, wp)
                nodes.add(nid)
                write_nodes.append(nid)
            if write_nodes:
                for w in write_nodes:
                    for r in read_nodes or list(active_inputs):
                        add_edge(r, w)
                active_inputs = set(write_nodes)
            else:
                for r in read_nodes:
                    active_inputs.add(r)
            continue

    return {
        "taskDir": os.path.abspath(task_dir),
        "agentKind": kind,
        "createdAt": _iso_now(),
        "nodes": sorted(nodes),
        "edges": [[a, b] for (a, b) in sorted(edges)],
    }


def evaluate_task(
    task_dir: str,
    *,
    eval_yaml_path: str,
    overwrite: bool = False,
    max_retries: int = 6,
    max_str_len: int = 2000,
    max_trace_items: int = 30,
    max_output_files: int = 10,
) -> Dict[str, Json]:
    """
    评估单个任务的执行结果
    
    Args:
        task_dir: 任务执行结果目录路径，包含 metadata.json、output 文件夹和执行过程文件
        eval_yaml_path: 评估用 LLM 的 YAML 配置文件路径
        overwrite: 是否覆盖已存在的评估结果
        max_retries: 最大重试次数
        max_str_len: 每个字符串字段的最大长度，超出部分会被截断
        max_trace_items: trace 中每个列表的最大项数
        max_output_files: 输出文件的最大数量
        
    Returns:
        评估结果字典
    """
    task_dir = os.path.abspath(task_dir)
    eval_yaml_path = os.path.abspath(eval_yaml_path)
    
    if not os.path.isdir(task_dir):
        return {"error": f"Task directory not found: {task_dir}", "success": False}
    
    if not os.path.isfile(eval_yaml_path):
        return {"error": f"Eval YAML file not found: {eval_yaml_path}", "success": False}
    
    eval_cfg = _read_yaml(eval_yaml_path)
    base_url = eval_cfg.get("baseUrl")
    model = eval_cfg.get("model")
    api_key = eval_cfg.get("apiKey")
    model_name = eval_cfg.get("model_name") or model or "unknown"
    
    if not base_url or not model or not api_key:
        return {"error": "Missing baseUrl, model, or apiKey in eval YAML", "success": False}
    
    task_id = os.path.basename(task_dir)
    kind = _detect_agent_kind(task_dir)
    meta = _load_task_metadata(task_dir)
    
    if meta is None:
        return {"error": "metadata.json not found or missing rubrics", "success": False, "taskId": task_id}
    
    rubrics = meta.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        return {"error": "No rubrics found in metadata", "success": False, "taskId": task_id}
    
    outputs = _collect_outputs(task_dir, meta)
    trace = _extract_trace(task_dir, kind)
    
    rubrics_out_path = os.path.join(task_dir, f"rubrics_judge--{model_name}.json")
    dep_graph_out_path = os.path.join(task_dir, f"dependency_graph--{model_name}.json")
    
    result = {
        "taskId": task_id,
        "taskDir": task_dir,
        "evalModel": model_name,
        "evalYamlPath": eval_yaml_path,
        "success": True,
    }
    
    if not overwrite and os.path.exists(rubrics_out_path):
        result["rubricsSkipped"] = True
    else:
        # 1) 构造 judge messages（包含文本 JSON 证据；若有图片则附加 image_url block）
        # 2) 调用评测模型生成 rubric 判定
        # 3) 容错解析模型返回的 JSON，并落盘 rubrics_judge--*.json
        messages = _build_grading_messages(
            task_id=task_id,
            meta=meta,
            outputs=outputs,
            trace=trace,
            max_trace_items=max_trace_items,
            max_str_len=max_str_len,
            max_output_files=max_output_files,
        )
        # 用于记录到产物里（注意该字符串可能很大，会在落盘时被截断）
        user_prompt_content = ""
        for m in messages:
            if m.get("role") == "user":
                user_prompt_content = json.dumps(m.get("content"), ensure_ascii=False)[:4000]
                break
        
        started = time.time()
        full = None
        usage = None
        content = ""
        err = ""
        tries = 0
        while True:
            tries += 1
            full, usage, content = _chat_completions(
                base_url=str(base_url),
                api_key=str(api_key),
                model=str(model),
                messages=messages,
                timeout_s=180,
                max_retries=max_retries,
            )
            if full is not None:
                break
            err = content
            if tries >= max_retries or not _is_rate_limited(err):
                break
            time.sleep(min(60, 5 * (2 ** (tries - 1))))
        
        duration_ms = int((time.time() - started) * 1000)
        
        judged_obj = _json_first_object(content)
        rows: List[Json] = []
        if isinstance(judged_obj, dict) and isinstance(judged_obj.get("rubrics"), list):
            for it in judged_obj.get("rubrics"):
                if not isinstance(it, dict):
                    continue
                idx = it.get("index")
                passed = it.get("passed")
                conf = it.get("confidence")
                ev = it.get("evidence")
                if not isinstance(idx, int):
                    continue
                rows.append(
                    {
                        "index": idx,
                        "rubric": rubrics[idx] if idx < len(rubrics) and isinstance(rubrics[idx], str) else None,
                        "passed": bool(passed) if isinstance(passed, bool) else False,
                        "confidence": float(conf) if isinstance(conf, (int, float)) else None,
                        "evidence": str(ev) if isinstance(ev, str) else "",
                    }
                )
        else:
            for i, r in enumerate(rubrics):
                if not isinstance(r, str):
                    continue
                rows.append({"index": i, "rubric": r, "passed": False, "confidence": 0.0, "evidence": f"LLM API call failed: {err[:200]}" if err else "Judge output parse failed"})
        
        passed_n = len([x for x in rows if isinstance(x, dict) and x.get("passed") is True])
        failed_n = len(rows) - passed_n
        
        _write_json(
            rubrics_out_path,
            {
                "taskId": task_id,
                "agentKind": kind,
                "createdAt": _iso_now(),
                "rubrics": sorted(rows, key=lambda x: int(x.get("index")) if isinstance(x, dict) and isinstance(x.get("index"), int) else 10**9),
                "summary": {"total": len(rows), "passed": passed_n, "failed": failed_n},
                "judge": {
                    "model": model,
                    "modelName": model_name,
                    "baseUrl": base_url,
                    "usage": usage,
                    "durationMs": duration_ms,
                    "tries": tries,
                    "error": err or None,
                    "rawResponseHead": (content or "")[:2000],
                },
                "prompt": {
                    "system": (messages[0].get("content") if isinstance(messages, list) and messages and isinstance(messages[0], dict) else None),
                    "user": user_prompt_content,
                    "userPromptSizeBytes": len(user_prompt_content.encode("utf-8")),
                    "userPromptSizeChars": len(user_prompt_content),
                },
            },
        )
        result["rubricsPath"] = rubrics_out_path
        result["rubricsSummary"] = {"total": len(rows), "passed": passed_n, "failed": failed_n}
    
    if not overwrite and os.path.exists(dep_graph_out_path):
        result["depGraphSkipped"] = True
    else:
        # 基于 toolCalls/exec/write/read 等信息构建依赖图，用于后续可视化或指标分析
        dep_graph = _build_dependency_graph(task_dir)
        dep_graph["evalModel"] = model_name
        _write_json(dep_graph_out_path, dep_graph)
        result["depGraphPath"] = dep_graph_out_path
        result["depGraphSummary"] = {"nodes": len(dep_graph.get("nodes", [])), "edges": len(dep_graph.get("edges", []))}
    
    return result


def evaluate_task_dir(
    task_dir: str,
    *,
    eval_yaml_path: str,
    overwrite: bool = False,
    max_retries: int = 6,
    max_str_len: int = 2000,
    max_trace_items: int = 30,
    max_output_files: int = 10,
) -> Dict[str, Json]:
    """
    评估任务目录（兼容旧接口名称）
    """
    return evaluate_task(
        task_dir,
        eval_yaml_path=eval_yaml_path,
        overwrite=overwrite,
        max_retries=max_retries,
        max_str_len=max_str_len,
        max_trace_items=max_trace_items,
        max_output_files=max_output_files,
    )




if __name__ == "__main__":
    import argparse
    
    p = argparse.ArgumentParser(description="Evaluate a single task execution result")
    p.add_argument("--task-dir", required=True, help="Path to task execution result directory")
    p.add_argument("--eval-yaml", required=True, help="Path to eval LLM YAML config file")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing evaluation results")
    p.add_argument("--max-retries", type=int, default=6, help="Max retries for LLM API calls")
    p.add_argument("--max-str-len", type=int, default=2000, help="Max length for each string field in prompt")
    p.add_argument("--max-trace-items", type=int, default=30, help="Max items in each trace list")
    p.add_argument("--max-output-files", type=int, default=10, help="Max output files to include in prompt")
    args = p.parse_args()
    
    # 遍历 task_dir 下每一个子目录进行评估
    task_dirs = []
    if os.path.isdir(args.task_dir):
        for entry in os.listdir(args.task_dir):
            entry_path = os.path.join(args.task_dir, entry)
            if os.path.isdir(entry_path):
                # 检查是否为有效的任务目录（包含 metadata.json）
                metadata_path = os.path.join(entry_path, "metadata.json")
                if os.path.isfile(metadata_path):
                    task_dirs.append(entry_path)
    
    # 如果没有找到子任务目录，则将当前目录作为任务目录
    if not task_dirs:
        task_dirs = [args.task_dir]
    
    all_results = []
    for single_task_dir in tqdm(task_dirs):
        print(f"Evaluating task: {single_task_dir}", file=sys.stderr)
        result = evaluate_task(
            task_dir=single_task_dir,
            eval_yaml_path=args.eval_yaml,
            overwrite=args.overwrite,
            max_retries=args.max_retries,
            max_str_len=args.max_str_len,
            max_trace_items=args.max_trace_items,
            max_output_files=args.max_output_files,
        )
        all_results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 如果有多个任务，输出汇总结果
    if len(all_results) > 1:
        summary = {
            "totalTasks": len(all_results),
            "successful": len([r for r in all_results if r.get("success")]),
            "failed": len([r for r in all_results if not r.get("success")]),
            "results": all_results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
