from tqdm import tqdm
import os
import shutil
import json
from pathlib import Path
import stat
from typing import Callable, Tuple
"""
在执行每一个任务后，模型可能会修改现有的文件系统目录结构，
我们需要在每个任务完成后，回滚到任务开始前的文件系统状态结构。
"""

def _chmod_writable(p: Path) -> None:
    """
    Best-effort: make path writable by current user so rollback can delete it.
    """
    try:
        mode = p.stat().st_mode
    except Exception:
        return
    try:
        if p.is_dir():
            # Ensure owner can list/enter/write.
            p.chmod(mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        else:
            p.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        return


def _rmtree_onerror(func: Callable[[str], None], path: str, exc_info: Tuple[type, BaseException, object]) -> None:
    """
    shutil.rmtree error handler: try to chmod then retry once.
    """
    try:
        _chmod_writable(Path(path))
    except Exception:
        pass
    try:
        func(path)
        return
    except Exception:
        # Re-raise original exception to keep failure visible.
        raise


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
        return
    except PermissionError:
        _chmod_writable(p)
        p.unlink()


def _safe_rmtree(p: Path) -> None:
    shutil.rmtree(p, onerror=_rmtree_onerror)


def make_filesys(raw_work_dir: str, standard_work_dir: str, role: str, task_dir: str) -> None:
    """
    基于 tasks 根目录下每个任务的 metadata.json(data_manifest)，构建标准 workdir：
    1) 复制 raw_work_dir 为 standard_work_dir
    2) 遍历 task_dir 下所有任务目录，将 data_manifest 指定的文件拷贝到 standard_work_dir 对应位置
    """
    raw_path = Path(raw_work_dir)
    standard_path = Path(standard_work_dir)
    tasks_root = Path(task_dir)

    if not raw_path.exists():
        raise FileNotFoundError(f"原始目录不存在: {raw_work_dir}")
    if not raw_path.is_dir():
        raise NotADirectoryError(f"原始目录不是目录: {raw_work_dir}")
    if not tasks_root.exists():
        raise FileNotFoundError(f"任务目录不存在: {task_dir}")
    if not tasks_root.is_dir():
        raise NotADirectoryError(f"任务目录不是目录: {task_dir}")

    if standard_path.exists():
        shutil.rmtree(standard_path)
    shutil.copytree(raw_path, standard_path)

    standard_abs = standard_path.resolve()

    meta_paths: list[Path] = []
    for p in sorted(tasks_root.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        if p.name.startswith("."):
            continue
        mp = p / "metadata.json"
        if mp.exists() and mp.is_file():
            meta_paths.append(mp)

    for meta_path in meta_paths:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise ValueError(f"metadata.json 不是对象: {str(meta_path)}")
    
        if meta.get("file_system") != role:
            continue
        
        data_manifest = meta.get("data_manifest")
        if not isinstance(data_manifest, list):
            continue

        task_path = meta_path.parent.resolve()
        for item in data_manifest:
            if not isinstance(item, dict):
                continue
            stored_relpath = item.get("stored_relpath")
            target_path = item.get("target_path")
            filename = item.get("filename")
            if not isinstance(stored_relpath, str) or not stored_relpath.strip():
                continue
            if not isinstance(target_path, str) or not target_path.strip():
                continue
            if not isinstance(filename, str) or not filename.strip():
                filename = os.path.basename(stored_relpath.strip())
            stored_relpath = stored_relpath.strip().replace("\\", "/").lstrip("/")
            src = (task_path / stored_relpath).resolve()
            if not str(src).startswith(str(task_path) + os.sep):
                raise ValueError(f"stored_relpath 越界: {stored_relpath}")
            if not src.exists() or not src.is_file():
                print(f"数据文件不存在: {str(src)}")
                continue
                # raise FileNotFoundError(f"数据文件不存在: {str(src)}")
                

            tp = target_path.strip().replace("\\", "/")
            while tp.startswith("/"):
                tp = tp[1:]
            tp = tp.rstrip("/")
            if tp.endswith("/" + filename.strip()) or tp == filename.strip():
                rel_dst = Path(tp)
            else:
                rel_dst = Path(tp) / filename.strip()
            dst = (standard_abs / rel_dst).resolve()
            if not str(dst).startswith(str(standard_abs) + os.sep):
                raise ValueError(f"target_path 越界: {target_path}")
            cur = standard_abs
            for part in rel_dst.parent.parts:
                cur = (cur / part).resolve()
                if cur.exists() and cur.is_file():
                    cur.unlink()
                cur.mkdir(parents=False, exist_ok=True)
            if dst.exists() and dst.is_dir():
                shutil.rmtree(dst)
            shutil.copy2(src, dst)

def filesys_rollback(standard_work_dir: str, work_dir: str) -> None:
    """
    保证执行任务后的文件系统和标准的文件系统结构一致，包括文件内容的对比和同步
    """
    # Keep paths stable even if caller passes relative paths.
    work_path = Path(work_dir)
    standard_path = Path(standard_work_dir)

    if not standard_path.exists():
        return

    # 收集标准目录中的所有文件和目录
    standard_files = set()
    standard_dirs = set()

    for root, dirs, files in os.walk(standard_path):
        rel_root = Path(root).relative_to(standard_path)
        for d in dirs:
            standard_dirs.add(rel_root / d)
        for f in files:
            standard_files.add(rel_root / f)

    # 收集工作目录中的所有文件和目录
    work_files = set()
    work_dirs = set()

    if work_path.exists():
        for root, dirs, files in os.walk(work_path):
            rel_root = Path(root).relative_to(work_path)
            for d in dirs:
                work_dirs.add(rel_root / d)
            for f in files:
                work_files.add(rel_root / f)

    # 1. 删除工作目录中多余的文件
    for rel_file in work_files - standard_files:
        target_file = work_path / rel_file
        if target_file.exists():
            _safe_unlink(target_file)

    # 2. 删除工作目录中多余的目录（从深到浅删除）
    extra_dirs = work_dirs - standard_dirs
    for rel_dir in sorted(extra_dirs, key=lambda x: -len(x.parts)):
        target_dir = work_path / rel_dir
        if target_dir.exists():
            _safe_rmtree(target_dir)

    # 3. 创建缺失的目录
    for rel_dir in standard_dirs - work_dirs:
        target_dir = work_path / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)

    # 4. 复制或更新文件（仅复制修改或缺失的文件）
    for rel_file in standard_files:
        source_file = standard_path / rel_file
        target_file = work_path / rel_file

        # 确保父目录存在
        target_file.parent.mkdir(parents=True, exist_ok=True)

        # 检查文件是否需要复制：文件不存在，或内容不同
        need_copy = False
        if not target_file.exists():
            need_copy = True
        else:
            # 比较文件大小和修改时间
            source_stat = source_file.stat()
            target_stat = target_file.stat()
            if source_stat.st_size != target_stat.st_size or source_stat.st_mtime != target_stat.st_mtime:
                need_copy = True

        # 仅当需要时才复制文件
        if need_copy:
            shutil.copy2(source_file, target_file)


if __name__ == "__main__":

    eval_root = Path(__file__).resolve().parents[1]
    os.chdir(eval_root)
    task_dir = os.environ.get("TASK_DIR", str(eval_root / "tasks"))
    fs_map_file = os.environ.get("FS_MAP_FILE", "fs_map/fs_map_Codex_GPT-5.4.json")
    fs_map_path = Path(fs_map_file)
    if not fs_map_path.is_absolute():
        fs_map_path = eval_root / fs_map_path

    fs_map_all = json.load(open(fs_map_path, encoding="utf-8"))

    raw_work_dir = fs_map_all.get("raw_work_dir", {})
    standard_work_dir = fs_map_all.get("standard_work_dir", {})
    work_dir = fs_map_all.get("work_dir", {})
    roles = raw_work_dir.keys()

    for role in roles:

        # 将任务需要的文件加入到 standard_work_dir 中，保持与 raw_work_dir 相同的目录结构
        make_filesys(raw_work_dir=raw_work_dir[role], 
                     standard_work_dir=standard_work_dir[role], 
                     role=role,
                     task_dir=task_dir) 

        # 将 standard_work_dir 中的文件同步到 work_dir 中，保持一致的文件系统结构
        filesys_rollback(standard_work_dir=standard_work_dir[role], work_dir=work_dir[role])
