# Issue #22 回复草稿

感谢详细核查。该 issue 提到的 3 个任务已在仓库侧加入下载后修正规则：

- **Task 23**
  - 已将 `当前库存物品总清单_2024-12.xlsx` 加入任务包、`data_manifest` 和 `file_dep_graph`。
  - 同时核对了采购单源文件，将两条与数据不一致的 rubric 修正为实际可统计值：待审批 **29** 张、已批准 **7** 张。
- **Task 127**
  - 已将题面中的 Python 文件数量由 **10** 改为 **8**，与 manifest、依赖图和实际输入保持一致。
- **Task 386**
  - 已移除“对 4 段 `.m4a` 录音执行音频转写”的不可完成要求。
  - 新题面明确说明当前可用内容为录音元数据和两份已完成 ASR 转写稿（D1 上午场、D2 下午场），并要求不得虚构未提供会话的内容。

为避免 Hugging Face 上游快照更新前再次下载到旧元数据，我们新增了可重复执行的 task patch 机制：

- `evaluation/src/task_patches.py`
- `evaluation/scripts/apply_task_patches.py`
- `evaluation/task_patches/lite_cn/`

`download_hf_assets.py` 下载完成后会自动应用这些修正；已有本地数据也可以手动执行：

```bash
cd evaluation
python3 scripts/apply_task_patches.py --kind lite --language cn
```

相关单元测试已补充并通过。
