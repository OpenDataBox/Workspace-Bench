#!/usr/bin/env python3
import argparse
import re
from pathlib import Path


SECRET_FIELD_RE = re.compile(
    r"(apiKey|api_key|appSecret|clientSecret|privateKey|privateKeyPem|token|authorization|password|credential)",
    re.I,
)
PLACEHOLDER_RE = re.compile(
    r"\\$\\{|^(|your[-_ ]?|example|dummy|placeholder|test|changeme|xxx+|<.*>)$",
    re.I,
)
CONCRETE_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"Bearer\\s+[A-Za-z0-9._=-]{20,}|[A-Za-z0-9_-]{16,}:[A-Za-z0-9_-]{16,}|"
    r"[A-Za-z0-9_-]{32,})"
)


def _is_probably_secret(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if not SECRET_FIELD_RE.search(stripped):
        return False
    if PLACEHOLDER_RE.search(stripped):
        return False
    return bool(CONCRETE_VALUE_RE.search(stripped))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "roots",
        nargs="*",
        default=["runs", "fs_map", "docker", "scripts", ".generated/docker"],
        help="Paths under evaluation/ to scan",
    )
    args = parser.parse_args()

    eval_root = Path(__file__).resolve().parents[1]
    findings = []
    for rel_root in args.roots:
        root = eval_root / rel_root
        if not root.exists():
            continue
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in files:
            if path.suffix.lower() not in {".yaml", ".yml", ".json", ".sh", ".py", ".env"}:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for lineno, line in enumerate(lines, 1):
                if _is_probably_secret(line):
                    findings.append(f"{path.relative_to(eval_root)}:{lineno}: possible hardcoded secret")

    if findings:
        print("\\n".join(findings))
        raise SystemExit(1)
    print("[ok] no concrete-looking hardcoded secrets found")


if __name__ == "__main__":
    main()
