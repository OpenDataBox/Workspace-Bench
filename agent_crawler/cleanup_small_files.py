import os
from pathlib import Path

TARGET_EXTENSIONS = {'.pdf', '.docx', '.xlsx', '.pptx'}
SIZE_THRESHOLD = 1024  # 1KB


def cleanup_small_files(folder_path: str | Path, dry_run=True) -> None:
    folder = Path(folder_path)
    deleted = []

    for file_path in folder.rglob("*"):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in TARGET_EXTENSIONS:
            continue

        size = file_path.stat().st_size

        if size < SIZE_THRESHOLD:
            print(f"Deleting {file_path} ({size} bytes)")
            
            if not dry_run:
                file_path.unlink()

            deleted.append(str(file_path))

    if deleted:
        print(f"\nDeleted {len(deleted)} file(s)")
    else:
        print("No files to delete")


if __name__ == '__main__':
    cleanup_small_files("/home/weizheng/RIPBench文件扩充/开发情景/kaifa", dry_run=False)