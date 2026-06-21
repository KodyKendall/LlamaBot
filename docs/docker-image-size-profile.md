# Docker Image Size Profile

**File:** `Dockerfile`  
**Base image:** `nikolaik/python-nodejs:python3.11-nodejs18`  
**Target platform:** `linux/amd64`

This document profiles the estimated disk footprint and RAM usage for each layer of the LlamaBot Docker image. Numbers are estimates based on known package sizes; run `docker history kody06/llamabot:<tag>` against a built image for exact per-layer measurements.

---

## Disk footprint by layer

| Layer | What it installs | Estimated size |
|---|---|---|
| Base image (`nikolaik/python-nodejs:python3.11-nodejs18`) | Debian slim + Python 3.11 + Node 18 | ~500–600 MB |
| `apt-get` tools layer | `curl`, `gnupg`, `ca-certificates`, `ripgrep`, `util-linux`, `gh` CLI | ~80–120 MB |
| `pip install -r requirements.txt` | All Python packages (see breakdown below) | ~800 MB–1.1 GB |
| `playwright install --with-deps chromium` | Chromium browser binary + system deps | ~600–800 MB |
| `COPY . .` | App source code | ~5–20 MB |
| **Total (compressed on Docker Hub)** | | **~1.5–2.0 GB** |
| **Total (uncompressed on disk)** | | **~2.0–2.7 GB** |

---

## Python packages — major contributors

The `requirements.txt` has ~160 packages. The heavyweight ones:

| Package / group | Estimated size |
|---|---|
| `torch` / ML frameworks | _not present — intentionally excluded_ |
| `langchain` + `langgraph` stack (12 packages) | ~150–200 MB |
| `google-genai`, `google-api-core`, `google-auth`, `googleapis-common-protos`, `grpcio`, `proto-plus`, `protobuf` | ~100–150 MB |
| `langchain-google-vertexai`, `langchain-google-genai` | ~30–50 MB |
| `openai` + `anthropic` SDKs | ~30–40 MB |
| `numpy` | ~25 MB |
| `playwright` (Python client library only, not the browser) | ~10–15 MB |
| `tiktoken` + `regex` | ~15–20 MB |
| `SQLAlchemy` + `sqlmodel` + `alembic` | ~20 MB |
| `cryptography` + `bcrypt` + `PyJWT` + `truststore` | ~15 MB |
| `ipython` + `Pygments` + `rich` | ~30 MB |
| `fastapi` + `uvicorn` + `starlette` + `websockets` | ~15 MB |
| All other packages | ~100–150 MB |
| **Python packages total** | **~800 MB–1.1 GB** |

---

## Chromium layer — the largest single optional layer

`RUN playwright install --with-deps chromium` (line 25 of `Dockerfile`) is the most expensive optional layer:

| Component | Estimated size |
|---|---|
| Chromium browser binary (playwright-bundled build) | ~250–300 MB |
| System deps (`libglib`, `libnss`, `libatk`, `libx11`, fonts, etc.) | ~300–500 MB |
| **Total** | **~600–800 MB** |

This layer exists to support the `browser_inspect` tool (headless page diagnostics). It is used only on demand — Chromium is not a daemon and consumes no disk beyond what it occupies in the image layer.

**If `browser_inspect` is not needed in an environment**, removing this layer would reduce the image by ~25–35%.

---

## RAM usage at runtime

| Scenario | RAM consumed |
|---|---|
| Container idle (no active request) | ~150–250 MB |
| Active LangGraph request (LLM call + graph traversal) | +100–300 MB per concurrent request |
| `browser_inspect` call (headless Chromium launch) | +150–400 MB per active Chromium process |
| Chromium at rest (not in use) | +0 MB — process exits after each call |

Chromium is ephemeral: it spawns when `browser_inspect` is invoked and exits when done. Memory pressure only applies during active browser sessions.

---

## How to get exact numbers from a built image

```bash
# Per-layer breakdown (uncompressed sizes)
docker history kody06/llamabot:<tag> --no-trunc

# Total uncompressed image size
docker image inspect kody06/llamabot:<tag> --format '{{.Size}}'

# Disk used inside the running container
docker run --rm kody06/llamabot:<tag> du -sh /root/.cache/ms-playwright /usr/local/lib/python3.11

# Playwright chromium path specifically
docker run --rm kody06/llamabot:<tag> du -sh /root/.cache/ms-playwright
```

---

## Reduction opportunities

| Change | Estimated saving | Trade-off |
|---|---|---|
| Remove `playwright install --with-deps chromium` | ~600–800 MB | Lose `browser_inspect` tool |
| Use `playwright install chromium` (no `--with-deps`) | ~300–500 MB | Only works if deps already present; likely breaks on Debian slim |
| Move Chromium to a sidecar container | ~600–800 MB from main image | Requires inter-container communication for `browser_inspect` |
| Trim unused LangChain provider packages (Vertex, Ollama, QwQ) | ~50–100 MB | Lose those LLM backends |
| Multi-stage build (builder stage for pip, runtime stage for app) | ~200–400 MB | Build complexity; pip cache and build tools excluded from final image |
