# User Agents Directory

This directory is reserved for user custom agents mounted from Leonardo projects.

## Mount Strategy

Leonardo projects mount their `langgraph/agents/` directory to this location via docker-compose:

```yaml
volumes:
  - ./langgraph/agents:/app/app/user_agents
```

## Built-in Agents

LlamaBot's core agents remain in `/app/app/agents/`:
- `rails_agent/` - Core Rails development agent
- `rails_ai_builder_agent/` - AI agent builder
- `rails_frontend_starter_agent/` - Frontend starter agent

## Custom Agents

User custom agents mount to `/app/app/user_agents/`:
- `leo/` - Leonardo's default agent
- `student/` - Example custom agent
- `{custom}/` - Any user-defined agents

## Agent Registration

All agents (built-in + custom) are registered in the Leonardo project's `langgraph.json`:

```json
{
  "dependencies": ["."],
  "graphs": {
    "rails_agent": "./agents/rails_agent/nodes.py:build_workflow",
    "rails_ai_builder": "./agents/rails_ai_builder_agent/nodes.py:build_workflow",
    "leo": "./user_agents/leo/nodes.py:build_workflow",
    "student": "./user_agents/student/nodes.py:build_workflow"
  }
}
```

## Path Convention

- **Built-in agents:** `./agents/{agent_name}/nodes.py:build_workflow`
- **Custom agents:** `./user_agents/{agent_name}/nodes.py:build_workflow`

The `agent_name` in AgentStateBuilder's `build()` method must match the key in `langgraph.json`.
