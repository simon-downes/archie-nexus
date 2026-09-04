## Improved system prompt / identity / personality

- Instruction style: https://chatgpt.com/c/6a8774d8-3ce8-83eb-a151-602bf1e100d9
- Needs an environment section to talk about Docker, can install system packages/tools if needed, key directories (workspace, brain, config, archie run time, session logs, etc)
- Needs improvement brain.md to explain what the brain is and how to use it effectively

# Skills

move skills to into tool result

add skills section to system prompt that tells model to treat skills as instructions and follow them
https://chatgpt.com/c/6a99e634-0768-83eb-8c47-2b00d28d2c74

# Credentials

how to obtain/propagate credentials?
orchestrator should store creds and pass to agents?

needed before we can do agent-kit integration

# Agent Kit Capabilities

docs/saas-tooling.md

need to define plans for each saas tool - rethink from first principals rather than straight copy, any gaps with current setup?
use typed classes/dataclasses for things like jira issues? tool output is usually strings which is potentially less helpful in an exec type environment?



## Kiro Lite

`uv run archie kiro`

Runs a container - same image, mounts, naming convention but runs kiro instead of the agent.
Kiro has a custom agent with no built-in tools specified/allowed - except subagent
Provide archie tools (except task) via a local MCP server.
Should generate custom agent config on each boot - yaml frontmatter from python module, prompt body via existing prompt construction

## Kiro ACP

Support similar to Kiro Lite but via ACP so we get TUI, orchestrator, etc

## General Sessions

Empty workspace directory, otherwise same behaviour

## Web Interface

## Orchestrator Web Interface

- Observability stuff
- Session log viewer

# Better Connection Durability

Originally prompted as fixing having to re-submit a prompt when a websocket timeout happens - seems to be between orchestrator and agent rather than tui and orchestrator.

The system can lose user prompts during WebSocket failures, lacks a durable acknowledgement that a prompt has been accepted,
and risks creating duplicate turns when delivery is retried or received concurrently.

Any solution must account for distributed state across the client, transport, server execution, persistence, and replay,
including failures between receipt, durable storage, acknowledgement, processing, and completion.

Key concerns include restart recovery, ordering and reconciliation of live versus historical events, retry and timeout behavior,
terminal-state guarantees, user-visible recovery, and preserving the exact conversation context.

Re-evaluate the problem from first principles, defining clear invariants and failure semantics before choosing an architecture or protocol.

# End of Turn Status Line

End of every turn should output a one-line summary:
Turn duration (hrs, mins, secs) - Iteration Count - Token usage (input, cache read, cache write, output), Cost

# Global Inference

Use the global inference endpoints for bedrock rather than the region specific ones - need to update the catalog and pricing appropriately

# Task Viewer

Ability to view task agents, both previous and current. Click agent tool block to display list of agents,
click agent to show conversation in same style as main session, press esc to back out (agent view to agent selector to main session)

## TUI Improvements

Done:
- Anchor throbber to bottom of screen directly above the status bar. Same background colour as the messages area. Needs to show/hide as currently.
- Skill tool summary should show how many lines were loaded
- Diff background green/red is jarring - switch to green/red text colour and no background instead
- Error blocks - Red text on red background is hard to read. Red background is overkill so let's remove it
- Esc generates an empty Error block - should probably say "Interrupted" or similar - no Error header bit needed
- Better markdown streaming support - can we actually just append incoming text to a markdown element directly and have it render automatically? streaming text and then updating to markdown is quite jarring on longer responses
- Disable autoscroll - as output streams to the ui the container automatically scrolls to the end making it impossible to read historical entries if a turn is in progress - can we disable the auto-scroll when the scrollbar isn't already at the bottom?
- Make the default model Luna
- Show exact context window size in tokens next to % - need to make sure we calculate correctly (new input + cache read + cache write : is that applicable to all bedrock models we have in the catalog?)
- We should time tool execution time and display in the summaries
- Shell errors repeat the command before showing the output - unncessary given it's above as the input

TODO:
- Better shell formatting - reformat long chained commands so the && and || are followed by line breaks
