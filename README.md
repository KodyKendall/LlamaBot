<div align="center">

<img src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" width="160" alt="LlamaBot logo">

# **LlamaBot**

*The open-source AI coding agent that rapidly builds MVPs, prototypes, and internal tools.*

[![Live Site](https://img.shields.io/badge/Visit-LlamaPress.ai-brightgreen?style=for-the-badge&logo=safari)](https://llamapress.ai)
[![LLM Prompts](https://img.shields.io/badge/LangSmith-Prompts-blue?style=for-the-badge&logo=langchain)](https://smith.langchain.com/hub/llamabot)
[![AGPL License](https://img.shields.io/github/license/KodyKendall/LlamaBot?style=for-the-badge)](LICENSE)
[![Discord](https://img.shields.io/badge/Join-Discord-7289DA?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/HtVVSxrK)

<img src="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/d7zial72abpkblr9n6lf8cov8lp4" width="600" alt="LlamaBot live demo">

</div>
---

## ✨ What is LlamaBot?

**LlamaBot is an AI coding agent that generates working prototypes, embeds AI directly into the app, and runs real workflows — letting you move from idea to production in record time.** 

It works across the full spectrum of users:
- **Non-technical founders** who want to build without code.
- **Product teams** who need to spin up prototypes fast.
- **Engineers** who want an AI teammate to automate workflows inside production apps.

Unlike typical codegen tools, LlamaBot doesn’t just write snippets — it can **embed directly in your app and run real workflows.** This makes it ideal for collaborative software building: founders guide the vision, engineers stay in control, and AI fills the gap.

---

LlamaBot is built for moving ideas fast:
- 🚀 *Prototype an AI MVP in a weekend* — landing pages, user flows, backend logic, all scaffolded by AI.
- 🧪 *Experiment with workflows* — test reporting, notifications, automations directly inside your app.
- 👥 *Collaborate with teammates* — Bridge the gap between non-technical founders and engineering teams.

---

## 🚀 Quick Start (<5 Minutes)

### Requires:
- Docker Compose
- OpenAI API Key

### Run the install script remotely (no Github clone)
```bash
# Only requirement: Docker + your OpenAI key
docker run -e OPENAI_API_KEY=sk-... -p 8000:8000 kody06/llamabot-backend
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
- Built on **LangGraph** for multi-step agent workflows
- **FastAPI + WebSockets** for streaming real-time responses
- Scoped memory per conversation/session
- Can call external APIs or internal app methods via whitelisted routes

---

## 📦 Project Structure
```
LlamaBot/
├── app/
│   ├── main.py            # FastAPI app with WebSocket + API routes
│   ├── chat.html          # Chat interface UI
│   ├── page.html          # Agent scratchpad to display visual UI to user, show results, etc. (Such as the Agent TODO list, etc.)
│   ├── agents/            # LangGraph agent logic
│   ├── main.py            # FastAPI app with WebSocket + API routes
│   ├── chat.html          # Chat interface UI
│   └── ...                # Utility code, workflows, memory, etc.
├── bin/
│   ├── install_llamabot_local.sh # local dev install script
│   └── install_llamabot_prod.sh  # production deployment script
├── docs/
│   └── dev_logs/
│      ├── ...
│      ├── v0.1.7
│      └── v0.2.0
├── Dockerfile             # Run backend anywhere
├── requirements.txt       # Python dependencies
└── README.md
```
---

## 🤝 Contributing
We welcome PRs, issues, and ideas! Jump into [Discord](https://discord.gg/HtVVSxrK) to collaborate.

---

## 📜 License
LlamaBot is **AGPLv3 open source**. For commercial licensing, contact [kody@llamapress.ai](mailto:kody@llamapress.ai).

<div align="center">
Made with ❤️ in San Francisco — inspired by the next wave of AI-powered software.
</div>