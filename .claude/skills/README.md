# Skills

*This file is documentation, not a skill.* The CLI discovers skills by globbing
`<name>/SKILL.md`, so a README at this level is never read by the app and costs
nothing at runtime. Write as much here as is useful.

Skills are instruction files holding the *mechanics* of a task — syntax, protocols,
environment details — that are only useful once the model has decided to do
that task. The decision of *whether* to do it stays in the system prompt
(`backend/prompt_files/`); a rule that has to be loaded before the model can
choose would arrive too late to influence the choice.

## Present skills

| Skill | For | Covers |
|---|---|---|
| [interactive-demo](interactive-demo/SKILL.md) | the demo builder agent | Structure and constraints for a self-contained HTML demo. Overflow in `references/patterns.md`. |
| [derivation-builder](derivation-builder/SKILL.md) | the derivation builder agent | Block kinds, writing in waves, verifying every step with sympy, copying plots in. |
| [code-execution](code-execution/SKILL.md) | the tutor | `run_code` mechanics: the `verified:<id>` placeholder, `%pip install`, background jobs, reading a result. |
| [teaching-mathematics](teaching-mathematics/SKILL.md) | the tutor | The intuition-then-derivation shape, per-domain guidance, notation, checking algebra with sympy. |

The split for maths follows the rule above. **Whether** to reach for a
derivation is a decision, so the trigger words and `request_derivation` live in
[15_mathematics.md](../../backend/prompt_files/15_mathematics.md); **how** to
explain one is mechanics, so it lives in `teaching-mathematics`. That section was
938 words before the split — 30% of the whole tutor prompt, on every turn,
including the ones about dictionaries.

## What actually gets sent

Two parts, and only one of them is deferred:

- **Frontmatter `description`** — advertised on **every** call, for every skill,
  so the model knows what exists. ~90-120 tokens each. Adding a skill adds this
  to every turn whether or not it is ever used.
- **Body** — loaded only when the model pulls the skill in. ~1200-1700 tokens
  each here.

So skills cut per-turn cost but do not zero it. A skill whose body is small
enough to just live in the prompt is not worth the indirection.

Loading is configured by `setting_sources` / `cwd` in
[backend/agent.py](../../backend/agent.py) — see the comments there for why it
is `"project"` and not `"user"`, and what the SDK default would do instead.

Skills need no entry in `MCP_TOOL_NAMES`: they are injected as context, not
invoked as a tool.

## Adding a skill

1. `mkdir .claude/skills/<name>/` and write `SKILL.md`.
2. Frontmatter needs `name` and `description`. **The description is the
   trigger** — it is what the model reads when deciding whether the skill is
   relevant, so name the situation concretely ("load this when you are about to
   call X"), not the topic in the abstract. Keep it tight; it is on every turn.
3. Overflow detail goes in `references/*.md` alongside, so `SKILL.md` stays
   scannable.
4. Add a row to the table above.

## Unverified

Whether the tutor reliably loads `code-execution` mid-turn is **not yet
confirmed**. `interactive-demo` works because the builder agent's whole job is
known when it spawns; the tutor has to pull its skill in at the moment it
decides to run code, which is a different trigger path. If it turns out not to
fire, the fallback is moving the fence syntax back into the `run_code` tool
description, which is always loaded.
