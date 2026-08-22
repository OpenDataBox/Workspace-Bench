# DeepSeek Harness minimal Cordis configuration

`minimal.cordis.yml` is an unchanged copy of the official unattended minimal
Python SDK composition from DeepSeek Harness commit
`99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`:

<https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/examples/jsonrpc-agent/minimal.cordis.yml>

Its SHA-256 is
`4ddf99b5492fac7b578e3caddb0158815e44d5db176ba0aeab57012d35299fca`.
The adapter verifies this checksum before every run so benchmark configurations
cannot silently drift. The copied file is distributed under the adjacent MIT
`LICENSE` from the same upstream commit.

`office-skills.cordis.yml` is the Workspace-Bench default DSH profile. It is
derived from that same minimal composition and keeps its persistent Bash and
string-replace editor surface. It additionally enables DeepSeek Harness's
official `dsh-skill-filesystem` provider and `dsh-tool-skill` consumer against
only the image-installed `WORKSPACE_BENCH_DSH_SKILLS_DIR` Office skill pack.
It disables default project and user skill roots so a reported benchmark run
does not inherit untracked host-local Skills. Its SHA-256 is
`12c3f46e55a2306197b7811844430c21ff7736a643e74acb33fded6b42e127c4`.
