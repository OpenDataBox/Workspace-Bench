# Workspace Filesystems

Workspace filesystem directories are intentionally not tracked in Git.

Expected local layout:

```text
evaluation/filesys/<role>_raw/
evaluation/filesys/<role>_standard/
evaluation/filesys/<role>_workdir_<harness>/
```

The `*_raw` and `*_standard` directories are large benchmark assets and should be
downloaded or restored outside Git. The `*_workdir_*` directories are mutable
runtime copies; the evaluation runner restores them after each task from the
corresponding standard workspace.
