FROM nikolaik/python-nodejs:python3.11-nodejs18

WORKDIR /app

# install gh cli, nsenter (for host command execution), and other tools
RUN rm -rf /var/lib/apt/lists/* && \
    apt-get update && apt-get install -y curl gnupg ca-certificates ripgrep util-linux && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*
    # curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    # echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian bullseye stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    # apt-get update && \
    # apt-get install -y docker-ce-cli && \

# OpenAI's Codex CLI — the AUTH HELPER for "run Leo on my own ChatGPT plan".
#
# Why a vendored binary rather than our own HTTP calls: auth.openai.com sits
# behind Cloudflare bot management that challenges our Python client (and curl)
# with a 403 HTML interstitial, while the Codex CLI's own client passes. Verified
# from the same host, same IP, same minute — so this is client fingerprinting,
# not an IP block, and no User-Agent change gets around it.
#
# Delegating to OpenAI's own signed client is also the most defensible option: the
# sign-in and token refresh happen inside their software doing exactly what it was
# built for, with nothing spoofed and no control bypassed.
#
# Used ONLY for `codex login --device-auth` / token refresh — never to run agents.
# Pinned so an upstream change can't silently alter the auth flow underneath us.
ENV CODEX_CLI_VERSION=0.147.0
RUN npm install -g @openai/codex@${CODEX_CLI_VERSION} && codex --version

# Install dependencies (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# docker buildx build --file Dockerfile --platform linux/amd64 --tag kody06/llamabot:0.2.19-saas --push .
# Install Chromium for browser_inspect tool (headless page diagnostics)
RUN playwright install --with-deps chromium

# Copy the rest of the app
COPY . .

# Change working directory to where the app code is located
WORKDIR /app/app

# Environment variables (all optional)
# DB_URI: PostgreSQL connection string (falls back to MemorySaver if not provided)
# Example: postgresql://user:password@host:port/database
ENV DB_URI=""

# Set PYTHONPATH so Python can find the app module regardless of working directory
ENV PYTHONPATH="/app:$PYTHONPATH"

# Expose port
EXPOSE 8000

CMD ["bash", "-c", "if [ ! -z \"$DB_URI\" ]; then python init_pg_checkpointer.py; fi && uvicorn main:app --host 0.0.0.0 --port 8000 --ws-max-size 157286400 --ws-ping-interval 60 --ws-ping-timeout 60"]

# These commands document how to build the Docker image quickly and deploy to dockerhub
# docker buildx build --file Dockerfile --platform linux/amd64,linux/arm64 --tag kody06/llamabot:0.5.0d --push .