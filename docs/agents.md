# Giving your agents a model and an effort

The model rule of the spawn gate checks that every agent it starts has a `model:` and an `effort:`. This page
explains why, how to add them, and what to do with built-in agent types. The level is set in `config.json`:

| `spawn_rules.model_effort_action` | Effect of a finding |
|---|---|
| `warn` (default) | The agent starts; the model is told what is missing. |
| `deny` | The start is refused. |
| `off` | The model rule is skipped; only the usage part of the gate applies. |

## Why

A subagent without `model:` runs on the model of the main session, and without `effort:` it inherits its
thinking effort. With an expensive main session every small search, count or test run then costs as much as
the hardest task of the day. Naming both per agent keeps cheap work cheap and makes the cost of a block
predictable before it starts.

## Check first

```sh
python3 tools/check_agents.py                     # agents of the current project and ~/.claude/agents
python3 tools/check_agents.py --project ~/my-repo
```

It lists every definition with a finding and its file, the built-in types without a definition file, and the
current exceptions. It changes nothing. Exit code 1 means the gate would act on something.

## Add model and effort

Agent definitions live in `.claude/agents/` of a project or in `~/.claude/agents/`. Add two lines to the
frontmatter:

```markdown
---
name: test-runner
description: Runs the test suite and reports the raw result.
model: haiku
effort: low
---
```

- `model:` an alias (`haiku`, `sonnet`, `opus`, `fable`, `inherit`) or a full name starting with `claude-`.
- `effort:` one of `spawn_rules.effort_values` (default `low`, `medium`, `high`, `xhigh`, `max`).

A sensible start, adjust to your own work:

| Kind of task | model | effort |
|---|---|---|
| Mechanical: search, count, run tests or builds, read logs | `haiku` | `low` |
| Read and sort: explore a codebase, compare, research | `sonnet` | `medium` |
| Build to a clear spec outside critical paths | `sonnet` | `high` |
| Judge: review, architecture, security, final check | `opus` | `high` |

In workflow scripts give each `agent(...)` call literal options, `agent(prompt, {model: 'haiku', effort:
'low'})`, or an `agentType` whose definition has both.

## Built-in agent types

`general-purpose`, `Explore` and `Plan` come with Claude Code and have no definition file, so their effort
cannot be set. Two ways out:

- Define an agent of your own for the same job (for example a read-only explorer with `model: sonnet`,
  `effort: medium`) and use that one instead.
- Or list the type in `spawn_rules.exceptions`; it then starts without the model rule. Setting `exceptions`
  replaces the default list, so keep `claude-code-guide` and `statusline-setup` in it.

## For the model: adapting agents on request

When the user asks you to make their agents fit the model rule:

1. Run `python3 tools/check_agents.py` (with `--project` for the project in question) and read its findings.
2. For each agent with a finding, propose a `model:` and an `effort:` and say why in one line, based on what
   its `description` says it does. Use the table above as the starting point. For built-in types propose an
   own definition or an exception.
3. Ask the user and wait for the answer. Do not edit any definition before that, and change only the agents
   the user agreed to.
4. Add or fix only the `model:` and `effort:` lines in the frontmatter; leave everything else as it is.
5. Run `tools/check_agents.py` again and report what is left.
