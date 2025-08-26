##
# LlamaBot Installation Script
# ---------------------------------------------------------------------
# This script installs Docker, Docker Compose, LlamaBot, and LlamaPress on a 
# fresh Ubuntu 24.04 instance. It also sets up Caddy as a reverse proxy.
# To run:
# https://raw.githubusercontent.com/KodyKendall/LlamaBot/refs/heads/rails-agent/bin/install_llamabot_prod.sh
# ---------------------------------------------------------------------

#!/usr/bin/env bash
set -e

# --- Pretty banner ----------------------------------------------------
_show_llamabot_banner() {
  # Respect non-interactive logs/CI or NO_BANNER=1
  if [ ! -t 1 ] || [ -n "${CI:-}" ] || [ -n "${NO_BANNER:-}" ]; then
    return
  fi

  # Simple color detection
  local BOLD="" DIM="" PURPLE="" CYAN="" RESET=""
  if [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    BOLD="\033[1m"; DIM="\033[2m"; PURPLE="\033[35m"; CYAN="\033[36m"; RESET="\033[0m"
  fi

  printf "${PURPLE}${BOLD}"
  cat <<'LLAMABOT_ASCII'
   _      _                           _          _   
  | |    | |  __ _  _ __  __    __ _ | | _  ___ | |_ 
  | |    | | / _` || '_ \ _ \  / _` ||  _ \/ _ \| __|
  | |___ | || (_| || | | | | || (_| || |_)  |_| | |_ 
  |_____||_| \__,_||_| |_| |_| \__,_||____/\___/ \__|
                                                           
                   (\     (\
                  (  )   (  )
                 (   )___(  )
         ______ /           )
        {                   )
         |                 ))   L L A M A B O T   I N S T A L L E R
          \______         ))    LangGraph + Rails + Docker + Caddy
             (           ))
             |           ))     LlamaBot (LangGraph) • LlamaPress (Rails)
             |           ))
             |           ))     v0.1.17
             |           ))
    

LLAMABOT_ASCII
  printf "${RESET}"
  printf "${CYAN}${BOLD}→ Kickstarting setup...${RESET} ${DIM}(press Ctrl+C to abort)${RESET}\n\n"
}

_show_llamabot_banner

# Prompt for OpenAI API Key
read -p "🦙🤖 Paste your OpenAI API Key: " OPENAI_API_KEY
export OPENAI_API_KEY

# Prompt for Hosted Domain
read -p "🌐 Enter your hosted domain (e.g., example.com): " HOSTED_DOMAIN
export HOSTED_DOMAIN
RAILS_HOSTED_DOMAIN=rails.$HOSTED_DOMAIN

# ---------------------------------------------------------------------
# 1. Install Docker & Compose if not present -- non-interactive
# ---------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    # Silence all interactive prompts and keep local config files
    export DEBIAN_FRONTEND=noninteractive
    APT_OPTS='-y -q'
    # 1-a  Update package index & apply security upgrades
    sudo apt-get update

    # 1-b  Install Docker prerequisites & add Docker’s APT repo
    sudo apt-get $APT_OPTS install ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
         sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo apt-get update
    sudo apt-get $APT_OPTS install docker-ce docker-ce-cli \
                                   containerd.io docker-buildx-plugin \
                                   docker-compose-plugin

    # 1-c  Enable & start Docker daemon
    sudo systemctl enable --now docker

    # 1-d  (Optional) Allow current user to run Docker without sudo
    sudo usermod -aG docker "$USER"
    
    sudo mkdir -p /etc/docker
    cat <<'EOF' | sudo tee /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
EOF

    sudo systemctl restart docker

    sudo mkdir -p /etc/systemd/journald.conf.d
    cat <<'EOF' | sudo tee /etc/systemd/journald.conf.d/size.conf
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=50M
EOF

    sudo systemctl restart systemd-journald    
fi

# 2. Clone (or curl) config files
mkdir -p llamapress && cd llamapress

# 3. Generate secrets
NEW_KEY=$(openssl rand -hex 64)
POSTGRES_PASSWORD=$(openssl rand -hex 16)

# [Generate POSTGRES_PASSWORD, SECRET_KEY_BASE, etc...]

# 4. Write .env file
cat <<EOF > .env
TAVILY_API_KEY=tvly-dev-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Necessary:
LLAMABOT_API_URL="http://llamabot:8000"
REDIS_URL="redis://redis:6379/1"
LLAMABOT_WEBSOCKET_URL="ws://llamabot:8000/ws"
LLAMAPRESS_API_URL="http://llamapress:3000"

AWS_KEY='your-access-key'
AWS_PASS='your-secret-key'
AWS_BUCKET='your-bucket-name'
AWS_REGION='your-region'

# A Record Domain to this specific LlamaPress, Needed for pages#home controller method if you want multi-site routing.
# HOSTED_DOMAIN="llamapress.ai" 
OPENAI_API_KEY=${OPENAI_API_KEY}

POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DB_URI="postgresql://postgres:${POSTGRES_PASSWORD}@db:5432/llamapress_production"
DATABASE_URL="postgresql://postgres:${POSTGRES_PASSWORD}@db:5432/llamapress_production"
SECRET_KEY_BASE=${NEW_KEY}
EOF

mkdir -p rails/app rails/config rails/db 
touch rails/config/routes.rb
DIR=llamapress

# 5. Write docker-compose.yml
cat <<EOF > docker-compose.yml
version: "3.8"

services:
  llamapress:
    image: kody06/llamapress-simple:0.1.17          # <— pre-built tag
    env_file: .env                           # read secrets from this file
    environment:
      - RAILS_ENV=development
      - BOOTSNAP_CACHE_DIR=/rails/tmp/cache/bootsnap
    volumes:
      - rails_storage:/rails/storage         # ActiveStorage local files
    command: bash -c "rm -f tmp/pids/server.pid && bundle exec rails db:prepare && bundle exec rails s -b 0.0.0.0"
    ports:
      - "3000:3000"                            # http://server_ip/
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
    networks:
      - llama-network

  llamabot:
    image: kody06/llamabot:0.1.17
    env_file:
      - .env
    command: bash -c "python init_pg_checkpointer.py --uri $$DB_URI && uvicorn main:app --host 0.0.0.0 --port 8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      # - ~/.config/gh:/root/.config/gh:ro
    ports:
      - "8080:8000"
    depends_on:
      db:
        condition: service_healthy
    networks:
      - llama-network

  db:
    image: postgres:16
    environment:
      POSTGRES_DB:      llamapress_production
      POSTGRES_USER:    postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped
    networks:
      - llama-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d llamapress_production"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    volumes: [redis_data:/data]
    restart: unless-stopped
    networks:
      - llama-network
      
volumes:
  postgres_data:
  redis_data:
  rails_storage:
  
# Declare the external network
networks:
  llama-network:
    name: llama-network
EOF

# 6. Install & configure Caddy (as reverse proxy)
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] \
https://dl.cloudsmith.io/public/caddy/stable/deb/debian/ any-version main" \
| sudo tee /etc/apt/sources.list.d/caddy-stable.list

sudo apt-get install -y caddy

caddy version #ensure it's installed

# Build the Caddyfile: domain if supplied, otherwise plain :80
if [ -n "$HOSTED_DOMAIN" ]; then
  SERVER_NAME="$HOSTED_DOMAIN"
else
  SERVER_NAME=":80"
fi

sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${SERVER_NAME} {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
rails.${SERVER_NAME} {
    encode gzip
    reverse_proxy 127.0.0.1:3000
}
EOF

# Open firewall for web traffic (Lightsail Console UI already did 443, but do both)
sudo ufw allow 22/tcp || true
sudo ufw allow 80/tcp || true
sudo ufw allow 443/tcp || true

sudo systemctl reload caddy

# verify caddy logs
journalctl -u caddy --no-pager --since "10 minutes ago"

# 7. Pull images & launch everything
sudo docker compose pull          # fetch images

sudo docker compose up -d         # start in the background

sudo docker compose exec llamapress bundle exec rails db:migrate && sudo docker compose exec llamapress bundle exec rails db:seed
sudo docker compose exec llamabot rm -rf /app/app/rails

sudo docker cp $DIR-llamapress-1:/rails/app/ rails
sudo docker cp $DIR-llamapress-1:/rails/config/routes.rb rails/config/routes.rb 
sudo docker cp $DIR-llamapress-1:/rails/db rails

cd rails
rm -rf .git
git init
git config --global user.name "Leonardo"
git config --global user.email "leonardo@llamapress.ai"
git config --global --add safe.directory /app/app/rails
git add .
git commit -m "init"
cd ..

sudo docker compose exec -it llamabot git config --global --add safe.directory /app/app/rails

sudo docker compose down
mv docker-compose.yml docker-compose.yml.old

cat <<EOF > docker-compose.yml
version: "3.8"

services:
  llamapress:
    image: kody06/llamapress-simple:0.1.17          # <— pre-built tag
    env_file: .env                           # read secrets from this file
    environment:
      - RAILS_ENV=development
      - BOOTSNAP_CACHE_DIR=/rails/tmp/cache/bootsnap
    volumes:
      - rails_storage:/rails/storage         # ActiveStorage local files
      - ./rails/app:/rails/app:delegated
      - ./rails/config/routes.rb:/rails/config/routes.rb:delegated
      - ./rails/db:/rails/db:delegated
    command: bash -c "rm -f tmp/pids/server.pid && bundle exec rails db:prepare && bundle exec rails s -b 0.0.0.0"
    ports:
      - "3000:3000"                            # http://server_ip/
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
    networks:
      - llama-network

  llamabot:
    image: kody06/llamabot:0.1.17
    env_file:
      - .env
    command: bash -c "python init_pg_checkpointer.py --uri $DB_URI && uvicorn main:app --host 0.0.0.0 --port 8000"
    volumes:
      - ./rails:/app/app/rails
      - /var/run/docker.sock:/var/run/docker.sock
    ports:
      - "8080:8000"
    depends_on:
      db:
        condition: service_healthy
    networks:
      - llama-network

  db:
    image: postgres:16
    environment:
      POSTGRES_DB:      llamapress_production
      POSTGRES_USER:    postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped
    networks:
      - llama-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d llamapress_production"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    volumes: [redis_data:/data]
    restart: unless-stopped
    networks:
      - llama-network
      
volumes:
  postgres_data:
  redis_data:
  rails_storage:
  
# Declare the external network
networks:
  llama-network:
    name: llama-network
EOF

sudo docker compose up -d         # start in the background
# sudo docker compose logs -f llamapress   # watch until you see:

sudo docker compose exec llamapress bundle exec rails db:migrate && sudo docker compose exec llamapress bundle exec rails db:seed

curl -I http://localhost:8080/health

# 8. Print URL
echo "🎉 Leonardo is deployed!"