# System prompt

This document describes how the agent builds its system prompt: what goes in it, in what order, and why it differs between sessions and subagents. Code is authoritative when this document and the implementation disagree. The builder is `agent/src/archie_agent/prompt.py`; most of the text lives in `persona/prompts/`.

## Contents

- [Sections and order](#sections-and-order)
- [How it is built](#how-it-is-built)
- [The Environment section](#the-environment-section)
- [What varies](#what-varies)
- [Prompt caching](#prompt-caching)
- [Changing the prompt](#changing-the-prompt)

## Sections and order

The main agent's prompt has these sections, in this order. Sections are joined with a blank line, and a section with no content is left out.

| # | Section | Source | Why it is there |
| --- | --- | --- | --- |
| 1 | Identity and core rules | `persona/prompts/identity.md` | Who Archie is and the rules that override everything else |
| 2 | Soul | `persona/prompts/soul.md` + the brain's `SOUL.md` | The user's voice and style; left out when `SOUL.md` is missing or empty |
| 3 | Environment | `persona/prompts/environment.md`, filled by `environment.py` | Facts about the container the agent would otherwise guess at |
| 4 | Available tools | `persona/prompts/tools-strategy.md` + `persona/prompts/delegation.md` + tool guidelines | How to choose between native tools and `exec`, and when to delegate with `task`. Child prompts omit `delegation.md` because children have no `task` tool |
| 5 | Brain | `persona/prompts/brain.md` + the brain's `BRAIN.md` | How to use the curated knowledge store |
| 6 | Skills catalog | discovered skills | One line per skill, so the agent knows what it can load |
| 7 | Project context | the workspace's `AGENTS.md` | Rules for the repository being worked on |

The order runs from general to specific. Identity and the core rules come first, so they frame everything after them; `soul.md` says outright that the core rules win a conflict. Workspace-specific content comes last because it differs per workspace and is the least stable part of the prompt (see [Prompt caching](#prompt-caching)).

User-owned files are wrapped in tags, so the agent can tell repository text from user text:

```
<soul.md>
# Soul
...
</soul.md>
```

`AGENTS.md` is wrapped the same way, in `<agents.md>`. `BRAIN.md` is appended to the Brain section without a wrapper.

## How it is built

`build_system_prompt_structured()` returns a `SystemPrompt` with two parts:

- `static_system` — every section above, joined. Providers cache this part.
- `dynamic_system` — optional extra content after the static part. No caller sets it at present; the split is there so content that changes mid-session doesn't invalidate the cached prefix.

Tool guidelines come from the `guidelines=` argument of the `@tool` decorator on core exec tools, collected in name order and de-duplicated:

```python
@tool(guidelines=("Use `code` to explore symbol structure before reading full files.",))
```

They are rendered as a `### Guidelines` list at the end of the tools section.

The skills catalog lists name and description only:

```
<skills>
Available skills (use the `skill` tool to load):
- research: Research a topic by finding, ...
</skills>
```

A skill's body never goes into the system prompt. The `skill` tool returns it as a tool result, in a `<skill>` block, when the agent loads it.

## The Environment section

`persona/prompts/environment.md` is plain markdown. The tool list and the paths are written out in the file; only values that differ between hosts are placeholders:

```
System: {os_name} ({arch}) · User: {user} (uid {uid}) · Home: {home} · CWD: {workspace_dir}
{?sudo}Passwordless sudo — install missing packages as needed.
```

- `{name}` is replaced by a value: `os_name`, `arch`, `user`, `uid`, `home`, `brain_dir`, `workspace_dir` or `exec_python`.
- A line that starts with `{?name}` is kept, without the guard, only when the value is set. The guards are `container` and `sudo`.
- An unknown name raises `ValueError`, so a typo fails at the first prompt build instead of reaching the model.

`environment.py` probes these facts once per process and caches them, so the section is byte-identical on every turn. `{brain_dir}` is where the brain actually is: `~/.nexus/brain` by default, or wherever `ARCHIE_BRAIN_DIR` points. It gets its own line in the paths list, wherever it is nested.

### Installed tools

Tools are grouped under a category heading. Tools without a note share one line; each tool with a note gets its own line:

```
*Cloud/IaC*:
- `aws`, `gh`, `terraform-docs`
- `tofu` — OpenTofu is installed in place of Terraform; use `tofu` wherever docs say `terraform`.
```

To add a tool, install it in the `Dockerfile` and add it to the right category in `environment.md`. Keep names in alphabetical order within each line.

Add a note only when it stops the agent from getting something wrong or missing it: a substitute binary (`tofu` for `terraform`), an option order the CLI insists on (`scalr`), or a better tool for the common case (`web_fetch` before `agent-browser`). Don't add a note that restates what the tool is; the model already knows `jq` and `rg`. Don't list versions; they rarely change what the agent does. If a version constraint matters, put it in a note.

The list isn't checked against the image. If you remove a tool from the `Dockerfile`, remove it from `environment.md` too.

### Paths

The paths list matches the fixed mounts in `orchestrator/src/archie_orchestrator/lifecycle.py` (see [Container mounts](architecture.md#container-mounts)). It tells the agent what survives the session and what it must not edit. If you change a fixed mount, update the list.

## What varies

**Between sessions.** The environment facts, the skills catalog and `AGENTS.md` are read once, when the session starts, and stay fixed for the session. The catalog depends on which plugins are enabled. `AGENTS.md` comes from the workspace the session was started in.

**Between turns.** The harness rebuilds the prompt on every outer turn, rereading the `persona/prompts/` files, `SOUL.md` and `BRAIN.md`. Edits to them apply from the next turn. The environment facts, skills catalog and `AGENTS.md` stay as they were at session start. The active model never appears in the prompt, so switching models mid-session doesn't change it.

**For subagents.** `build_subagent_prompt()` builds a smaller prompt for agents started with the `task` tool:

| Section | Main agent | Subagent |
| --- | --- | --- |
| Identity and core rules | yes | replaced by the agent body (`persona/agents/<name>.md`, or the built-in `default` agent) |
| Soul | yes | no: subagents report to the parent agent, not the user |
| Environment, Tools, Brain | yes | yes |
| Skills catalog | all discovered skills | only the skills the agent definition lists in `skills:` |
| Project context | yes | no: the parent passes what the task needs in the prompt |
| `dynamic_system` | optional | never |

A subagent body is short and task-specific, for example:

```
You are an independent reviewer spawned by the build-it skill. You have no context from
the conversation that produced the work. Judge only what is on disk.
```

**For compaction.** The compactor doesn't use any of this. It sends its own fixed `SYSTEM` prompt from `compaction_prompts.py`.

## Prompt caching

Providers cache the prompt as a prefix, so any byte that changes in the static part invalidates the cache from that point on. That drives most of the decisions above:

- The environment facts are probed once and cached, and nothing in the section changes per turn.
- The model name is kept out of the prompt.
- Content that varies by workspace (`AGENTS.md`) goes last.
- Bedrock Converse adds a `cachePoint` after the flattened prompt. The Responses-API clients (Bedrock OpenAI and OpenAI) send `static_system` and `dynamic_system` as separate developer-message parts, each with a cache breakpoint, and they build the cache key from the static part with `<agents.md>` removed, so sessions in different workspaces share a key.

## Changing the prompt

- To change wording, edit the file in `persona/prompts/`. `persona/` is mounted into the container, so edits apply from the next turn without rebuilding the image. Each edit also invalidates the prompt cache once.
- To add or reorder a section, change `build_system_prompt_structured()` and, if subagents should get it too, `build_subagent_prompt()`. Update the tables in this document.
- To add a tool, see [Installed tools](#installed-tools).
- Run `uv run pytest tests/test_prompt.py tests/test_environment.py`. The shipped-template tests render the real `environment.md`.
