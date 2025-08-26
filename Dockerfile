FROM nikolaik/python-nodejs:python3.11-nodejs18

WORKDIR /app

# install gh cli
RUN curl -fsSL https://github.com/cli/cli/releases/download/v2.78.0/gh_2.78.0_linux_amd64.deb -o gh.deb \
    && apt-get install -y ./gh.deb \
    && rm gh.deb

# Install dependencies (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# RUN playwright install --with-deps chromium

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

CMD ["bash", "-c", "if [ ! -z \"$DB_URI\" ]; then python init_pg_checkpointer.py; fi && uvicorn main:app --host 0.0.0.0 --port 8000"]

# These commands document how to build the Docker image quickly and deploy to dockerhub
# docker buildx build --file Dockerfile --platform linux/amd64,linux/arm64 --tag kody06/llamabot-backend:latest --push .
# docker buildx build --file Dockerfile --platform linux/amd64,linux/arm64 --tag kody06/llamabot:0.1.17 --push .
# docker buildx build --file Dockerfile --platform linux/amd64 --tag kody06/llamabot:0.1.17 --push .
