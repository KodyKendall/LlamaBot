##
# LlamaBot Installation Script
# ---------------------------------------------------------------------
# This script installs LlamaBot, and a lightweight LlamaPress app (Rails app being modified) on a local machine.

# To run:
# curl -fsSL "https://raw.githubusercontent.com/KodyKendall/LlamaBot/refs/heads/rails-agent/bin/install_llamabot_local.sh" -o install_llamabot_local.sh && bash install_llamabot_local.sh
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
          \______         ))    LangGraph + Rails + Docker
             (           ))
             |           ))     LlamaBot (LangGraph) • LlamaPress (Rails)
             |           ))
             |           ))     v0.2.1
             |           ))
    

LLAMABOT_ASCII
  printf "${RESET}"
  printf "${CYAN}${BOLD}→ Kickstarting setup...${RESET} ${DIM}(press Ctrl+C to abort)${RESET}\n\n"
}

_show_llamabot_banner

# Prompt for OpenAI API Key
read -p "🦙🤖 Paste your OpenAI API Key: " OPENAI_API_KEY
export OPENAI_API_KEY

# Check for required tools
command -v openssl >/dev/null 2>&1 || { echo "openssl required but not installed. Aborting." >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "Docker required but not installed. Please install Docker Desktop." >&2; exit 1; }

# 2. Clone (or curl) config files
mkdir -p llamapress && cd llamapress
DIR=llamapress

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
    image: kody06/llamabot:0.2.1
    env_file:
      - .env
    command: bash -c "python init_pg_checkpointer.py --uri $$DB_URI && uvicorn main:app --host 0.0.0.0 --port 8000"
    volumes:
      - auth_data:/app/auth.json
    ports:
      - "8000:8000"
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
  auth_data:

# Declare the external network
networks:
  llama-network:
    name: llama-network
EOF

# 7. Pull images & launch everything
docker compose pull          # fetch images

docker compose up -d         # start in the background

docker compose exec llamapress bash -c "sleep 5 && bundle exec rails db:migrate"
docker compose exec llamabot rm -rf /app/app/rails

echo "Copying Rails application files from the container..."

# Use 'docker exec' and 'tar' to stream files. This avoids the file
# ownership issues that occur with 'docker cp' and eliminates the need
# for 'sudo chown', providing a better experience for developers.
# The tar stream from the container is extracted directly on the host,
# resulting in files owned by the current user.
docker compose exec llamapress tar cf - -C /rails app db config/routes.rb | tar xf - -C rails

# Set appropriate permissions for the copied files.
chmod -R 755 rails/

cd rails
rm -rf .git
git init

# Set user details for the local repository only. This avoids modifying the
# developer's global Git configuration, which is a much safer and less
# intrusive approach.
git config user.name "Leonardo"
git config user.email "leonardo@llamapress.ai"

git add .
git commit -m "init"
cd ..

docker compose exec -it llamabot git config --global --add safe.directory /app/app/rails

docker compose down
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
      - ./rails/app:/rails/app
      - ./rails/config/routes.rb:/rails/config/routes.rb
      - ./rails/db:/rails/db
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
    image: kody06/llamabot:0.2.1
    env_file:
      - .env
    command: bash -c "python init_pg_checkpointer.py --uri $DB_URI && uvicorn main:app --host 0.0.0.0 --port 8000"
    volumes:
      - ./rails:/app/app/rails
      - auth_data:/app/auth.json
      - /var/run/docker.sock:/var/run/docker.sock
      # - ~/.config/gh:/root/.config/gh:ro
    ports:
      - "8000:8000"
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
  auth_data:
  
# Declare the external network
networks:
  llama-network:
    name: llama-network
EOF

echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@" 
echo "Note! Password required for this next step." 
echo "Sudo needed for this last docker command, in order to let the docker containers communicate with each other."
echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@" 

sudo docker compose up -d         # start in the background

sudo docker compose exec llamapress bash -c "bundle exec rails db:migrate"

echo "🎉 Done! Open http://localhost:8000 to build with Leonardo the Llama!"