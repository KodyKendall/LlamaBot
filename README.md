<div align="center">

<img src="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/uj1j3g2ffgcifri2qiq591u00tvy" width="160" alt="LlamaBot logo">

# **LlamaBot**

*The open-source AI coding agent that rapidly builds MVPs, prototypes, and internal tools.*

[![Live Site](https://img.shields.io/badge/Visit-LlamaPress.ai-brightgreen?style=for-the-badge&logo=safari)](https://llamapress.ai)
[![LLM Prompts](https://img.shields.io/badge/LangSmith-Prompts-blue?style=for-the-badge&logo=langchain)](https://smith.langchain.com/hub/llamabot)
[![AGPL License](https://img.shields.io/github/license/KodyKendall/LlamaBot?style=for-the-badge)](LICENSE)
[![Discord](https://img.shields.io/badge/Join-Discord-7289DA?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/7DapkvmWKZ)

<img src="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/d7zial72abpkblr9n6lf8cov8lp4" width="600" alt="LlamaBot live demo">

</div>
---

## ✨ What is LlamaBot?

**LlamaBot is a production-ready AI coding agent framework that generates working prototypes, embeds AI directly into the app, and runs real workflows — letting you move from idea to production in record time.**

It works across the full spectrum of users:
- **Non-technical founders** who want to build without code.
- **Product teams** who need to spin up prototypes fast.
- **Engineers** who want an AI teammate to automate workflows inside production apps.

Unlike typical codegen tools, LlamaBot doesn't just write snippets — it can **embed directly in your app and run real workflows.** This makes it ideal for collaborative software building: founders guide the vision, engineers stay in control, and AI fills the gap.

---

LlamaBot is built for moving ideas fast:
- 🚀 *Prototype an AI MVP in a weekend* — landing pages, user flows, backend logic, all scaffolded by AI.
- 🧪 *Experiment with workflows* — test reporting, notifications, automations directly inside your app.
- 👥 *Collaborate with teammates* — Bridge the gap between non-technical founders and engineering teams.
- 🔧 *Production automation* — Trigger Sidekiq jobs, query databases, send emails/SMS, all from natural language.

---

## 🎯 Key Features

### **Multi-Agent Architecture**
LlamaBot includes **6 specialized agents** for different tasks:
- **`rails_agent`** - Full-featured Rails development with 20+ tools
- **`llamapress`** - HTML/webpage editing and website cloning
- **`rails_testing_agent`** - Automated test generation and execution
- **`rails_ai_builder_agent`** - Build custom LangGraph agents
- **`rails_frontend_starter_agent`** - Rapid frontend scaffolding
- **`llamabot`** - General-purpose helpful assistant

### **Powerful Tool Ecosystem (20+ Built-in Tools)**
- 📁 **File Operations**: Read, write, edit with smart whitespace normalization
- 🌿 **Git Integration**: Status, commit, push, full command execution
- 💻 **Bash Execution**: Run shell commands directly
- 🔍 **Internet Search**: Tavily-powered web search
- 🌐 **Web Scraping**: Playwright + BeautifulSoup for browser automation
- 🐙 **GitHub CLI**: Issue management, PR creation, workflow automation
- 📝 **Task Management**: Built-in TODO tracking with status updates

### **Multiple LLM Support**
Choose your preferred AI model:
- **Claude** (Anthropic) - Sonnet 4.5, Haiku 4.5 with prompt caching
- **GPT** (OpenAI) - GPT-5-Codex, GPT-4.1
- **Gemini** (Google) - Full Generative AI support

### **Real-Time Streaming**
- WebSocket-based streaming shows AI thinking token-by-token
- Live code preview with iframe updates
- Message history with thread management
- Mobile-responsive interface

### **Production-Ready Persistence**
- PostgreSQL checkpoint-based conversation state
- Graceful fallback to in-memory storage
- Connection pooling and automatic reconnection
- Message history trimming (prevents unbounded growth)

---

## 📊 By the Numbers

- **12,753 lines** of Python code across 71 source files
- **6 specialized agents** for different workflows
- **20+ built-in tools** covering file ops, git, web, and more
- **3 LLM providers** (Claude, GPT, Gemini)
- **1,392 lines** of comprehensive tests (unit + integration)
- **Production-ready** with PostgreSQL persistence, Nginx deployment, SSL/TLS

---

## 🚀 Quick Start (<5 Minutes)

### Requires:
- Docker Compose
- OpenAI API Key

### Run the install script remotely (no Github clone)
```bash
# Only requirement: docker compose + your OpenAI key
curl -fsSL "https://raw.githubusercontent.com/KodyKendall/LlamaBot/refs/heads/main/bin/install_llamabot_local.sh" -o install_llamabot_local.sh && bash install_llamabot_local.sh
```

Open your browser:
```
http://localhost:8000/chat
```

## 🚀 Dev Start (5-10 Minutes)

### Clone repo & run install script locally
```bash
git clone https://github.com/kodykendall/LlamaBot
cd LlamaBot
bash bin/install_llamabot_local.sh
```


## 🔌 Embed in an Existing App (Rails first, others coming)

Today, Rails is the primary supported framework. With the [`llama_bot_rails`](https://github.com/kodykendall/llama_bot_rails) gem, you can use LlamaBot to:
- Call existing ActiveRecord models
- Trigger your real services, jobs, and routes
- Automate workflows with natural language

**Example use cases:**
- Refund a user and send SMS
- Generate a weekly revenue report
- Queue 100 Sidekiq jobs from chat

Future adapters: Django, Laravel, Node.js.

> **Not a developer but want to build something with LlamaBot?** Join the Discord or reach out directly — we’d love to collaborate on real-world MVPs and prototypes.

---

## 🧠 Under the Hood

### **Core Architecture**
```
Browser (Modular ES6 JavaScript)
    ↕ WebSocket (Real-time streaming)
FastAPI Backend (Python 3.11+)
    ├─ LangGraph Workflows (6 specialized agents)
    ├─ 20+ Tools (file, git, bash, web, GitHub)
    ├─ PostgreSQL Checkpointer (conversation state)
    └─ Multiple LLM Providers (Claude, GPT, Gemini)
```

### **Tech Stack**
- **Agent Framework**: LangGraph 1.0.0 + LangChain
- **Web Framework**: FastAPI + Uvicorn (ASGI)
- **Communication**: WebSocket (starlette) with chunked streaming
- **Database**: PostgreSQL 16 (optional, falls back to in-memory)
- **Frontend**: Vanilla JavaScript with modular architecture
- **Observability**: LangSmith integration for tracing

### **Key Design Patterns**
- **Singleton Workflow Compilation**: Agents compiled once at startup, cached globally
- **Checkpoint-Based State**: All conversation history persisted to PostgreSQL
- **Streaming Architecture**: Real-time token-by-token message delivery
- **Tool Node Pattern**: LangGraph conditional edge routing
- **Graceful Degradation**: Falls back to MemorySaver if PostgreSQL unavailable

### **Smart Optimizations**
- **Prompt Caching** (Anthropic): Reduces costs by 90% for repeated calls
- **Path Normalization**: Automatically fixes common LLM path mistakes
- **Whitespace Normalization**: Flexible string matching for code edits
- **Failed Tool Recovery**: Switches to text-only after 3 failed tool calls
- **Message History Trimming**: Keeps last 30 messages + system messages

---

## 📦 Project Structure
```
LlamaBot/
├── app/
│   ├── main.py                    # FastAPI app with WebSocket + API routes (480 LOC)
│   ├── websocket/                 # WebSocket layer
│   │   ├── web_socket_handler.py  # Connection lifecycle management
│   │   ├── request_handler.py     # Core streaming logic (357 LOC)
│   │   └── web_socket_connection_manager.py
│   ├── agents/                    # LangGraph agent workflows
│   │   ├── llamabot/              # Basic assistant
│   │   ├── llamapress/            # HTML/webpage editor (supervisor pattern)
│   │   └── leonardo/              # Full-featured agents
│   │       ├── rails_agent/       # Rails development (1,342 LOC tools!)
│   │       ├── rails_testing_agent/
│   │       ├── rails_ai_builder_agent/
│   │       └── rails_frontend_starter_agent/
│   ├── frontend/                  # Modular JavaScript architecture
│   │   ├── index.html             # Chat interface
│   │   ├── chat/                  # ES6 modules
│   │   │   ├── index.js           # Main ChatApp class
│   │   │   ├── websocket/         # WebSocket management
│   │   │   ├── messages/          # Message rendering
│   │   │   ├── ui/                # UI components
│   │   │   └── threads/           # Thread management
│   │   └── styles/                # CSS
│   └── db/                        # Database utilities
├── bin/
│   ├── install_llamabot_local.sh  # Local dev install script
│   └── install_llamabot_prod.sh   # Production deployment script
├── tests/                         # Comprehensive test suite (1,392 LOC)
│   ├── test_app.py                # FastAPI endpoint tests
│   ├── test_websocket.py          # WebSocket tests
│   ├── test_agents.py             # Agent workflow tests
│   └── test_integration.py        # End-to-end tests
├── docs/
│   ├── DEPLOY.md                  # Production deployment guide
│   └── dev_logs/                  # Version history
├── langgraph.json                 # Agent configuration
├── Dockerfile                     # Container definition
├── docker-compose.yml             # Local development setup
├── requirements.txt               # Python dependencies
└── README.md
```
---

## 🚢 Deployment Options

### **1. Standalone (Docker)**
Run as an independent service with web UI:
```bash
docker-compose up
# Access at http://localhost:8000/chat
```

### **2. Embedded (Rails Integration)**
Integrate into existing Rails apps via the [`llama_bot_rails`](https://github.com/kodykendall/llama_bot_rails) gem:
```ruby
# Gemfile
gem 'llama_bot_rails'

# Can now:
# - Call ActiveRecord models
# - Trigger Sidekiq jobs
# - Send emails/SMS
# - Query databases
# - Automate workflows with natural language
```

### **3. Production (Multi-tenant SaaS)**
Full deployment guide available in [`docs/DEPLOY.md`](docs/DEPLOY.md):
- Ubuntu 20 LTS with systemd
- Nginx reverse proxy (HTTPS/WSS)
- PostgreSQL for persistence
- SSL/TLS via Certbot
- Multi-worker support (Uvicorn)

---

## ⚙️ Configuration

### **Environment Variables**
| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `OPENAI_API_KEY` | ✅ Yes | - | OpenAI LLM access |
| `ANTHROPIC_API_KEY` | ❌ No | - | Claude LLM access |
| `GOOGLE_API_KEY` | ❌ No | - | Gemini LLM access |
| `DB_URI` | ❌ No | "" | PostgreSQL connection (falls back to MemorySaver) |
| `TAVILY_API_KEY` | ❌ No | - | Internet search capability |
| `LANGSMITH_API_KEY` | ❌ No | - | LangSmith tracing/observability |
| `LANGSMITH_TRACING` | ❌ No | false | Enable LangSmith |

### **Agent Selection**
Configure available agents in `langgraph.json`:
```json
{
  "graphs": {
    "llamabot": "./agents/llamabot/nodes.py:build_workflow",
    "rails_agent": "./agents/leonardo/rails_agent/nodes.py:build_workflow",
    "llamapress": "./agents/llamapress/nodes.py:build_workflow"
  }
}
```

### **LLM Model Selection**
Choose your preferred model per conversation:
- `claude-4.5-sonnet` (default for rails_agent)
- `claude-4.5-haiku` (faster, cheaper)
- `gpt-5-codex` (OpenAI)
- `gpt-4.1` (OpenAI)
- `gemini-pro` (Google)

---

## 🧪 Testing

Run the comprehensive test suite:
```bash
pytest                           # Run all tests
pytest -m unit                   # Only unit tests
pytest -m integration            # Only integration tests
pytest --cov=app --cov-report=html  # Coverage report
```

Test coverage includes:
- FastAPI endpoint testing
- WebSocket communication
- Agent workflow execution
- End-to-end integration tests
- Mock LLM and database interactions

---

## 🤝 Contributing
We welcome PRs, issues, and ideas! Jump into [Discord](https://discord.gg/7DapkvmWKZ) to collaborate.
---

## 📜 License
LlamaBot is **AGPLv3 open source**. For commercial licensing, contact [kody@llamapress.ai](mailto:kody@llamapress.ai).

<div align="center">
Made with ❤️ in San Francisco — inspired by the next wave of AI-powered software.
</div>