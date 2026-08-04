"""
config.py — 全局配置与常量定义
"""

import os

# ================= 路径配置 =================

ROOT_DIR = "/home/weizheng/RIPBench文件扩充/产品情景/chanpin"
LOG_DIR = "/home/weizheng/RIPBench文件扩充/产品情景/log_最终"
# BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# MD_FILE_PATH = os.path.join(BASE_DIR, "folder_structure.md")
MD_FILE_PATH = "/home/weizheng/RIPBench文件扩充/产品情景/文件树.md"
# ================= LLM API/Brave Search API 配置（搜索用） =================

LLM_CONFIG = {
    "api_key": "sk-cp-6W3t_YA5i5Kmrm53VcAI7RNuAoHr-nwkINjqOkh_QRczR62se5kyxouk0wIw25agYVGYuiGMbJXUiCeG4A5Cq6R2FR8dRKriaTXeVTPPShvVeHolKQWTSqM",
    "base_url": "https://api.minimax.chat/v1",
    "model": "MiniMax-M2.7",
}
BRAVE_API_KEY = "BSAWV9iHb1I4wlQhFU4BMPg43HxJO3C"

# LLM 后验证配置（留 None 则复用 LLM_CONFIG）
VALIDATOR_LLM_CONFIG = None

# ================= 文件类型筛选 =================

# 运行时选择要处理的类型: "pdf", "xlsx", "pptx", "text"
TARGET_FILE_TYPES = ["pdf", "xlsx", "pptx"]

# 类型名 → 实际后缀
FILE_TYPE_EXTENSION_MAP = {
    "pdf":  [".pdf"],
    "xlsx": [".xlsx", ".xls"],
    "pptx": [".pptx", ".ppt"],
    "text": [".docx", ".doc", ".txt"],
}

# 空文件判定阈值（字节），<= 此值视为空文件需要替换
EMPTY_FILE_THRESHOLD = 1024  # 1KB

# ================= 运行限制 =================

MAX_URLS_PER_FILE = 30        # 每个空文件最多尝试的候选 URL 数
MAX_LLM_RETRIES = 2
MIN_FILE_SIZE = 20 * 1024     # 20KB
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB

# ================= HTTP =================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ================= MIME =================

MIME_MAP = {
    'application/pdf': '.pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-excel': '.xls',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.ms-powerpoint': '.ppt',
    'application/octet-stream': None,
}

VALID_DOC_MIMES = {
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.ms-powerpoint',
    'application/octet-stream',
    'application/force-download',
    'application/x-download',
    'binary/octet-stream',
}

VALID_DOC_EXTENSIONS = {'.pdf', '.docx', '.doc',
                        '.xlsx', '.xls', '.pptx', '.ppt', '.txt'}

# ================= 安全规则 =================

LOGIN_WALL_PATTERNS = [
    '/login', '/signin', '/auth', '/sso/', '/cas/',
    '/captcha', '/verify', '/register', '/signup',
    'accounts.google.com', 'login.microsoftonline.com',
    'drive.google.com/file',
]

NOISE_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'ref', 'referer', 'referrer', 'source', 'fbclid', 'gclid', 'mc_cid',
    'mc_eid', '_ga', '_gl', 'spm', 'scm', 'from', 'isappinstalled',
}
# ================= 协同文件生成 =================

# 是否启用协同文件生成（方便开关）
ENABLE_SYNTH = True

# 每个源文件最多生成几个协同文件
MAX_SYNTH_FILES_PER_SOURCE = 1

# Claude Agent SDK 执行超时（秒）
SYNTH_EXECUTION_TIMEOUT = 1000

# Agent 最大工具调用轮数（防止死循环）
SYNTH_MAX_TURNS = 15
