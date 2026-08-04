"""
downloader.py — 文件下载与技术验证（线程安全版）

变更:
  - pre_check_url: seen_urls 改用 try_add() 原子去重，避免竞态重复下载
  - post_download_verify: seen_hashes 改用 try_add() 原子去重
  - download_file: 临时文件名加入线程标识，避免多线程文件名冲突
"""

import os
import re
import hashlib
import zipfile
import time
import threading
import requests
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from pipeline.config import (
    HEADERS, MIME_MAP, VALID_DOC_MIMES, VALID_DOC_EXTENSIONS,
    LOGIN_WALL_PATTERNS, NOISE_PARAMS,
    MIN_FILE_SIZE, MAX_FILE_SIZE,
)
from pipeline.utils import log_event


def normalize_url(url):
    """标准化 URL：统一大小写、去除追踪参数和 fragment。"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=False)
        cleaned_params = {k: v for k, v in params.items() if k.lower() not in NOISE_PARAMS}
        cleaned_query = urlencode(cleaned_params, doseq=True) if cleaned_params else ''
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                           parsed.params, cleaned_query, ''))
    except Exception:
        return url


def pre_check_url(url, seen_urls, log_file):
    """
    HEAD 预检：去重 + MIME + 登录墙 + 大小。

    线程安全: seen_urls 必须是 ThreadSafeSet，使用 try_add() 原子去重。
    返回 (passed, reason, final_url)。
    """
    norm_url = normalize_url(url)

    # ★ 原子性检查+占位，防止两个线程同时通过同一 URL
    if not seen_urls.try_add(norm_url):
        return False, "URL 重复，跳过。", url

    try:
        head_resp = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        head_resp.raise_for_status()
    except requests.exceptions.TooManyRedirects:
        return False, "重定向次数过多。", url
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP {e.response.status_code}", url
    except Exception as e:
        log_event(f"      [预检] HEAD 失败 ({str(e)[:50]})，放行。", log_file)
        return True, "HEAD 失败但放行", url

    final_url = head_resp.url
    for pattern in LOGIN_WALL_PATTERNS:
        if pattern in final_url.lower():
            return False, f"重定向到登录页 ({pattern})。", final_url

    norm_final = normalize_url(final_url)
    # ★ 重定向后的 URL 也原子去重
    if norm_final != norm_url and not seen_urls.try_add(norm_final):
        return False, "重定向后 URL 重复。", final_url

    ct = head_resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
    if 'text/html' in ct or 'text/plain' in ct:
        return False, f"Content-Type={ct}，非文档。", final_url
    if ct and ct not in VALID_DOC_MIMES:
        path_ext = os.path.splitext(urlparse(final_url).path)[1].lower()
        if path_ext not in VALID_DOC_EXTENSIONS:
            return False, f"Content-Type ({ct}) 不在白名单。", final_url

    cl = head_resp.headers.get('Content-Length')
    if cl:
        try:
            size = int(cl)
            if size < MIN_FILE_SIZE:
                return False, f"文件太小 ({size/1024:.1f}KB)。", final_url
            if size > MAX_FILE_SIZE:
                return False, f"文件过大 ({size/1024/1024:.1f}MB)。", final_url
        except ValueError:
            pass

    return True, "预检通过", final_url


def download_file(url, save_dir, index, log_file):
    """
    HTTP GET 流式下载。

    线程安全: 临时文件名加入线程ID，避免多线程写同一文件。
    返回 (success, filename_or_reason)。
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        response.raise_for_status()

        content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
        ext = MIME_MAP.get(content_type)
        if not ext:
            path_ext = os.path.splitext(url.split("/")[-1].split("?")[0])[1].lower()
            ext = path_ext if path_ext in VALID_DOC_EXTENSIONS else '.pdf'

        base_name = os.path.splitext(url.split("/")[-1].split("?")[0])[0]
        if not base_name or len(base_name) < 3:
            # ★ 加入线程ID，确保多线程不会生成相同的临时文件名
            tid = threading.current_thread().ident
            base_name = f"doc_{index}_{tid}_{time.time():.0f}"
        base_name = re.sub(r'[<>:"/\\|?*]', '_', base_name)[:100]

        full_filename = f"{base_name}{ext}"
        file_path = os.path.join(save_dir, full_filename)

        if os.path.exists(file_path):
            name_part, ext_part = os.path.splitext(full_filename)
            counter = 1
            while os.path.exists(file_path):
                full_filename = f"{name_part}_{counter}{ext_part}"
                file_path = os.path.join(save_dir, full_filename)
                counter += 1

        ct_lower = response.headers.get('Content-Type', '').lower()
        if 'text/html' in ct_lower or 'text/plain' in ct_lower:
            return False, f"检测到网页内容 ({ct_lower})。"
        cl = response.headers.get('Content-Length')
        if cl and int(cl) < MIN_FILE_SIZE:
            return False, f"文件太小 ({int(cl)/1024:.1f}KB)。"

        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)

        actual_size = os.path.getsize(file_path)
        if actual_size < MIN_FILE_SIZE:
            os.remove(file_path)
            return False, f"实际文件太小 ({actual_size/1024:.1f}KB)，已删除。"

        return True, full_filename
    except Exception as e:
        return False, f"连接异常: {str(e)[:100]}"


def post_download_verify(file_path, expected_ext, seen_hashes, log_file):
    """
    下载后技术验证：magic bytes + 后缀修正 + 哈希去重。

    线程安全: seen_hashes 必须是 ThreadSafeSet，使用 try_add() 原子去重。
    """
    real_ext = detect_real_type_from_magic(file_path)

    if real_ext == '.html':
        os.remove(file_path)
        return False, "内容实际是 HTML，已删除。", file_path
    if real_ext == '.zip':
        os.remove(file_path)
        return False, "普通 ZIP 而非 Office 文档，已删除。", file_path
    if real_ext == '.ole':
        log_event("      [Magic] OLE2 无法细分，保留原后缀。", log_file)

    if real_ext and real_ext not in ('.zip', '.ole', None):
        current_ext = os.path.splitext(file_path)[1].lower()
        if current_ext != real_ext:
            new_path = os.path.splitext(file_path)[0] + real_ext
            if os.path.exists(new_path):
                base = os.path.splitext(new_path)[0]
                counter = 1
                while os.path.exists(new_path):
                    new_path = f"{base}_{counter}{real_ext}"
                    counter += 1
            os.rename(file_path, new_path)
            log_event(f"      [Magic] 后缀修正: {current_ext} -> {real_ext}", log_file)
            file_path = new_path

    file_hash = compute_file_hash(file_path)
    if file_hash:
        # ★ 原子性检查+添加，防止两个线程下载了相同内容的文件
        if not seen_hashes.try_add(file_hash):
            os.remove(file_path)
            return False, f"内容哈希重复 ({file_hash[:16]}...)，已删除。", file_path

    return True, "后验证通过", file_path


# ================= 以下函数不变 =================

def detect_real_type_from_magic(file_path):
    """通过 magic bytes 判断文件真实类型。"""
    try:
        with open(file_path, 'rb') as f:
            header = f.read(32)
        if len(header) < 4:
            return None
        if header[:4] == b'%PDF':
            return '.pdf'
        if header[:4] in (b'PK\x03\x04', b'PK\x05\x06'):
            return _identify_zip_subtype(file_path)
        if header[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
            return _identify_ole_subtype(file_path)
        raw = header.lstrip(b'\xef\xbb\xbf')
        text_header = raw.decode('utf-8', errors='ignore').strip().lower()
        if text_header.startswith(('<!doctype', '<html', '<head', '<?xml')):
            return '.html'
        return None
    except Exception:
        return None


def _identify_zip_subtype(file_path):
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            names = zf.namelist()
            if any(n.startswith('word/') for n in names): return '.docx'
            elif any(n.startswith('xl/') for n in names): return '.xlsx'
            elif any(n.startswith('ppt/') for n in names): return '.pptx'
            elif '[Content_Types].xml' in names: return '.docx'
            else: return '.zip'
    except zipfile.BadZipFile: return None
    except Exception: return None


def _identify_ole_subtype(file_path):
    try:
        with open(file_path, 'rb') as f:
            content = f.read(min(os.path.getsize(file_path), 64 * 1024))
        if b'WordDocument' in content or b'MSWordDoc' in content: return '.doc'
        elif b'Workbook' in content or b'Book' in content: return '.xls'
        elif b'PowerPoint' in content or b'PP40' in content: return '.ppt'
        else: return '.ole'
    except Exception: return None


def compute_file_hash(file_path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    try:
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk: break
                h.update(chunk)
        return h.hexdigest()
    except Exception: return None