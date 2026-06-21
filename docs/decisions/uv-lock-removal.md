# Decision: Remove uv.lock, keep pip + requirements.txt

**Date:** 2026-06-21  
**Branch:** feat-disable-models-policy  
**Author:** Kody Kendall

## What happened

A `uv.lock` file was introduced as a side effect of local development work during the Playwright / `browser_inspect` tool addition. Someone (or a tool) ran `uv` locally against the existing `pyproject.toml`, which auto-generates a lockfile. It was never intentional.

## Why we removed it

The lockfile had no consumer. The Dockerfile installs dependencies via:

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt
```

`uv` is never called in the build pipeline. The lockfile was also generated from the minimal `pyproject.toml` dependency list (10 packages — mainly there for pytest config), not from the real `requirements.txt`, so it didn't even reflect the actual install surface. Committing it creates confusion about which tool manages dependencies.

## Future consideration: migrating to uv

`uv` is significantly faster than pip and produces reproducible, hash-verified installs. If we run into dependency instability issues (conflicting transitive deps, non-reproducible CI builds, slow `pip install` in Docker layer rebuilds), switching is worth evaluating. The migration would be:

1. Make `pyproject.toml` the source of truth (mirror `requirements.txt` into it)
2. Replace `pip install -r requirements.txt` in the Dockerfile with `uv sync`
3. Commit the generated `uv.lock` as the lockfile

For now, pip + `requirements.txt` works and is what the team knows. We're not switching for its own sake.
