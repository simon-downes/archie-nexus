### Delegating with task

`task` runs child agents in parallel. Use it when work splits into
independent parts and a summary is all you need back: investigations across
several repos or subsystems, a review that needs a fresh context, a wide
survey that would flood your context. A task that a few reads would finish is
cheaper to do yourself.

- Put independent tasks in one `task` call, not one call per task. The tool
  description lists the available agents and how many run at a time per call;
  use `default` unless a listed agent fits.
- A child sees nothing of this conversation. Its prompt must state the goal,
  the paths, what you already know, what it must not do (for example "don't
  edit files"), and the report's shape and length ("under 300 words: findings
  with file:line").
- Issue your own independent tool calls in the same response as `task`
  rather than waiting idle for children.
- Act on the summary. Don't re-read what the child already reported unless
  you need to verify a specific claim.
