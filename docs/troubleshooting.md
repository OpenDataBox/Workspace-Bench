# Troubleshooting

Common issues encountered when setting up or running Workspace-Bench evaluations.

## Docker Issues

### Docker build fails

**Symptom**: `docker compose build` exits with errors.

**Solutions**:

- Ensure Docker Daemon is running
- Check that you have sufficient disk space (workspaces can be large)
- Try building with `--no-cache`:
  ```bash
  docker compose -f docker/docker-compose.yaml build --no-cache
  ```

### Container cannot access workspaces

**Symptom**: Agent reports "workspace not found" or empty directories.

**Solutions**:

- Verify `WORKSPACE_BENCH_ROOT` is set correctly in the container (default: `/workspace/Workspace-Bench`)
- Ensure the volume mount `../..:/workspace/Workspace-Bench` is active
- Run `bash /workspace/Workspace-Bench/evaluation/docker/bootstrap.sh` inside the container

## API Key Issues

### Authentication errors

**Symptom**: Evaluation fails immediately with 401 or 403 errors.

**Solutions**:

- Double-check `.env` values for the model you are using
- Ensure the base URL ends without a trailing slash (some providers are sensitive)
- Verify the API key has sufficient quota

### Rate limiting (429)

**Symptom**: Intermittent failures with "Too Many Requests".

**Solutions**:

- Reduce `--max_workers` or workers in run config
- The evaluation harness has built-in exponential backoff, but very aggressive limits may still fail
- Consider running Lite first to validate setup before Full

## Data Download Issues

### HuggingFace download fails

**Symptom**: `download_hf_assets.py` hangs or errors.

**Solutions**:

- Ensure `huggingface_hub` is installed: `pip install huggingface_hub`
- Set `HF_TOKEN` if the dataset requires authentication
- Use `--cache_dir` to specify a download cache location with sufficient space

## Evaluation Issues

### All tasks timeout

**Symptom**: Every task returns `timeout` status.

**Solutions**:

- Increase `timeout_sec` in the run configuration (default is 300s)
- Check if the model endpoint is responding slowly
- Verify Docker container has enough CPU/memory resources

### Output files not found

**Symptom**: Tasks pass but output directories are empty.

**Solutions**:

- Check `agent.json` for the `retrievalMethod` field — it shows how outputs were collected
- Some agents write files to unexpected paths; the harness searches common locations
- Inspect `raw/stdout.txt` and `raw/stderr.txt` for agent execution logs

### Workdir rollback errors

**Symptom**: `rollback_error` appears in agent logs.

**Solutions**:

- This is usually non-fatal; the harness attempts to restore the workspace between tasks
- If workspaces become corrupted, delete `filesys/*_workdir_*` and re-run bootstrap

## Visualization Issues

### Dashboard shows no runs

**Symptom**: `npm run dev` starts but the UI shows "No runs found".

**Solutions**:

- Ensure `evaluation/output/` exists and contains run directories
- Check the browser console and API server logs for path resolution errors
- Verify the API server is running on the expected port (default 3000)

## Getting Help

If your issue is not covered here:

1. Check the logs in `evaluation/output/{run}/` for detailed error messages
2. Review the [GitHub Issues](https://github.com/OpenDataBox/Workspace-Bench/issues) page
3. Open a new issue with the task ID, harness, model, and relevant log excerpts
