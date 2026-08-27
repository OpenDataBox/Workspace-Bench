import importlib.util
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT_DIR / "evaluation" / "src" / "task_patches.py"
SPEC = importlib.util.spec_from_file_location("task_patches", MODULE_PATH)
task_patches = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(task_patches)


class TaskPatchTests(unittest.TestCase):
    def test_downloader_does_not_apply_optional_patches_automatically(self):
        downloader = (
            ROOT_DIR / "evaluation" / "scripts" / "download_hf_assets.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("from task_patches import", downloader)
        self.assertNotIn("apply_task_patches(", downloader)

    def test_patch_bundle_covers_reported_tasks(self):
        bundle = ROOT_DIR / "evaluation" / "task_patches" / "lite_cn"
        task_ids = {path.parent.name for path in bundle.glob("*/patch.json")}
        self.assertEqual(task_ids, {"23", "33", "127", "207", "269", "346", "380", "381", "386"})

    def test_applies_metadata_and_generated_xlsx(self):
        source_bundle = ROOT_DIR / "evaluation" / "task_patches"
        with tempfile.TemporaryDirectory() as td:
            task_root = Path(td) / "tasks_lite"
            for task_id in ("207", "269"):
                task_dir = task_root / task_id
                task_dir.mkdir(parents=True)
                source_patch = json.loads(
                    (source_bundle / "lite_cn" / task_id / "patch.json").read_text(encoding="utf-8")
                )
                patched_rubrics = source_patch.get("metadata", {}).get("rubrics", [])
                (task_dir / "metadata.json").write_text(
                    json.dumps(
                        {
                            "id": task_id,
                            "task": "old",
                            "output_files": ["old.docx"],
                            "rubrics": [],
                            "rubric_types": ["结果评估"] * len(patched_rubrics),
                            "data_manifest": [],
                            "file_dep_graph": [],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                if task_id == "207":
                    data_dir = task_dir / "data"
                    data_dir.mkdir()
                    for name in (
                        "5c6df2b2a45aad70_张浩然简历.docx",
                        "aa1e07f51052d198_王佳宁简历.docx",
                        "b6587a043148382d_赵思远简历.docx",
                        "f73d2057470dd5f4_李雨辰简历.docx",
                    ):
                        (data_dir / name).write_bytes(b"placeholder")
                else:
                    data_dir = task_dir / "data"
                    data_dir.mkdir()
                    (data_dir / "465e3d852589364e_2024 年销售业务全量数据.xlsx").write_bytes(
                        b"placeholder"
                    )

            patched = task_patches.apply_task_patches(
                task_root,
                kind="lite",
                language="cn",
                patch_root=source_bundle,
            )

            self.assertEqual(patched, ["207", "269"])
            metadata_269 = json.loads((task_root / "269" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata_269["output_files"], ["2024 年 12 月客户对账分析报告.md"])
            self.assertTrue(metadata_269["file_dep_graph"][0]["to"].endswith(".md"))

            model_path = task_root / "207" / "data" / "5-通用人才画像（模型）.xlsx"
            self.assertTrue(zipfile.is_zipfile(model_path))
            first_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
            task_patches.apply_task_patches(
                task_root,
                kind="lite",
                language="cn",
                patch_root=source_bundle,
            )
            self.assertEqual(hashlib.sha256(model_path.read_bytes()).hexdigest(), first_hash)
            with zipfile.ZipFile(model_path) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
            self.assertIn("评分模型", workbook)
            self.assertIn("评分规则", workbook)

    def test_patches_remove_the_reported_contract_conflicts(self):
        bundle = ROOT_DIR / "evaluation" / "task_patches" / "lite_cn"

        def metadata_patch(task_id: str):
            return json.loads((bundle / task_id / "patch.json").read_text(encoding="utf-8"))["metadata"]

        self.assertNotIn("当前库存清单", metadata_patch("23")["task"])
        self.assertNotIn("三级、二级、一级", metadata_patch("33")["task"])
        self.assertIn("8个python文件", metadata_patch("127")["task"])
        self.assertTrue(
            any(item["filename"] == "当前库存物品总清单_2024-12.xlsx" for item in metadata_patch("23")["data_manifest"])
        )
        inventory_item = next(
            item
            for item in metadata_patch("23")["data_manifest"]
            if item["filename"] == "当前库存物品总清单_2024-12.xlsx"
        )
        self.assertEqual(
            inventory_item["target_path"],
            "后勤/库存/物品清单/当前库存物品总清单_2024-12.xlsx",
        )
        self.assertTrue(
            any(item["filename"] == "5-通用人才画像（模型）.xlsx" for item in metadata_patch("207")["data_manifest"])
        )
        model_item = next(
            item
            for item in metadata_patch("207")["data_manifest"]
            if item["filename"] == "5-通用人才画像（模型）.xlsx"
        )
        self.assertEqual(model_item["target_path"], "人才画像/模板/5-通用人才画像（模型）.xlsx")
        self.assertEqual(metadata_patch("269")["output_files"], ["2024 年 12 月客户对账分析报告.md"])
        self.assertIn("业务处理基准日期为2026-04-07", metadata_patch("346")["task"])
        self.assertNotIn("志愿者", metadata_patch("380")["task"])
        self.assertIn("数据统计层级不同", metadata_patch("381")["task"])
        self.assertIn("两份可用的已完成ASR转写稿", metadata_patch("386")["task"])
        self.assertNotIn("音频→文字", metadata_patch("386")["task"])


if __name__ == "__main__":
    unittest.main()
