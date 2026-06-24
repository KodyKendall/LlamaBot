https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills

# Agent Skills

Agent Skills are modular capabilities that extend Claude's functionality. Each Skill packages instructions, metadata, and optional resources (scripts, templates) that Claude uses automatically when relevant.

---

<Note>
This feature is **not** eligible for [Zero Data Retention (ZDR)](/docs/en/build-with-claude/api-and-data-retention). Data is retained according to the feature's standard retention policy.
</Note>

## Why use Skills

Skills are reusable, filesystem-based resources that provide Claude with domain-specific expertise: workflows, context, and best practices that transform general-purpose agents into specialists. Unlike prompts (conversation-level instructions for one-off tasks), Skills load on-demand and eliminate the need to repeatedly provide the same guidance across multiple conversations.

**Key benefits**:
- **Specialize Claude**: Tailor capabilities for domain-specific tasks
- **Reduce repetition**: Create once, use automatically
- **Compose capabilities**: Combine Skills to build complex workflows

<Note>
For a deep dive into the architecture and real-world applications of Agent Skills, see the engineering blog post [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills).
</Note>

## Using Skills

Anthropic provides pre-built Agent Skills for common document tasks (PowerPoint, Excel, Word, PDF), and you can create your own custom Skills. Both work the same way. Claude automatically uses them when relevant to your request.

**Pre-built Agent Skills** are available on claude.ai, the Claude API, [Claude Platform on AWS](/docs/en/build-with-claude/claude-platform-on-aws), and [Microsoft Foundry](/docs/en/build-with-claude/claude-in-microsoft-foundry). See [Available Skills](#available-skills) for the complete list.

**Custom Skills** let you package domain expertise and organizational knowledge. They're available across Claude's products: create them in Claude Code, upload them through the Claude API, or add them in claude.ai settings. On [Claude Platform on AWS](/docs/en/build-with-claude/claude-platform-on-aws) and [Microsoft Foundry](/docs/en/build-with-claude/claude-in-microsoft-foundry), upload custom Skills through the Skills API.

<Note>
**Get started:**
- For pre-built Agent Skills: See the [quickstart tutorial](/docs/en/agents-and-tools/agent-skills/quickstart) to start using PowerPoint, Excel, Word, and PDF skills in the API
- For custom Skills: See the [Agent Skills Cookbook](https://platform.claude.com/cookbook/skills-notebooks-01-skills-introduction) to learn how to create your own Skills
</Note>

## How Skills work

Skills leverage Claude's VM environment to provide capabilities beyond what's possible with prompts alone. Claude operates in a virtual machine with filesystem access, allowing Skills to exist as directories containing instructions, executable code, and reference materials, organized like an onboarding guide you'd create for a new team member.

This filesystem-based architecture enables **progressive disclosure**: Claude loads information in stages as needed, rather than consuming context upfront.

### Three types of Skill content, three levels of loading

Skills can contain three types of content, each loaded at different times:

### Level 1: Metadata (always loaded)

**Content type: Instructions**. The Skill's YAML frontmatter provides discovery information:

```yaml
---
name: pdf-processing
description: Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction.
---
```

Claude loads this metadata at startup and includes it in the system prompt. This lightweight approach means you can install many Skills without context penalty; Claude only knows each Skill exists and when to use it.

### Level 2: Instructions (loaded when triggered)

**Content type: Instructions**. The main body of SKILL.md contains procedural knowledge: workflows, best practices, and guidance:

````markdown
# PDF Processing

## Quick start

Use pdfplumber to extract text from PDFs:

```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    text = pdf.pages[0].extract_text()
```

For advanced form filling, see [FORMS.md](FORMS.md).
````

When you request something that matches a Skill's description, Claude reads SKILL.md from the filesystem via bash. Only then does this content enter the context window.

### Level 3: Resources and code (loaded as needed)

**Content types: Instructions, code, and resources**. Skills can bundle additional materials:

```text nowrap
pdf-skill/
├── SKILL.md (main instructions)
├── FORMS.md (form-filling guide)
├── REFERENCE.md (detailed API reference)
└── scripts/
    └── fill_form.py (utility script)
```

**Instructions**: Additional markdown files (FORMS.md, REFERENCE.md) containing specialized guidance and workflows

**Code**: Executable scripts (fill_form.py, validate.py) that Claude runs via bash; scripts provide deterministic operations without consuming context

**Resources**: Reference materials like database schemas, API documentation, templates, or examples

Claude accesses these files only when referenced. The filesystem model means each content type has different strengths: instructions for flexible guidance, code for reliability, resources for factual lookup.

| Level | When Loaded | Token Cost | Content |
|-------|------------|------------|---------|
| **Level 1: Metadata** | Always (at startup) | ~100 tokens per Skill | `name` and `description` from YAML frontmatter |
| **Level 2: Instructions** | When Skill is triggered | Under 5k tokens | SKILL.md body with instructions and guidance |
| **Level 3+: Resources** | As needed | Effectively unlimited | Bundled files executed via bash without loading contents into context |

Progressive disclosure ensures only relevant content occupies the context window at any given time.

### The Skills architecture

Skills run in a code execution environment where Claude has filesystem access, bash commands, and code execution capabilities. Think of it like this: Skills exist as directories on a virtual machine, and Claude interacts with them using the same bash commands you'd use to navigate files on your computer.

![Agent Skills Architecture - showing how Skills integrate with the agent's configuration and virtual machine](/docs/images/agent-skills-architecture.png)

**How Claude accesses Skill content:**

When a Skill is triggered, Claude uses bash to read SKILL.md from the filesystem, bringing its instructions into the context window. If those instructions reference other files (like FORMS.md or a database schema), Claude reads those files too using additional bash commands. When instructions mention executable scripts, Claude runs them via bash and receives only the output (the script code itself never enters context).

**What this architecture enables:**

**On-demand file access**: Claude reads only the files needed for each specific task. A Skill can include dozens of reference files, but if your task only needs the sales schema, Claude loads just that one file. The rest remain on the filesystem consuming zero tokens.

**Efficient script execution**: When Claude runs `validate_form.py`, the script's code never loads into the context window. Only the script's output (like "Validation passed" or specific error messages) consumes tokens. This makes scripts far more efficient than having Claude generate equivalent code on the fly.

**No practical limit on bundled content**: Because files don't consume context until accessed, Skills can include comprehensive API documentation, large datasets, extensive examples, or any reference materials you need. There's no context penalty for bundled content that isn't used.

This filesystem-based model is what makes progressive disclosure work. Claude navigates your Skill like you'd reference specific sections of an onboarding guide, accessing exactly what each task requires.

### Example: Loading a PDF processing skill

Here's how Claude loads and uses a PDF processing skill:

1. **Startup**: System prompt includes: `PDF Processing - Extract text and tables from PDF files, fill forms, merge documents`
2. **User request**: "Extract the text from this PDF and summarize it"
3. **Claude invokes**: `bash: read pdf-skill/SKILL.md` → Instructions loaded into context
4. **Claude determines**: Form filling is not needed, so FORMS.md is not read
5. **Claude executes**: Uses instructions from SKILL.md to complete the task

![Skills loading into context window - showing the progressive loading of skill metadata and content](/docs/images/agent-skills-context-window.png)

The diagram shows:
1. Default state with system prompt and skill metadata pre-loaded
2. Claude triggers the skill by reading SKILL.md via bash
3. Claude optionally reads additional bundled files like FORMS.md as needed
4. Claude proceeds with the task

This dynamic loading ensures only relevant skill content occupies the context window.

## Where Skills work

Skills are available across Claude's agent products:

<Note>
Claude Platform on AWS and Microsoft Foundry inherit the same Skills behavior as the Claude API in all following sections.
</Note>

### Claude API

The Claude API supports both pre-built Agent Skills and custom Skills. Both work identically: specify the relevant `skill_id` in the `container` parameter along with the code execution tool.

**Prerequisites**: Using Skills via the API requires three beta headers:
- `code-execution-2025-08-25` - Skills run in the code execution container
- `skills-2025-10-02` - Enables Skills functionality
- `files-api-2025-04-14` - Required for uploading/downloading files to/from the container

Use pre-built Agent Skills by referencing their `skill_id` (for example, `pptx`, `xlsx`), or create and upload your own through the Skills API (`/v1/skills` endpoints). Custom Skills are shared workspace-wide; all workspace members can access them.

To learn more, see [Use Skills with the Claude API](/docs/en/build-with-claude/skills-guide).

### Claude Code

[Claude Code](https://code.claude.com/docs/en/overview) supports only Custom Skills.

**Custom Skills**: Create Skills as directories with SKILL.md files. Claude discovers and uses them automatically.

Custom Skills in Claude Code are filesystem-based and don't require API uploads.

To learn more, see [Use Skills in Claude Code](https://code.claude.com/docs/en/skills).

### claude.ai

[claude.ai](https://claude.ai) supports both pre-built Agent Skills and custom Skills.

**Pre-built Agent Skills**: These Skills are already working behind the scenes when you create documents. Claude uses them without requiring any setup.

**Custom Skills**: Upload your own Skills as zip files through Settings > Features. Available on Pro, Max, Team, and Enterprise plans with code execution enabled. Custom Skills are individual to each user; they are not shared organization-wide and cannot be centrally managed by admins.

To learn more about using Skills in claude.ai, see the following resources in the Claude Help Center:
- [What are Skills?](https://support.claude.com/en/articles/12512176-what-are-skills)
- [Using Skills in Claude](https://support.claude.com/en/articles/12512180-using-skills-in-claude)
- [How to create custom Skills](https://support.claude.com/en/articles/12512198-creating-custom-skills)
- [Teach Claude your way of working using Skills](https://support.claude.com/en/articles/12580051-teach-claude-your-way-of-working-using-skills)

## Skill structure

Every Skill requires a `SKILL.md` file with YAML frontmatter:

```yaml
---
name: your-skill-name
description: Brief description of what this Skill does and when to use it
---

# Your Skill Name

## Instructions
[Clear, step-by-step guidance for Claude to follow]

## Examples
[Concrete examples of using this Skill]
```

**Required fields**: `name` and `description`

**Field requirements**:

`name`:
- Maximum 64 characters
- Must contain only lowercase letters, numbers, and hyphens
- Cannot contain XML tags
- Cannot contain reserved words: "anthropic", "claude"

`description`:
- Must be non-empty
- Maximum 1024 characters
- Cannot contain XML tags

The `description` should include both what the Skill does and when Claude should use it. For complete authoring guidance, see the [best practices guide](/docs/en/agents-and-tools/agent-skills/best-practices).

## Security considerations

Use Skills only from trusted sources: those you created yourself or obtained from Anthropic. Skills provide Claude with new capabilities through instructions and code, and while this makes them powerful, it also means a malicious Skill can direct Claude to invoke tools or execute code in ways that don't match the Skill's stated purpose.

<Warning>
If you must use a Skill from an untrusted or unknown source, exercise extreme caution and thoroughly audit it before use. Depending on what access Claude has when executing the Skill, malicious Skills could lead to data exfiltration, unauthorized system access, or other security risks.
</Warning>

**Key security considerations**:
- **Audit thoroughly**: Review all files bundled in the Skill: SKILL.md, scripts, images, and other resources. Look for unusual patterns like unexpected network calls, file access patterns, or operations that don't match the Skill's stated purpose
- **External sources are risky**: Skills that fetch data from external URLs pose particular risk, as fetched content may contain malicious instructions. Even trustworthy Skills can be compromised if their external dependencies change over time
- **Tool misuse**: Malicious Skills can invoke tools (file operations, bash commands, code execution) in harmful ways
- **Data exposure**: Skills with access to sensitive data could be designed to leak information to external systems
- **Treat like installing software**: Only use Skills from trusted sources. Be especially careful when integrating Skills into production systems with access to sensitive data or critical operations

## Available Skills

### Pre-built Agent Skills

The following pre-built Agent Skills are available for immediate use:

- **PowerPoint (pptx)**: Create presentations, edit slides, analyze presentation content
- **Excel (xlsx)**: Create spreadsheets, analyze data, generate reports with charts
- **Word (docx)**: Create documents, edit content, format text
- **PDF (pdf)**: Generate formatted PDF documents and reports

These Skills are available on the Claude API, [Claude Platform on AWS](/docs/en/build-with-claude/claude-platform-on-aws), [Microsoft Foundry](/docs/en/build-with-claude/claude-in-microsoft-foundry), and claude.ai. See the [quickstart tutorial](/docs/en/agents-and-tools/agent-skills/quickstart) to start using them in the API.

### Open-source Skills

Anthropic also publishes open-source Skills in the [skills repository](https://github.com/anthropics/skills):

- **[Claude API](/docs/en/agents-and-tools/agent-skills/claude-api-skill)**: Provides Claude with up-to-date API reference material, SDK documentation, and best practices for 8 programming languages. Bundled with Claude Code and also available for installation from the skills repository.

### Custom Skills examples

For complete examples of custom Skills, see the [Skills cookbook](https://platform.claude.com/cookbook/skills-notebooks-01-skills-introduction).

## Data retention

Agent Skills is not covered by ZDR arrangements. Skill definitions and execution data are retained according to Anthropic's standard data retention policy.

For ZDR eligibility across all features, see [API and data retention](/docs/en/manage-claude/api-and-data-retention).

## Limitations and constraints

Understanding these limitations helps you plan your Skills deployment effectively. Claude Platform on AWS and Microsoft Foundry follow the same limitations as the Claude API in the following subsections.

### Cross-surface availability

**Custom Skills do not sync across surfaces**. Skills uploaded to one surface are not automatically available on others:

- Skills uploaded to claude.ai must be separately uploaded to the API
- Skills uploaded through the API are not available on claude.ai
- Claude Code Skills are filesystem-based and separate from both claude.ai and API

You'll need to manage and upload Skills separately for each surface where you want to use them.

### Sharing scope

Skills have different sharing models depending on where you use them:
- **claude.ai**: Individual user only; each team member must upload separately
- **Claude API**: Workspace-wide; all workspace members can access uploaded Skills
- **Claude Code**: Personal (`~/.claude/skills/`) or project-based (`.claude/skills/`); can also be shared via Claude Code Plugins

claude.ai does not support centralized admin management or org-wide distribution of custom Skills.

### Runtime environment constraints

The exact runtime environment available to your skill depends on the product surface where you use it.

 - **claude.ai**:
    - **Varying network access**: Depending on user/admin settings, Skills may have full, partial, or no network access. For more details, see the [Create and Edit Files](https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude#h_6b7e833898) support article.
- **Claude API**:
    - **No network access**: Skills cannot make external API calls or access the internet
    - **No runtime package installation**: Only pre-installed packages are available. You cannot install new packages during execution.
    - **Pre-configured dependencies only**: Check the [code execution tool documentation](/docs/en/agents-and-tools/tool-use/code-execution-tool) for the list of available packages
- **Claude Code**:
    - **Full network access**: Skills have the same network access as any other program on the user's computer
    - **Global package installation discouraged**: Skills should only install packages locally in order to avoid interfering with the user's computer

Plan your Skills to work within these constraints.

## Next steps

<CardGroup cols={2}>
  <Card
    title="Get started with Agent Skills"
    icon="graduation-cap"
    href="/docs/en/agents-and-tools/agent-skills/quickstart"
  >
    Create your first Skill
  </Card>
  <Card
    title="API Guide"
    icon="code"
    href="/docs/en/build-with-claude/skills-guide"
  >
    Use Skills with the Claude API
  </Card>
  <Card
    title="Use Skills in Claude Code"
    icon="terminal"
    href="https://code.claude.com/docs/en/skills"
  >
    Create and manage custom Skills in Claude Code
  </Card>
  <Card
    title="Authoring best practices"
    icon="lightbulb"
    href="/docs/en/agents-and-tools/agent-skills/best-practices"
  >
    Write Skills that Claude can use effectively
  </Card>
</CardGroup>

Equipping agents for the real world with Agent Skills
Published Oct 16, 2025

Claude is powerful, but real work requires procedural knowledge and organizational context. Introducing Agent Skills, a new way to build specialized agents using files and folders.

Update: We've published Agent Skills as an open standard for cross-platform portability. (December 18, 2025)

As model capabilities improve, we can now build general-purpose agents that interact with full-fledged computing environments. Claude Code, for example, can accomplish complex tasks across domains using local code execution and filesystems. But as these agents become more powerful, we need more composable, scalable, and portable ways to equip them with domain-specific expertise.

This led us to create Agent Skills: organized folders of instructions, scripts, and resources that agents can discover and load dynamically to perform better at specific tasks. Skills extend Claude’s capabilities by packaging your expertise into composable resources for Claude, transforming general-purpose agents into specialized agents that fit your needs.

Building a skill for an agent is like putting together an onboarding guide for a new hire. Instead of building fragmented, custom-designed agents for each use case, anyone can now specialize their agents with composable capabilities by capturing and sharing their procedural knowledge. In this article, we explain what Skills are, show how they work, and share best practices for building your own.

To activate skills, all you need to do is write a SKILL.md file with custom guidance for your agent.
A skill is a directory containing a SKILL.md file that contains organized folders of instructions, scripts, and resources that give agents additional capabilities.
The anatomy of a skill
To see Skills in action, let’s walk through a real example: one of the skills that powers Claude’s recently launched document editing abilities. Claude already knows a lot about understanding PDFs, but is limited in its ability to manipulate them directly (e.g. to fill out a form). This PDF skill lets us give Claude these new abilities.

At its simplest, a skill is a directory that contains a SKILL.md file. This file must start with YAML frontmatter that contains some required metadata: name and description. At startup, the agent pre-loads the name and description of every installed skill into its system prompt.

This metadata is the first level of progressive disclosure: it provides just enough information for Claude to know when each skill should be used without loading all of it into context. The actual body of this file is the second level of detail. If Claude thinks the skill is relevant to the current task, it will load the skill by reading its full SKILL.md into context.

Anatomy of a SKILL.md file including the relevant metadata: name, description, and context related to the specific actions the skill should take.
A SKILL.md file must begin with YAML Frontmatter that contains a file name and description, which is loaded into its system prompt at startup.
As skills grow in complexity, they may contain too much context to fit into a single SKILL.md, or context that’s relevant only in specific scenarios. In these cases, skills can bundle additional files within the skill directory and reference them by name from SKILL.md. These additional linked files are the third level (and beyond) of detail, which Claude can choose to navigate and discover only as needed.

In the PDF skill shown below, the SKILL.md refers to two additional files (reference.md and forms.md) that the skill author chooses to bundle alongside the core SKILL.md. By moving the form-filling instructions to a separate file (forms.md), the skill author is able to keep the core of the skill lean, trusting that Claude will read forms.md only when filling out a form.

How to bundle additional content into a SKILL.md file.
You can incorporate more context (via additional files) into your skill that can then be triggered by Claude based on the system prompt.
Progressive disclosure is the core design principle that makes Agent Skills flexible and scalable. Like a well-organized manual that starts with a table of contents, then specific chapters, and finally a detailed appendix, skills let Claude load information only as needed:

This image depicts how progressive disclosure of context in Skills.
Agents with a filesystem and code execution tools don’t need to read the entirety of a skill into their context window when working on a particular task. This means that the amount of context that can be bundled into a skill is effectively unbounded.

Skills and the context window
The following diagram shows how the context window changes when a skill is triggered by a user’s message.

This image depicts how skills are triggered in your context window.
Skills are triggered in the context window via your system prompt.
The sequence of operations shown:

To start, the context window has the core system prompt and the metadata for each of the installed skills, along with the user’s initial message;
Claude triggers the PDF skill by invoking a Bash tool to read the contents of pdf/SKILL.md;
Claude chooses to read the forms.md file bundled with the skill;
Finally, Claude proceeds with the user’s task now that it has loaded relevant instructions from the PDF skill.
Skills and code execution
Skills can also include code for Claude to execute as tools at its discretion.

Large language models excel at many tasks, but certain operations are better suited for traditional code execution. For example, sorting a list via token generation is far more expensive than simply running a sorting algorithm. Beyond efficiency concerns, many applications require the deterministic reliability that only code can provide.

In our example, the PDF skill includes a pre-written Python script that reads a PDF and extracts all form fields. Claude can run this script without loading either the script or the PDF into context. And because code is deterministic, this workflow is consistent and repeatable.

This image depicts how code is executed via Skills.
Skills can also include code for Claude to execute as tools at its discretion based on the nature of the task.
Developing and evaluating skills
Here are some helpful guidelines for getting started with authoring and testing skills:

Start with evaluation: Identify specific gaps in your agents’ capabilities by running them on representative tasks and observing where they struggle or require additional context. Then build skills incrementally to address these shortcomings.
Structure for scale: When the SKILL.md file becomes unwieldy, split its content into separate files and reference them. If certain contexts are mutually exclusive or rarely used together, keeping the paths separate will reduce the token usage. Finally, code can serve as both executable tools and as documentation. It should be clear whether Claude should run scripts directly or read them into context as reference.
Think from Claude’s perspective: Monitor how Claude uses your skill in real scenarios and iterate based on observations: watch for unexpected trajectories or overreliance on certain contexts. Pay special attention to the name and description of your skill. Claude will use these when deciding whether to trigger the skill in response to its current task.
Iterate with Claude: As you work on a task with Claude, ask Claude to capture its successful approaches and common mistakes into reusable context and code within a skill. If it goes off track when using a skill to complete a task, ask it to self-reflect on what went wrong. This process will help you discover what context Claude actually needs, instead of trying to anticipate it upfront.
Security considerations when using Skills
Skills provide Claude with new capabilities through instructions and code. While this makes them powerful, it also means that malicious skills may introduce vulnerabilities in the environment where they’re used or direct Claude to exfiltrate data and take unintended actions.

We recommend installing skills only from trusted sources. When installing a skill from a less-trusted source, thoroughly audit it before use. Start by reading the contents of the files bundled in the skill to understand what it does, paying particular attention to code dependencies and bundled resources like images or scripts. Similarly, pay attention to instructions or code within the skill that instruct Claude to connect to potentially untrusted external network sources.

The future of Skills
Agent Skills are supported today across Claude.ai, Claude Code, the Claude Agent SDK, and the Claude Developer Platform.

In the coming weeks, we’ll continue to add features that support the full lifecycle of creating, editing, discovering, sharing, and using Skills. We’re especially excited about the opportunity for Skills to help organizations and individuals share their context and workflows with Claude. We’ll also explore how Skills can complement Model Context Protocol (MCP) servers by teaching agents more complex workflows that involve external tools and software.

Looking further ahead, we hope to enable agents to create, edit, and evaluate Skills on their own, letting them codify their own patterns of behavior into reusable capabilities.

Skills are a simple concept with a correspondingly simple format. This simplicity makes it easier for organizations, developers, and end users to build customized agents and give them new capabilities.

We’re excited to see what people build with Skills. Get started today by checking out our Skills docs and cookbook.

Acknowledgements
Written by Barry Zhang, Keith Lazuka, and Mahesh Murag, who all really like folders. Special thanks to the many others across Anthropic who championed, supported, and built Skills.