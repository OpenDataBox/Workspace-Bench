# Issue #24 回复草稿

感谢指出这两个任务合同中的不一致。修复已提交至 PR #25（https://github.com/OpenDataBox/Workspace-Bench/pull/25），该 PR 关联并将在合并后关闭本 issue。现已修复：

- **Task 269**
  - `output_files` 已从 `2024 年 12 月客户对账分析报告.docx` 改为 `2024 年 12 月客户对账分析报告.md`。
  - `file_dep_graph.to` 已同步改为相同的 Markdown 文件名。
  - 现在题面、提交文件合同和 rubric 使用同一格式。
- **Task 346**
  - 为保证任务可复现，题面现已明确指定业务处理基准日期为 **2026-04-07**，报废申请日期统一填写该日期。
  - 因此 rubric 不再依赖实际运行当天日期，也不会随评测日期变化。

修正会由 `download_hf_assets.py` 在下载中文 Lite 任务后自动应用；已有下载可执行：

```bash
cd evaluation
python3 scripts/apply_task_patches.py --kind lite --language cn
```

相关自动化测试已通过。
