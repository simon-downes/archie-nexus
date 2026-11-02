## Coding standards

These are the defaults for code you write or review. The project's own
conventions win: when `CONTRIBUTING.md`, `AGENTS.md`, the linter config or the
surrounding code says otherwise, follow the project. Language standards are in
the `python` and `terraform` skills; load the matching skill before you write
or review code in that language.

### Design

- Build only what was asked. Add a parameter, option or extension point when a
  real requirement needs it, not in case one appears.
- Match the complexity of the solution to the problem. Prefer explicit,
  readable code to compact or clever code.
- Extract shared logic when duplication creates a maintenance burden. Keep a
  little duplication when it reads more clearly than the abstraction would.
- Prefer composition to inheritance. Use inheritance only for a real "is-a"
  relationship, and keep a subclass usable wherever its parent is.
- Keep functions focused; a function that is hard to name does too much.
  Group code by feature or domain concept.
- Don't optimise before profiling, but don't write an O(n²) loop when the
  O(n) version is just as clear.

### Readability

- Name things for what they are for, in the problem's domain language. Avoid
  abbreviations unless they are universal.
- Give every function and class a docstring. Put argument types in it only
  when the language has no type annotations.
- Comment why, not what. Update a comment when the code it describes changes.
- In a typed language, annotate parameters, return values and variables, and
  run the project's type checker and linter.

### Errors and logging

- Write the expected path first and handle failures as exceptions, instead of
  guarding against every possible failure up front.
- Catch specific exceptions, never a catch-all, and put context in the message.
  Tell users what they can do; give developers the technical detail.
- Log significant events, state changes and errors, not routine calls. ERROR
  is a failure that needs attention, WARN is unexpected but handled, INFO is a
  significant event, DEBUG is troubleshooting detail. Follow the project's
  existing logging pattern.

### Security and configuration

- Validate external input (user input, API responses, file contents) before
  you use it, and sanitise it before it reaches a query, command or output.
- Never hardcode or commit secrets. Read them from the environment, a secret
  manager or config, and mask them before they reach a log.
- Default to the safe option: fail closed, and make insecure options an
  explicit opt-in.
- Keep environment-specific values in configuration, not in code.

### Tests

- Test behaviour: business logic, edge cases, boundaries and error paths. Skip
  framework code and trivial accessors.
