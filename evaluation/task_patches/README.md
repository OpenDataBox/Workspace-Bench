# Optional Task Patches

This directory contains repository-maintained corrections for downloaded task
snapshots. The patches are **opt-in**: downloading a dataset does not apply
them automatically.

The published Hugging Face snapshots remain unchanged for fair comparison with
existing leaderboard submissions. To evaluate the corrected CN Lite task
contracts locally, first download the same task snapshot used by the
leaderboard, then apply the patch explicitly:

```bash
cd evaluation
python3 scripts/download_hf_assets.py --language cn --lite
python3 scripts/apply_task_patches.py --kind lite --language cn
```

The command updates the local `tasks_lite/` copy only. It does not modify the
downloaded Hugging Face repository, the raw workspace archive, or any remote
dataset.

The patched task contracts are intended as an interim local option. A new
version of Workspace-Bench with refreshed task metadata and assets will be
released separately.
