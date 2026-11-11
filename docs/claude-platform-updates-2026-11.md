# Claude platform updates since PyMentor was built (as of 2026-11-11)

**Where we are.** `requirements.txt` pins `claude-agent-sdk==0.1.34`. That wheel bundles Claude Code CLI **2.1.38** (2026-02-10), and the bundled CLI is what `query()` actually runs. The installed `claude` 2.1.241 is not used. The latest versions are SDK **0.2.180** (2026-11-09), which bundles CLI about 2.1.318, and CLI **2.1.322**. That is about 145 SDK releases and 280 CLI releases of drift.

Part A covers what fits this app, checked against the code. Part B is the full list of updates to think through. Items marked *unverified* could not be confirmed from the changelogs.

---

## Part A: what fits PyMentor

### A0. Upgrade first, and know what changes underneath you

Almost everything below requires the SDK upgrade. These default changes would affect the app as soon as it upgrades:

| Change | Why it matters here | Action |
|---|---|---|
| **1M context is the default** for Sonnet 5 and later (CLI 2.1.287). Auto-compaction triggers at **~967K** of 1M (2.1.247). | Today a session compacts at about 200K. After the upgrade, a long tutoring session keeps growing to about 1M before it compacts, so every turn re-sends far more context. The cost per turn rises a lot, and "condensing this long session…" arrives much later. `replay.MAX_REPLAY_CHARS` was sized for a 200K world. | Decide on purpose: set `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` in `_cli_env()` to keep 200K, or keep 1M and budget for it. |
| **`verbatim_prompts` defaults to False** (SDK 0.2.158). String prompts get `@path` file-mention expansion and slash-command dispatch. | Learner text and `replay_from_memory` transcripts are passed as string prompts. A learner typing `@/Users/.../.env`, or an old transcript containing one, could make the CLI read a local file into context. | Set `verbatim_prompts=True` on every `ClaudeAgentOptions`. Also check whether the current 2.1.38 already expands `@path` in `-p` prompts. If it does, this is a live issue today. |
| Fork subagents are on by default in SDK sessions (2.1.302). | The tutor could start spawning its own subagents. | Make sure `DENIED_TOOL_NAMES` still covers `Agent`/`Task` under the new tool names. |
| New `ConversationResetMessage` message type (0.2.137). | The stream loop in `agent.py` around line 1614 uses `isinstance` chains, which ignore unknown types, so this is fine. Just be aware of it. | None. |
| `bypassPermissions` + `allowed_tools` now emits a runtime warning (0.2.111). | Already documented in a comment. The deny list still does the real work. | None. |
| `"Skill"` in `allowed_tools` is deprecated (0.1.77). | The app already omits it (agent.py:349). | None. |

### A1. Prompt caching (likely the biggest cost lever, so measure first)

`build_system_prompt` substitutes `$knowledge_state` and `$recent_episodes` into the **system prompt on every turn**. The system prompt is the start of the cached prefix, so whenever mastery or the episode list changes, the whole conversation is re-cached instead of read from cache. Cache reads cost 0.05–0.1x, writes 1.25–2x.

- **Measure first:** log `usage.cache_read_input_tokens` and `cache_creation_input_tokens` from each `AssistantMessage.usage` (per turn since 0.1.49) and `ResultMessage.model_usage`. If the cache-read share is low on long sessions, this item is confirmed.
- **Fix:** make the system prompt static with `SystemPromptCustom{type:"custom", prompt:..., snapshot:True}` (0.2.153). Deliver learner state some other way: a short `<learner_state>` block prepended to each user turn, or let the tutor pull it from a tool (`suggest_next_topics` already exists).
- Related settings: `promptCacheTtl` (CLI 2.1.243) for the 1-hour TTL. Learners often pause for more than 5 minutes between turns, so the 5-minute cache expires mid-session.

### A2. Retire workarounds the platform now covers (verify each first)

- **"Stream closed".** The HTTP transport in `mcp_http.py` already sidesteps it. The SDK also fixed the background-task variants (0.2.127, 0.2.160). After the upgrade, the replay of dropped `update_concept` writes (`agent.py` around line 513) and the "If a tool call comes back Stream closed" section in `80_learner_state.md` are candidates for removal. That section is sent to the model on every turn. Remove them only after the logs show zero occurrences over a reasonable window.
- **Payload caps.** `MAX_TOOL_PAYLOAD_BYTES = 8_000` and `TOOL_RESULT_MAX_CHARS = 2000` were set to protect the stdin channel. Since then:
  - tools moved to HTTP;
  - the SDK fixed a bug that silently corrupted lines over 64 KiB (0.2.111);
  - tools can declare `_meta["anthropic/maxResultSizeChars"]` up to 500K (SDK 0.1.55, CLI 2.1.91).

  Re-measure before raising either cap. Lesson 3d (amortised cost) still applies: a bigger result is re-sent on every turn.

### A3. Sessions: replace hand-built resume and edit plumbing

- **`resume_session_at` + `resume_drops_turn`** (0.2.137) fork a session at an earlier transcript entry. That is exactly "edit message N and continue". Today the app does this with `memory.supersede_from` plus a replay. A native fork keeps the real transcript and the cache prefix.
- **`SessionStore`** (0.1.64; S3/Redis/Postgres reference adapters, `session_store_flush="eager"`) lets the SDK mirror transcripts into your own store and resume from it. That removes the coupling to the CLI's `~/.claude/projects/<slug>/` (the `cwd` comment at agent.py around line 1462, `replay._get_claude_project_dir`). It could replace most of `replay.py`'s transcript fallback.
- `list_sessions` / `get_session_messages` / `rename_session` / `tag_session` / `delete_session` helpers could back the session picker.

### A4. Cost and quality controls per agent

- **`effort`** (0.1.36) and **`thinking={"type":"adaptive"}`**: today every agent runs at defaults. Suggested starting points: tutor `medium`, derivation builder `high` or `xhigh`, demo builder `medium`, summaries `low`.
- **Haiku 5.5** for auxiliary calls such as session summaries, topic extraction and naming. Everything currently uses `MODEL_NAME` (Sonnet 5.5).
- **`max_budget_usd`** (already available) and **`task_budget`** (0.1.51) per background job, so a runaway demo build stops on its own.
- **`fallback_model`** for overload.

### A5. Hardening (cheap)

- `strict_mcp_config=True`: use only `learning-tools`, and ignore any `.mcp.json` or user MCP config.
- `skills=[...]` per agent (0.1.62) instead of every agent loading every project skill through `setting_sources=["project"]`. The tutor doesn't need `derivation-builder` or `interactive-demo`, and the builders don't need `teaching-mathematics`. Run `/skill-doctor` (CLI 2.1.261) to see what each skill costs in context.
- `HookMatcher(on_failure="block")` (0.2.177) on the payload hooks, so a crashing guard fails closed.

### A6. Observability

- `ResultMessage`: `terminal_reason`, `api_error_status`, `errors`, `model_usage` (per model, with cache split). `ResultError` exception (0.2.140).
- `ClaudeSDKClient.get_context_usage()` would drive a real "context used" meter in place of guessing when compaction will happen.
- OTel trace propagation (`claude-agent-sdk[otel]`, 0.1.60). The repo already has MLflow.

### A7. Evaluate prompt changes instead of eyeballing them

- `claude plugin eval` (CLI 2.1.269) runs eval suites against skills and plugins. It suits regression checks on tutor rules the lessons doc keeps rediscovering, for example "never asks what a domain contains", "never asserts output it hasn't run", "doesn't retry after Stream closed".

### A8. Probably not worth it now

- **Replacing the builder job system with SDK subagents** (`agents=`, background tasks, `TaskStarted/Progress/Notification`, `stop_task`). The job modules give you orphan recovery, per-job sessions you can refine, and UI progress, and subagents would lose most of that. Revisit if those modules become a maintenance burden.
- **API-only features** (context editing `clear_tool_uses`, memory tool, message threads, Files API, Managed Agents). The SDK's typed `betas` only accepts the 1M header, so these require calling the Messages API directly.
- **CLI interactive features** (`/loop`, `/schedule`, Monitor, agent view, workflows, Remote Control). These are for developers. They don't apply to a server-driven tutor.

---

## Part B: the full list of updates

Sources:
- [claude-agent-sdk-python CHANGELOG](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md)
- [claude-code CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- [Claude API release notes](https://platform.claude.com/docs/en/release-notes/overview)

CLI bug-fix bullets were reviewed only for the pain points above. The feature list is complete.

### B1. Python SDK (claude-agent-sdk): changes since 0.1.34

Upgrade-risk items come first, then the groups matching your app.

#### 1a. Breaking or behaviour-changing (check before upgrading)

- **0.1.36** (2026-02-13): `max_thinking_tokens` deprecated in favour of `thinking` (`ThinkingConfig`); `effort` added. Your 0.1.34 app likely uses `max_thinking_tokens` or nothing.
- **0.1.51** (2026-03-27): `dontAsk` added to `PermissionMode`. 0.1.57 (date not on PyPI index): `auto` added. Full literal now: `default, acceptEdits, plan, bypassPermissions, dontAsk, auto`.
- **0.1.57** (not on PyPI index): `thinking={"type":"adaptive"}` now maps to `--thinking adaptive`; `disabled` maps to `--thinking disabled`. Previously they became `--max-thinking-tokens`.
- **0.1.60** (2026-04-16): `setting_sources=[]` now passes `--setting-sources=` (disables all filesystem settings). Before, an empty list was silently dropped.
- **0.1.65** (2026-04-22): docstring fix: `dontAsk` denies unapproved tools, and `auto` uses a model classifier.
- **0.1.77** (2026-05-08): `"Skill"` in `allowed_tools` deprecated. Use the `skills` option.
- **0.2.82** (2026-05-15): `mcp` lower bound raised to `>=1.23.0` (GHSA-9h52-p55h-vw2f, DNS-rebinding protection off in older versions). `permission_suggestions` type tightened.
- **0.2.96** (2026-06-10): `mcp` pinned `<2.0.0`. **0.2.140** (2026-08-18) widened it to `mcp>=1.23.0,<3.0.0`, adding mcp 2.x support.
- **0.2.111** (2026-07-06): runtime warning when `can_use_tool` is silently shadowed by `allowed_tools` or `bypassPermissions`. Your app uses `bypassPermissions`, so `can_use_tool` never fires.
- **0.2.124** (2026-07-20), Windows only: `.bat` and `.cmd` CLI launchers (including the npm `claude.cmd` shim) are refused. `resume` and `session_id` containing cmd.exe metacharacters or newlines raise `ValueError`.
- **0.2.129** (2026-08-04): skill-name validation. `skills=["plugin:*"]` and `skills=["*"]` raise `ValueError`. Use `skills="all"` or a `Skill(...)` rule. Names with leading `/` or whitespace also raise.
- **0.2.137** (2026-08-12): new `ConversationResetMessage` widens the `Message` union. Exhaustive `assert_never` matches will break.
- **0.2.158** (2026-09-23): new `verbatim_prompts` option, default `False`. With the default, `@path` file expansion and slash-command dispatch happen in string prompts.
- **0.2.166** (2026-10-14): `SandboxSettings.ignoreViolations` type changed from a `file`/`network` TypedDict to `dict[str, list[str]]` (command-pattern to violation-text map).
- **0.2.177** (2026-11-03): `HookMatcher.on_failure` added. Default `"continue"`. `"block"` raises `CLIConnectionError` against an older CLI.

#### 1b. "Stream closed" and background work (your pain point)

- **0.1.46** (2026-03-05), fixed: a string prompt closed stdin before MCP initialisation finished, so MCP servers failed to register (#630).
- **0.1.53** (not on PyPI index), fixed: deadlock with string prompt plus hooks or MCP servers making many tool calls. `wait_for_result_and_end_input()` now runs as a background task.
- **0.2.101** (2026-06-13): typed `TaskUpdatedMessage` for terminal `system/task_updated` events. Fixes consumers hanging when a background task ends without a `TaskNotificationMessage`.
- **0.2.111** (2026-07-06): (a) shielded subprocess teardown so `SIGTERM`/`SIGKILL` run on cancel, preventing orphaned `claude` children; (b) **NDJSON stream buffer bug**: whitespace was silently dropped when one line exceeded the 64 KiB buffer, which could corrupt tool output or message content. This matters for large tool payloads.
- **0.2.127** (2026-07-24), fixed: `query()` closed stdin on the first `result` frame while background tasks (`run_in_background` subagents) were still running. SDK-MCP tool calls from those tasks then failed with **"Stream closed"** and silently bypassed PreToolUse hooks. Stdin now stays open until in-flight tasks finish and the final result arrives.
- **0.2.160** (2026-09-25), fixed: follow-up turns failed with "Stream closed" after a background subagent finished just before the turn's result. The SDK now listens for CLI `session_state_changed` and keeps stdin open until the CLI reports `idle`. Bounded by `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 10 minutes). Older CLIs fall back to the old behaviour.
- **0.1.70** (2026-04-28): trio nursery corruption on early cancellation fixed.
- **0.2.170** (2026-10-21): `get_session_messages` restores all parallel tool calls and results (only one fork branch was followed).

Read against your pain points: "Stream closed" on MCP calls mid-turn matches the 0.2.127 and 0.2.160 descriptions almost exactly, when background subagents are involved. Those fixes are not in your pinned 0.1.34.

#### 1c. ClaudeAgentOptions: fields added since 0.1.34

Compared by diffing `types.py` at tag v0.1.34 against main (0.2.180). Field name, then the version that added it where the changelog states it:

- `effort` (0.1.36; `"xhigh"` added 0.1.74; `EffortLevel` type exported 0.2.82)
- `thinking` (0.1.36; `ThinkingConfig` adaptive/enabled/disabled; `display` field 0.1.65)
- `skills` (0.1.62; `"all"`, list of names, or `[]`)
- `session_id` (0.1.52)
- `strict_mcp_config` (0.1.74)
- `include_hook_events` (0.1.74)
- `forward_subagent_text` (0.2.140)
- `verbatim_prompts` (0.2.158)
- `resume_session_at` and `resume_drops_turn` (0.2.137)
- `session_store` and `session_store_flush` (0.1.64, `"batched"` or `"eager"` in 0.1.73)
- `task_budget` (0.1.51)
- `load_timeout_ms` (**not in changelog; version unverified**)

Options that already existed at 0.1.34 and that the app uses: `permission_mode`, `resume`, `fork_session`, `allowed_tools`, `disallowed_tools`, `mcp_servers`, `model`, `fallback_model`, `cwd`, `env`, `stderr`, `hooks`, `include_partial_messages`, `setting_sources`, `agents`, `sandbox`, `plugins`, `output_format`, `enable_file_checkpointing`, `max_budget_usd`, `betas`, `user`, `settings`, `add_dirs`, `can_use_tool`, `permission_prompt_tool_name`, `extra_args`, `max_buffer_size`.

Stale type: `SdkBeta` is still `Literal["context-1m-2025-08-07"]` only. You cannot pass other beta headers through the typed `betas` field.

#### 1d. Client, messages and usage

- `ClaudeSDKClient` methods in 0.2.180: `connect`, `query`, `receive_messages`, `receive_response`, `interrupt`, `set_permission_mode`, `set_model`, `rewind_files`, `reconnect_mcp_server`, `toggle_mcp_server`, `stop_task`, `get_mcp_status`, `get_context_usage`, `get_server_info`, `disconnect`. Dated in changelog: `get_context_usage` (0.1.52, 2026-03-29). Added and dated in changelog: `add_mcp_server`, `remove_mcp_server`, typed `McpServerStatus` (0.1.46, 2026-03-05). Others: **version unverified**.
- `query()` with string prompts now supports `can_use_tool` and keeps stdin open for permission requests (0.2.140, 2026-08-18).
- Top-level helpers: `list_sessions`, `get_session_messages` (0.1.46); `tag_session`, `rename_session` (0.1.49); `get_session_info` and `SDKSessionInfo.tag`/`created_at` (0.1.50); `fork_session()` helper, `delete_session()` and offset pagination (0.1.51); `list_subagents`, `get_subagent_messages` and cascading subagent-directory delete (0.1.60, 2026-04-16).
- `ResultMessage`: `stop_reason` (0.1.46); `errors` field (0.1.51); `api_error_status` (0.1.76); `terminal_reason` (0.2.126); `model_usage` typed as `dict[ModelUsage]` with `canonicalModel`/`provider` (0.2.126); `deferred_tool_use` (0.1.74); `origin` (0.2.137).
- `AssistantMessage`: per-turn `usage` (0.1.49).
- `UserMessage.origin`: `MessageOrigin` ("human" vs background/scheduled/peer injection) (0.2.137).
- `RateLimitEvent` typed (0.1.49, 2026-03-17).
- Task messages: `TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage` (0.1.46); `TaskUpdatedMessage` with `TERMINAL_TASK_STATUSES` (0.2.101).
- `HookEventMessage` yielded when `include_hook_events` is set (0.1.74). The CLI emits `SessionStart` and `Setup` regardless (0.2.169 docs).
- `SystemInitData` TypedDict (0.2.166): `capabilities`, `model`, `mcp_servers`, `plugins`, `plugin_errors`, fast-mode state.
- `ResultError` exception, subclass of `ProcessError`, carrying `subtype`, `errors`, `result`, `api_error_status`, `terminal_reason`, `session_id` (0.2.140). Raised when the CLI exits after a terminal error result.
- Actionable error text after an error result instead of "exit code 1" (0.1.77, 2026-05-08).
- Unknown message types are skipped rather than raising (0.1.40).
- Server-tool blocks `ServerToolUseBlock` and `AdvisorToolResultBlock` no longer dropped (0.1.65, 2026-04-22).
- Cost: no SDK-side cost-budget field beyond `max_budget_usd` (already at 0.1.34) and `task_budget` (0.1.51). Spend limits and `modelPricing` are CLI and gateway features (see section 2).

#### 1e. Hooks

- **HookEvent literal, identical at 0.1.34 and 0.2.180 (10 names):** `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`, `SubagentStart`, `PermissionRequest`.
- **Not in the typed literal, but emitted by the CLI (per the CLI changelog):** `PostCompact` (CLI 2.1.76), `StopFailure` (2.1.78), `Elicitation` and `ElicitationResult` (2.1.76), `ConfigChange` (2.1.49), `InstructionsLoaded` (2.1.69), `CwdChanged` and `FileChanged` (2.1.83), `WorktreeCreate` and `WorktreeRemove` (2.1.50), `TaskCreated` (2.1.84), `TeammateIdle`, `PermissionDenied` (2.1.89), `MessageDisplay` (2.1.152), `PreModelSwitch` and `PostModelSwitch` (2.1.251), `DirectoryAdded` (2.1.219). Whether `hooks={...}` accepts these names at runtime is **unverified**. The SDK has `SessionStartHookSpecificOutput` types, but `SessionStart` is not in the literal.
- Hook output types typed per event (0.2.166, 2026-10-14).
- `HookMatcher` fields: `matcher`, `hooks`, `timeout` (default 60s) and `on_failure` (0.2.177).
- Hook callbacks for one event fire concurrently (0.2.82 docs clarification).
- Hook cancel requests from the CLI are honoured (0.1.52, `control_cancel_request`).
- `defer` decision for `PreToolUseHookSpecificOutput.permissionDecision`, with `DeferredToolUse` on `ResultMessage` (0.1.74, 2026-05-04).
- `updatedToolOutput` on PostToolUse replaces any tool's output (0.1.74).
- `agent_id` and `agent_type` on tool-lifecycle hook inputs (0.1.46, 2026-03-05).
- `can_use_tool` fires only on "ask" decisions (0.1.74 docs).

#### 1f. Custom tools and MCP (`create_sdk_mcp_server`, `@tool`)

- `TypedDict` `input_schema` converted to JSON Schema (0.1.51, 2026-03-27). `resource_link` and embedded resource content handled. `is_error` propagated from tool results (0.1.51).
- `Annotated[...]` per-parameter descriptions in `@tool` and `create_sdk_mcp_server` (0.1.52).
- `_meta["anthropic/maxResultSizeChars"]` forwarded from `ToolAnnotations` so results over 50K characters are not silently truncated (0.1.55, 2026-04-03). The CLI side goes up to 500K (CLI 2.1.91).
- Older `mcp` versions mishandled `CallToolResult` returns, so the model got a validation blob (0.1.70 fixed the floor to `>=1.19.0`).
- In-process servers run over mcp's in-memory transport, so `mcp.server.Server` instances get full fidelity: resources, prompts, all content types (0.2.140). Tool cancellation on interrupt is supported on mcp 2.x.
- `ToolAnnotations` accepts camelCase and snake_case hint names (0.2.140).
- `strict_mcp_config` (0.1.74): only `mcp_servers` are used, ignoring project, user and global config.
- `ToolPermissionContext` gains `tool_use_id` and `agent_id` (0.1.52); `decision_reason`, `blocked_path`, `title`, `display_name`, `description` (0.1.74).
- `PermissionUpdate.from_dict()` so suggestions can be echoed back (0.1.76).
- Runtime MCP management: see `add_mcp_server` and `remove_mcp_server` in 1d.

#### 1g. Subagents and agents

- `AgentDefinition` gains `skills`, `memory`, `mcpServers` (0.1.49); `disallowedTools`, `maxTurns`, `initialPrompt` (0.1.51); `effort` including `"xhigh"` (0.1.74).
- `forward_subagent_text` (0.2.140): subagent text and thinking appear in the stream, matching the TS SDK.
- `list_subagents` and `get_subagent_messages` (0.1.60); `parent_tool_use_id` recovered from subagent metadata and `SessionMessage.parent_agent_id` added (0.2.140).
- Background subagents: TaskStarted and friends (see 1d), plus the stdin fixes in 1b.

#### 1h. Skills

- `skills` option (0.1.62, 2026-04-?; not on PyPI index for that version): `"all"`, a list of names, or `[]` to suppress all skills. Replaces `setting_sources` plus `allowed_tools` for skill enablement.
- `"Skill"` in `allowed_tools` deprecated (0.1.77).
- Skill-name validation and `--allowedTools` injection hardening (0.2.129).
- Skill ordering fix: `skills` values forwarded to the CLI (no changelog entry beyond the validation above).

#### 1i. Sessions, resume, fork and session stores

- `resume` and `fork_session` options existed at 0.1.34.
- `session_id` option (0.1.52).
- `SessionStore` protocol (0.1.64, 2026-04-20): `append`, `load`, `list_sessions`, `delete`, `list_subkeys`. Also `InMemorySessionStore`, transcript mirroring via `--session-mirror`, resume from store, and nine store-backed helpers (`list_sessions_from_store`, `get_session_messages_from_store`, `fork_session_via_store`, and others). Conformance harness at `claude_agent_sdk.testing.run_session_store_conformance`. Reference adapters for S3, Redis and Postgres under `examples/session_stores/` (copy-in, not in the wheel).
- `list_session_summaries()` protocol method and `fold_session_summary()` (0.1.65): O(1) per-session list views. `import_session_to_store()` (0.1.65).
- `session_store_flush="eager"` (0.1.73, 2026-05-04): near-real-time appends for live tailing and crash durability.
- Session stores ported to anyio for trio (0.2.88).
- Resume from store seeds `settings.json` and `cowork_settings.json` into the temp config dir, preserving `apiKeyHelper` auth (0.2.139, 2026-08-14).
- `resume_session_at` (fork at an earlier transcript entry) and `resume_drops_turn` (guard that only the expected turn is discarded) (0.2.137). Improved resume error messages (0.2.139).
- `get_session_messages` keeps parallel tool calls (0.2.170).
- `session_id` and `resume` values passed as single `--flag=value` tokens (0.2.121, 2026-07-17): argv-injection hardening.
- `verbatim_prompts` (0.2.158) disables `@path` and slash expansion for untrusted text.
- `SystemPromptPreset.snapshot` and `SystemPromptCustom` (0.2.153, 2026-09-15): keep the first request's system prompt across resumed sessions, which improves cache hits. `snapshot=False` rebuilds every request.
- `exclude_dynamic_sections` on `SystemPromptPreset` (0.1.57, 2026-04-08): moves per-user sections out of the system prompt for cross-user cache hits.

#### 1j. Structured output, budgets, sandbox, permissions, plugins, checkpointing

- Structured output: `output_format` already existed at 0.1.34 (dict). No SDK changelog entry. The API moved structured outputs to `output_config.format` (see section 3). **Whether the SDK maps `output_format` to that is unverified.**
- Budgets: `max_budget_usd` (already at 0.1.34); `task_budget` (0.1.51, 2026-03-27).
- Sandbox: `SandboxNetworkConfig` gains `allowedDomains`, `deniedDomains`, `allowManagedDomainsOnly`, `allowMachLookup` (0.1.71, 2026-04-29). `ignoreViolations` type fixed (0.2.166).
- Permissions: `dontAsk` (0.1.51), `auto` (0.1.57), docstring corrections (0.1.65).
- Plugins: `plugins: list[SdkPluginConfig]` existed at 0.1.34. No SDK changelog entry since.
- File checkpointing: `enable_file_checkpointing` existed at 0.1.34. `rewind_files` on the client is in 0.2.180; **version unverified**.
- Telemetry: W3C `TRACEPARENT`/`TRACESTATE` propagated to the CLI when an OTel span is active. Install extra `claude-agent-sdk[otel]` (0.1.60, 2026-04-16).
- Process hygiene: atexit cleanup of CLI children (0.1.74); orphaned-child fix (0.2.111); `SIGKILL` fallback in `close()` (0.1.51).
- `extra_args` values starting with `-` bound as `--flag=value` (0.2.124).
- `setting_sources` empty list now honoured (0.1.60).
- Stderr callback isolation: a raising callback no longer stops later lines (0.2.82).
- Anonymous `NDJSON` parser: non-JSON stdout lines skipped (0.1.51).

#### 1k. Version to date to bundled CLI (generated from PyPI and the changelog)

Format: SDK version, PyPI upload date, bundled CLI. "not on PyPI index" means the version is in the changelog but was not in the PyPI index I queried. "-" means no bundled-CLI entry in the changelog.

```
0.1.34  not on PyPI index  2.1.38
0.1.35  2026-02-10  2.1.39
0.1.36  2026-02-13  2.1.42
0.1.37  2026-02-16  2.1.44
0.1.38  not on PyPI index  2.1.47
0.1.39  not on PyPI index  2.1.49
0.1.40  not on PyPI index  2.1.51
0.1.41  2026-02-24  2.1.52
0.1.42  not on PyPI index  2.1.55
0.1.43  2026-02-25  2.1.56
0.1.44  2026-02-26  2.1.59
0.1.45  2026-03-03  2.1.63
0.1.46  2026-03-05  2.1.69
0.1.47  2026-03-06  2.1.70
0.1.48  2026-03-07  2.1.71
0.1.49  2026-03-17  2.1.77
0.1.50  2026-03-20  2.1.81
0.1.51  2026-03-27  2.1.85
0.1.52  2026-03-29  2.1.87
0.1.53  not on PyPI index  2.1.88
0.1.55  2026-04-03  2.1.91
0.1.56  2026-04-04  2.1.92
0.1.57  not on PyPI index  2.1.96
0.1.58  2026-04-09  2.1.97
0.1.59  2026-04-13  2.1.105
0.1.60  2026-04-16  2.1.111
0.1.61  2026-04-16  2.1.112
0.1.62  not on PyPI index  2.1.113
0.1.63  2026-04-18  2.1.114
0.1.64  2026-04-20  2.1.116
0.1.65  2026-04-22  2.1.118
0.1.66  2026-04-23  2.1.119
0.1.67  not on PyPI index  2.1.120
0.1.68  2026-04-25  2.1.119
0.1.69  2026-04-28  2.1.121
0.1.70  2026-04-28  2.1.122
0.1.71  2026-04-29  2.1.123
0.1.72  2026-05-01  2.1.126
0.1.73  2026-05-04  2.1.128
0.1.74  not on PyPI index  2.1.129
0.1.75  not on PyPI index  2.1.131
0.1.76  2026-05-06  2.1.132
0.1.77  2026-05-08  2.1.133
0.1.78  2026-05-08  2.1.136
0.1.79  not on PyPI index  2.1.137
0.1.80  2026-05-09  2.1.138
0.1.81  2026-05-11  2.1.139
0.2.82  2026-05-15  2.1.142
0.2.83  2026-05-21  2.1.146
0.2.84  2026-05-21  2.1.147
0.2.85  2026-05-22  2.1.148
0.2.86  2026-05-22  2.1.149
0.2.87  2026-05-23  2.1.150
0.2.88  2026-06-02  2.1.161
0.2.89  2026-06-03  2.1.162
0.2.90  2026-06-04  2.1.163
0.2.91  2026-06-05  2.1.165
0.2.92  not on PyPI index  2.1.166
0.2.93  2026-06-06  2.1.167
0.2.94  2026-06-08  2.1.169
0.2.95  2026-06-09  2.1.170
0.2.96  2026-06-10  2.1.172
0.2.97  2026-06-11  2.1.173
0.2.98  not on PyPI index  2.1.174
0.2.99  2026-06-12  2.1.175
0.2.100 2026-06-12  2.1.176
0.2.101 2026-06-13  2.1.177
0.2.102 2026-06-15  2.1.178
0.2.103 2026-06-16  2.1.179
0.2.104 2026-06-17  2.1.181
0.2.105 2026-06-19  2.1.183
0.2.106 2026-06-20  2.1.185
0.2.107 2026-06-22  2.1.186
0.2.108 2026-06-23  2.1.187
0.2.109 2026-06-24  2.1.190
0.2.110 2026-06-24  2.1.191
0.2.111 2026-07-06  2.1.202
0.2.112 2026-07-07  2.1.203
0.2.113 2026-07-08  2.1.204
0.2.114 2026-07-08  2.1.205
0.2.115 2026-07-09  2.1.206
0.2.116 2026-07-11  2.1.207
0.2.117 2026-07-14  2.1.208
0.2.118 2026-07-14  2.1.209
0.2.119 2026-07-14  2.1.210
0.2.120 2026-07-15  2.1.211
0.2.121 2026-07-17  2.1.212
0.2.122 2026-07-18  2.1.214
0.2.123 2026-07-19  2.1.215
0.2.124 2026-07-20  2.1.216
0.2.125 2026-07-21  2.1.217
0.2.126 2026-07-22  2.1.218
0.2.127 2026-07-24  2.1.219
0.2.128 2026-07-25  2.1.220
0.2.129 2026-08-04  2.1.221
0.2.130 2026-08-04  2.1.222
0.2.131 2026-08-06  2.1.223
0.2.132 2026-08-07  2.1.224
0.2.133 2026-08-08  2.1.225
0.2.134 2026-08-08  2.1.226
0.2.135 2026-08-10  2.1.227
0.2.136 2026-08-11  2.1.228
0.2.137 2026-08-12  2.1.229
0.2.138 2026-08-13  2.1.232
0.2.139 2026-08-14  2.1.233
0.2.140 2026-08-18  2.1.235
0.2.141 2026-08-19  2.1.236
0.2.142 2026-08-20  2.1.237
0.2.143 2026-08-20  2.1.238
0.2.144 2026-08-21  2.1.246
0.2.145 2026-08-27  2.1.247
0.2.146 2026-08-27  2.1.248
0.2.147 2026-08-28  2.1.250
0.2.148 2026-08-28  2.1.251
0.2.149 2026-08-31  2.1.252
0.2.150 2026-09-01  2.1.257
0.2.151 2026-09-01  2.1.258
0.2.152 2026-09-02  2.1.259
0.2.153 2026-09-15  2.1.273
0.2.154 2026-09-17  2.1.274
0.2.155 2026-09-17  2.1.275
0.2.156 2026-09-18  2.1.276
0.2.157 2026-09-18  -           (no changelog entry)
0.2.158 2026-09-23  2.1.280
0.2.159 2026-09-23  2.1.281
0.2.160 2026-09-25  2.1.283
0.2.161 2026-09-28  2.1.284
0.2.162 2026-09-29  2.1.285
0.2.163 2026-09-30  2.1.286
0.2.164 2026-10-06  2.1.292
0.2.165 2026-10-08  2.1.294
0.2.166 2026-10-14  2.1.299
0.2.167 2026-10-14  2.1.300
0.2.168 2026-10-15  2.1.301
0.2.169 2026-10-20  2.1.305
0.2.170 2026-10-21  2.1.306
0.2.171 2026-10-21  2.1.307
0.2.172 2026-10-22  2.1.308
0.2.173 2026-10-22  2.1.309
0.2.174 2026-10-24  2.1.310
0.2.175 2026-10-30  2.1.314
0.2.176 2026-10-31  2.1.316
0.2.177 2026-11-03  2.1.317
0.2.178 2026-11-04  2.1.318
0.2.179 2026-11-06  -           (notes: install line only)
0.2.180 2026-11-09  -           (notes: install line only)
```

### B2. Claude Code CLI: 2.1.38 to 2.1.322

The CLI dates below are npm publish dates. Items are grouped by the themes you listed. Anything marked "SDK" is a fix or behaviour that reaches `query()` or `-p` sessions specifically.

#### 2a. Release anchors

- 2.1.38 (2026-02-10): the version your app bundles. Fixes include blocking writes to `.claude/skills` in sandbox mode.
- 2.1.41 (2026-02-13): `claude auth login`, `auth status`, `auth logout`.
- 2.1.45 (2026-02-17): Sonnet 4.6 support; `SDKRateLimitInfo` and `SDKRateLimitEvent` types.
- 2.1.63 (2026-02-28): HTTP hooks; `/simplify` and `/batch`.
- 2.1.69 (2026-03-04): `InstructionsLoaded` hook; `agent_id` and `agent_type` on hook events; `/reload-plugins`; `${CLAUDE_SKILL_DIR}`; `/claude-api` skill.
- 2.1.71 (2026-03-06): `/loop` and cron scheduling tools; `voice:pushToTalk`.
- 2.1.76 (2026-03-14): MCP elicitation; `Elicitation` and `ElicitationResult` hooks; `PostCompact` hook; `/effort`; `-n`/`--name`; `worktree.sparsePaths`; session quality survey.
- 2.1.81 (2026-03-20): `--bare` (scripted `-p`, skips hooks, LSP, plugin sync, and skill walks); `--channels` permission relay.
- 2.1.89 (2026-03-31): `"defer"` permission decision on PreToolUse; `PermissionDenied` hook; `CLAUDE_CODE_NO_FLICKER`; `MCP_CONNECTION_NONBLOCKING`.
- 2.1.91 (2026-04-02): `_meta["anthropic/maxResultSizeChars"]` up to 500K; `disableSkillShellExecution`; plugin `bin`/monitors.
- 2.1.98 (2026-04-09): Monitor tool; Vertex setup wizard; `--exclude-dynamic-system-prompt-sections`; subprocess sandbox with PID namespace (when `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`).
- 2.1.105 (2026-04-13): `PreCompact` hook can block compaction (exit 2 or `{"decision":"block"}`); background monitors from plugin manifest.
- 2.1.111 (2026-04-16): `xhigh` effort (Opus 4.7); `/ultrareview`; `/less-permission-prompts`; `OTEL_LOG_RAW_API_BODIES`.
- 2.1.139 (2026-05-11): agent view (`claude agents`, research preview); `/goal`; hook `args` exec form; `continueOnBlock` for PostToolUse.
- 2.1.147 (2026-05-21): `/simplify` renamed to `/code-review`.
- 2.1.152 (2026-05-26): `MessageDisplay` hook; `/reload-skills`; `pluginSuggestionMarketplaces`.
- 2.1.160 (2026-06-01): `workflow` keyword renamed to `ultracode`.
- 2.1.166 (2026-06-05): `fallbackModel` setting (up to three fallbacks).
- 2.1.181 (2026-06-17): `/config key=value`; `sandbox.allowAppleEvents`.
- 2.1.186 (2026-06-22): `claude mcp login` and `claude mcp logout`.
- 2.1.198 (2026-07-01): agent notifications via the `Notification` hook (`agent_needs_input`, `agent_completed`); `/dataviz`; `/agents` wizard removed.
- 2.1.212 (2026-07-16): per-session cap of 200 subagent spawns; WebSearch session cap (200); `set_model` control request applies mid-turn in SDK sessions.
- 2.1.217 (approx 2026-07-18 to 07-24): cap of 20 concurrent subagents (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`); subagents no longer spawn nested subagents by default (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`).
- 2.1.219 (2026-07-24): Opus 5 default; `DirectoryAdded` hook; `sandbox.network.strictAllowlist`; `mcp_server_errors` in stream-json init; `--forward-subagent-text` nested forwarding.
- 2.1.224 (2026-08-07): self-hosted runners (`claude self-hosted-runner`); `archive` plugin source; the 200-subagent spawn cap removed (concurrency and depth limits remain).
- 2.1.241 (2026-08-22): your locally installed version.
- 2.1.251 (2026-08-28): `PreModelSwitch` and `PostModelSwitch` hooks; `SessionStart` resume hooks get staleness data; per-session prompt-cache line in `/cost`; `CLAUDE_CODE_SUBAGENT_MODEL` now sets a default rather than overriding everything; `/effort` saved per model.
- 2.1.257 (2026-09-01): Fable 5.1; `permissions.blockReadsOutsideWorkingDirectories`; `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`; `--resume <id> --bg` continues under its own id.
- 2.1.261 (approx 2026-09-01 to 09-11): `/skill-doctor` (usage and context cost of loaded skills); `--append-subagent-system-prompt-file`; `bashOutputMaxChars` and `taskOutputMaxChars` (up to 128K inline before saving to file).
- 2.1.269 (2026-09-11): `claude plugin eval` (generally available; see section 5); `/output-style` works in headless sessions; `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (1 to 256).
- 2.1.280 (2026-09-22): Opus 5.5 default; `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`; default Opus on Pro and Team Standard.
- 2.1.284 (2026-09-28): Sonnet 5.5 (`claude-sonnet-5-5`), default Sonnet on the Anthropic API, 1M context; `deniedModels`; `availableModelsMatch`; auto mode default for interactive sessions.
- 2.1.287 (2026-10-01): Claude Mods (plugins modify deeper behaviour); 1M context by default for Opus 4.7 and later, Fable and Sonnet 5 and later; MCP `alwaysLoad: false` defers all of a server's tools.
- 2.1.295 (2026-10-08): `onFailure: "block"` for command and HTTP hooks; OSC 7501 program status; MCP protocol 2026-07-28 negotiated for claude.ai connectors.
- 2.1.299 (2026-10-14): idle compaction in terminal (200K+ tokens idle about 54 minutes, before the one-hour cache expires); `CLAUDE_CODE_DISABLE_FILE_MENTIONS`; `$.session.identity()`; `--shutdown-drain-sec`; MCP allow rules now match only their named server (`mcp__a` no longer matches `a__b`).
- 2.1.302 (2026-10-16): fork subagents on by default in `-p`, SDK, desktop and VS Code; `CLAUDE_CODE_AUTO_MODE_MODEL`; SDK sessions with no permission mode start in `default` on third-party providers.
- 2.1.304 (2026-10-19): `/auto-mode-setup`; tool search on by default behind a custom `ANTHROPIC_BASE_URL` (400 or 422 retried without it); compaction resends the skill list; skill hooks capped at 1,024 per source.
- 2.1.311 (2026-10-26): `ultrathink` runs at xhigh on Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1; stream-json DEL and C1 characters escaped as `\uXXXX`; `DISABLE_COMPACT` can only turn compaction off.
- 2.1.314 (2026-10-30): `permissions.blockWritesOutsideWorkingDirectories`; idle compaction for more providers; PreCompact output capped at 10,000 characters.
- 2.1.316 (2026-10-31 to 11-02, approx): `CLAUDE_CODE_SUBAGENT_EFFORT`; `CLAUDE_CODE_DISABLE_ULTRATHINK`; Opus 4.8 fast mode deprecated (removal 2026-11-30).
- 2.1.317 (2026-11-02): `onFailure: "block"` for Agent SDK hook callbacks; `/schedule` warns on unrequested connectors; `anthropic.*` OTel attributes; subagent `effort` follows the subagent's model.
- 2.1.318 (2026-11-03): background subagents move shell commands to background after 3 minutes (cache-expiry protection); `deniedMcpServers` URL entries also block Anthropic-provided servers.
- 2.1.319 (2026-11-06): `permissions.denyInsteadOfPrompting` (a tool call that would prompt is denied and the turn continues, including in subagents and teammates); MCP tasks moved to background (`CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND`, `CLAUDE_CODE_DISABLE_MCP_TASKS`); `CLAUDE_CODE_DISABLE_FEATURE_FLAGS`; `/tasks` shows finished subagents.
- 2.1.321 (2026-11-09): Claude Fable 5.2 (`claude-fable-5-2`) as default Fable; `--settings` for `claude remote-control`.
- 2.1.322 (2026-11-10): fast mode on Vertex.

#### 2b. Background tasks, agents and concurrency (your concurrency pain point)

- 2.1.144: background sessions appear in `/resume`.
- 2.1.186: background subagents surface permission prompts in the main session.
- 2.1.212: agent view shows sessions waiting on sandbox or MCP input as "Needs input".
- 2.1.217: concurrent subagent cap (default 20).
- 2.1.218: skills with `context: fork` run in the background by default; opt out with `background: false`.
- 2.1.224: 200-subagent cap removed.
- 2.1.260: one-hour limit on background commands started by subagents removed.
- 2.1.285: background Bash and PowerShell have a time limit (default 30 minutes, max 2 hours); Claude is notified when stopped.
- 2.1.288: the background command time limit applies only in unattended sessions (`-p`, Agent SDK, CI, cloud).
- 2.1.290: background sessions waiting on a `/loop` wakeup are left running through updates and low memory.
- 2.1.296: `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`; `autoCompactWindow` in subagent frontmatter.
- 2.1.302: fork subagents on by default in SDK sessions; desktop and VS Code always run the main conversation's subagents in the background.
- 2.1.317: SendMessage to a running background agent moves its command to background, so the message lands sooner.
- 2.1.319: subagent-visible "Earlier in this session" list in `/tasks`.
- Cross-session messaging: `SendMessage` and `ListAgents` (2.1.224); `notify_when_idle` (2.1.236).

#### 2c. Workflows

- 2.1.202: `workflow.run_id` and `workflow.name` OTel attributes; "Dynamic workflow size" setting.
- 2.1.269: `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (1 to 256).
- 2.1.296: `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`.
- 2.1.300: background sessions resume a carried workflow through the Workflow tool's approval.
- 2.1.305: workflow resume serves completed `pipeline()` and `parallel()` agents from the journal.
- 2.1.318: workflows no longer re-run finished agents after a usage-limit resume.

#### 2d. Memory

- 2.1.74: `autoMemoryDirectory` setting.
- 2.1.75: last-modified timestamps on memory files.
- 2.1.181: "Improved N memories" line no longer lists files outside verbose mode.
- 2.1.214: ISO `modified` timestamp in memory frontmatter.
- 2.1.285: auto-memory cannot be turned on from a background session or a session Claude Code's own tools started.
- 2.1.311: fixed stale MEMORY.md and CLAUDE.md after automatic compaction.
- 2.1.283 and 2.1.304: `/doctor prompt-audit` and skill-list handling noted under skills.

#### 2e. Compaction and context

- 2.1.74: `/context` gives actionable suggestions.
- 2.1.76: `PostCompact` hook.
- 2.1.105: `PreCompact` can block compaction.
- 2.1.248: `experimental.cacheTtl` (`"5m"` or `"1h"`) in agent frontmatter.
- 2.1.243: `promptCacheTtl` and `subagentPromptCacheTtl` settings.
- 2.1.260: cause hints for prompt-cache misses in `/cost` and the status line.
- 2.1.265: 1 GB cap on tool results saved to disk.
- 2.1.271: `omitClaudeMd` in agent frontmatter.
- 2.1.273: request headers `x-claude-code-compaction` and `x-claude-code-context-compacted`.
- 2.1.296: `autoCompactWindow` per subagent (can lower the window, never raise it).
- 2.1.299: idle compaction (default-on, for 200K+ token idle conversations; `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`; `"idleCompaction": false` turns it off). Extended to more providers in 2.1.314 and 2.1.302.
- 2.1.302: `/config` row for idle compaction.
- 2.1.304: compaction summary lists earlier tool names.
- 2.1.307: `/autocompact` per model (2.1.288); thrashing fix for turns with many large tool results.
- 2.1.311: 1M-context sessions over 200K compact rather than failing with "Usage credits required".
- 2.1.317: "Prompt is too long" after compaction with a very large CLAUDE.md fixed.
- 2.1.223: `CLAUDE_CODE_DISABLE_1M_CONTEXT` holds any native-1M model to 200K via auto-compaction.
- 2.1.285: custom `ANTHROPIC_BASE_URL` sessions use the 1M window of models that have one; run `/autocompact 200k` if your gateway caps at 200K.
- 2.1.287: 1M context by default for Opus 4.7 and later, Fable and Sonnet 5 and later, without the `[1m]` suffix (`CLAUDE_CODE_DISABLE_1M_CONTEXT=1` keeps 200K).
- 2.1.247: Sonnet 5 auto-compact at about 967K of 1M (was 934K).

#### 2f. MCP transports, tool results and tool search

- Transports: HTTP and streamable HTTP (direct HTTP servers use MCP v2 client and protocol 2026-07-28 for Bedrock, Vertex, Foundry and telemetry-off installs since 2.1.274). Protocol 2026-07-28 negotiated by default for stdio servers (2.1.292), claude.ai connectors (2.1.295), cloud and Remote Control connectors (2.1.299). `MCP_PROTOCOL_NEGOTIATION=legacy` opts out.
- Elicitation: form and URL modes (2.1.76; URL mode on 2026-07-28 connections, 2.1.281; 2025-11-25 URL prompts, 2.1.287).
- MCP tasks: moved to background (2.1.319).
- Large results: `_meta["anthropic/maxResultSizeChars"]` up to 500K (2.1.91); improved large-result handling with smaller session files (2.1.287); images from MCP tools saved to a file too (2.1.283); truncation prompt gives format-specific recipes (2.1.105); `bashOutputMaxChars` and `taskOutputMaxChars` (2.1.261).
- Tool descriptions: default cap raised from 2,048 to 4,096 characters (2.1.296); tool-search-loaded descriptions cut at 16,384 (2.1.295); cap changeable with `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` (2.1.280); first-party server descriptions no longer cut at 4,096 (2.1.299).
- Tool search: on by default behind a custom base URL (2.1.304); `alwaysLoad` (2.1.121), and `alwaysLoad: false` defers a server's tools (2.1.287); `isDeferred` for mods (2.1.293); late-connecting tools load deferred to keep the tool list byte-stable (2.1.268).
- Permissions for MCP: `mcp__<server>__*` must name the whole normalised server name (2.1.299). Agent `tools:` lists take only the named server's tools.
- SDK-relevant MCP fixes: `--resume` of a deferred tool call with a missing server now exits 1 (2.1.319); HTTP MCP server large responses bounded in memory (2.1.290); MCP tool calls no longer run twice when a remote result exceeds 16 MB (2.1.288); MCP servers declared `"type":"sdk"` in `.mcp.json` skipped with a warning (2.1.274).

#### 2g. Skills

- `/skill-doctor` (2.1.261): usage and context cost of loaded skills.
- `/reload-skills` (2.1.152); reloads only new or changed skills after compaction (2.1.304).
- `context: fork` skills run in the background by default (2.1.218); `allowed-tools` behaviour under policy (2.1.304).
- Subagents preload at most 32 skills (2.1.295).
- Skill names with hidden, direction-changing or angle-bracket characters ignored (2.1.299); names with `:` rejected in agent markdown (2.1.218).
- `Skill(anthropic-skills:*)` and `Skill(claude-ai:*)` cover only synced claude.ai skills (2.1.282); `anthropic-skills` and `claude-ai` namespaces no longer load folder skills (2.1.282).
- claude.ai skills synced to terminal sessions (2.1.275); sync interval about 40 minutes when idle (2.1.293); `CLAUDE_CODE_SYNC_SKILLS` (2.1.311).
- `disableBundledSkills` (2.1.169); `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS`.
- Skill `effort` frontmatter (2.1.80); `${CLAUDE_SKILL_DIR}` (2.1.69).
- Skill and plugin booleans accept `yes`, `no`, `on`, `off` (2.1.218).

#### 2h. Plugins

- `claude plugin eval` and `claude plugin eval init` (2.1.269; see section 5).
- `claude plugin init` scaffolds a plugin (2.1.157); `claude plugin details` (2.1.139); `claude plugin prune` (2.1.121); `claude plugin tag` (2.1.118); `claude plugin validate --json` (2.1.259) and MCP checks in validate (2.1.281).
- `claude plugin install --marketplace <source>` (2.1.292, 2.1.275); `--accept-command <sha256>` (2.1.271); `claude plugin configure` (2.1.285).
- Dependency enforcement on disable (2.1.143).
- Plugin marketplace `command` source (2.1.229); archive source (2.1.224); npm sources fetched with `npm pack --ignore-scripts` (2.1.275).
- Plugin monitors via manifest `monitors` key (2.1.105); `${CLAUDE_PLUGIN_DATA}` (2.1.78).
- `--plugin-dir` accepts a folder of plugins (2.1.265).
- Agent-plugin subagent frontmatter (`effort`, `maxTurns`, `disallowedTools`) (2.1.78).

#### 2i. Output styles

- `Concise` built-in output style (2.1.237).
- `/output-style [name]` works in headless and SDK sessions (2.1.269).
- `keep-coding-instructions` on plugin output styles (2.1.94).
- Output style selection is fixed at session start for caching (2.1.73; `/output-style` deprecated then in favour of `/config`).

#### 2j. Monitors, scheduling and loops

- Monitor tool (2.1.98); Monitor watches always have a deadline of 30 minutes, 10 in single-prompt `-p` (2.1.271).
- `/loop` (2.1.71); self-paced `/loop` ends when a turn does not reschedule (2.1.319); `CLAUDE_CODE_DISABLE_CRON` (2.1.72).
- `/schedule` cloud routines; warns on unrequested connectors (2.1.317).
- `claude remote-control --settings` (2.1.321).

#### 2k. Hooks (CLI side)

- `PreToolUse` `defer` (2.1.89); `PermissionDenied` (2.1.89); `CwdChanged` and `FileChanged` (2.1.83); `ConfigChange` (2.1.49); `InstructionsLoaded` (2.1.69); `Elicitation` and `ElicitationResult` (2.1.76); `WorktreeCreate` and `WorktreeRemove` (2.1.50); `TaskCreated` (2.1.84); `MessageDisplay` (2.1.152); `PreModelSwitch` and `PostModelSwitch` (2.1.251); `DirectoryAdded` (2.1.219); `StopFailure` (2.1.78); `PostCompact` (2.1.76).
- `if` field with permission-rule syntax (2.1.85); `args` exec form (2.1.139); `continueOnBlock` on PostToolUse (2.1.139); `terminalSequence` in hook JSON (2.1.141).
- `onFailure: "block"` for command and HTTP hooks (2.1.295) and for SDK hook callbacks (2.1.317).
- `updatedInput` from PreToolUse and PermissionRequest validated like Claude's own input (2.1.300).
- Hook output over 10,000 characters saved to disk with a 2,000-character preview (2.1.89).
- PostToolUse hooks run on tool calls interrupted by a new message, up to 10 seconds (2.1.319).
- Prompt and agent hooks: at most 24 at a time per session (2.1.312); at most 12 of one event at once (2.1.302).

#### 2l. Permissions and auto mode

- `Tool(param:value)` rules (2.1.178).
- `permissions.denyInsteadOfPrompting` (2.1.319).
- `permissions.blockWritesOutsideWorkingDirectories` (2.1.314); `permissions.blockReadsOutsideWorkingDirectories` (2.1.257).
- `--permission-prompts none` for unattended hosts (2.1.259).
- Auto mode: default for interactive sessions (2.1.284, 2.1.283); server-side classifier default on the direct API (2.1.282, 2.1.278); `CLAUDE_CODE_AUTO_MODE_SERVER`; `autoMode.classifyAllShell` (2.1.193); `autoMode.hard_deny` (2.1.136).
- `bypassPermissions` in project `.claude/settings.json` ignored (2.1.257). Your app sets the mode in code, so this does not affect it.
- Dangerous `rm` in bypass mode denied after 2 minutes with a rewrite hint (2.1.281); denied at once in Desktop and VS Code bypass mode (2.1.309).
- SDK and `-p` sessions with no permission mode start in `auto` on third-party providers or with telemetry off (2.1.285); on third-party providers they start in `default` (2.1.302). Your app sets `bypassPermissions`, so neither applies.

#### 2m. Sandbox

- `sandbox.failIfUnavailable` (2.1.83); `sandbox.excludedCommands` rules (2.1.282, 2.1.285).
- `sandbox.network.deniedDomains` (2.1.113); `strictAllowlist` (2.1.219).
- `sandbox.credentials` (2.1.187); masking options `extract`, `decode: "jwt"`, `awsPairs`, `sigv4` (2.1.224); mode `mask` (2.1.221).
- `sandbox.filesystem.disabled` (2.1.216); `sandbox.allowNestedUserNamespaces` (2.1.305); `sandbox.bwrapPath` and `socatPath` (2.1.133).
- Per-command `allowed_domains` in auto mode with sandboxing (2.1.271).

#### 2n. Sessions, resume and fork

- `claude attach`, `logs`, `stop`, `respawn`, `rm` (2.1.251); `claude attach <name>` and `logs <name>` (2.1.290).
- `/resume` lists background sessions (2.1.144).
- `/branch` (renamed from `/fork` at 2.1.77). `/fork` creates a new worktree (2.1.221).
- Transcripts record each request's system prompt, tools and parameters (2.1.300).
- Resume warning when an inactive conversation would use a large share of the 5-hour limit (2.1.304).
- `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` and its `_MAX_AGE_MS` (2.1.317).
- `--resume` and `--continue` restore agent-team task lists (2.1.311).
- `CLAUDE_CODE_NESTED_SESSION_ID` behaviour and `CLAUDE_CODE_DISABLE_NESTED_SESSION_ID` (2.1.317).

#### 2o. Rewind and checkpoints

- `/rewind` across `/clear` (2.1.191).
- Bash `sed -i` no longer restored by `/rewind` (2.1.309).

#### 2p. Telemetry and cost (SDK-visible)

- `claude_code.llm_request` span carries `effort` (2.1.274); `api_request` splits cache writes by TTL (2.1.300); `OTEL_LOG_TOOL_DETAILS` adds agent, skill, plugin and MCP names to cost metrics (2.1.273); W3C trace context to Bash subprocesses (2.1.98).
- `prompt_text` on `user_prompt` (2.1.287).
- `claude_code.safety_event` (2.1.307); stopped sending it (2.1.312).
- `/usage` shows loops breakdown (2.1.243), max-effort and workflow warnings (2.1.299), spend limits (2.1.251).
- `modelPricing` managed setting, multiplier up to 10 (2.1.243, 2.1.271).
- `CLAUDE_CODE_ENABLE_FEEDBACK_SURVEY_FOR_OTEL` (2.1.136).

#### 2q. Models and effort (CLI)

- Opus 4.7 `xhigh` effort (2.1.111); Opus 4.8 and Fable 5 do not hold launch-default effort over `/effort` in `-p` or SDK (2.1.280).
- `CLAUDE_CODE_SUBAGENT_EFFORT` (2.1.316); `effort` on the Agent tool (2.1.292).
- `ultrathink` runs at xhigh on Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1 (2.1.311); `CLAUDE_CODE_DISABLE_ULTRATHINK` (2.1.316).
- `CLAUDE_CODE_DISABLE_1M_CONTEXT` (2.1.50, extended 2.1.223).
- `ANTHROPIC_DEFAULT_MODEL` (2.1.236); `modelOverrides` (2.1.73); `modelPicker` (2.1.243).
- Sonnet 5.5 default on API (2.1.284); Opus 5.5 default (2.1.280); Opus 5 default (2.1.219); Fable 5.2 default (2.1.321); Haiku 5.5 default Haiku (2.1.293); Opus 4.8 fast mode removal 2026-11-30 (2.1.316).
- `ANTHROPIC_CUSTOM_MODEL_OPTION` (2.1.78).
- `--add-dir` network paths refused (2.1.257).

#### 2r. Removals and breaking changes (CLI)

- 2.1.75 (breaking): Windows managed-settings fallback path `C:\ProgramData\ClaudeCode\managed-settings.json` removed.
- 2.1.160: `CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE` a no-op.
- 2.1.198: `/agents` wizard removed.
- 2.1.222: ultraplan removed.
- 2.1.277: deprecated `TaskOutput` tool removed; use `Read` on the output file. `taskOutputMaxChars` and `TASK_MAX_OUTPUT_LENGTH` have no effect.
- 2.1.218: `Write`, `NotebookEdit` and `Glob` path rules warned on startup (use `Edit(path)` or `Read(path)`).
- 2.1.98: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`, opt-in.
- 2.1.92: `/tag` and `/vim` removed.
- 2.1.260: the one-hour background-command limit for subagents removed.
- 2.1.259: `allowedMcpServers` governs only user-added servers.
- 2.1.278: `claude -p` runs no longer make a background Haiku auto-title request.
- Local 2.1.241 vs latest 2.1.322: about 80 releases of drift. Nothing in the changelog marks a breaking change inside 2.1.241 to 2.1.322 that would require an app change beyond the items above.

#### 2s. Your listed topics: direct answers

- **Tool search and deferred tools:** 2.1.287, 2.1.293, 2.1.295, 2.1.304 (see 2f).
- **Plugins:** section 2h.
- **Output styles:** section 2i.
- **Monitors:** section 2j.
- **Scheduling:** section 2j.
- **MCP transports:** section 2f.

#### 2t. Fixes aimed at your pain points (CLI side)

- **Stream closed (CLI changelog):** the CLI changelog has **no** "Stream closed" entry. The fix is in the SDK (section 1b). Related CLI fixes: 2.1.290 (SDK and headless stalls), 2.1.319 (informational messages dropped for SDK clients), 2.1.318 (custom slash commands printing nothing in `-p` and SDK).
- **Tool payload size:** 2.1.91, 2.1.105, 2.1.261, 2.1.265, 2.1.274, 2.1.283, 2.1.287, 2.1.296 (Read `allow_large`), 2.1.217 (memory leak with truncated MCP output), 2.1.290 (unbounded memory on huge HTTP MCP responses).
- **Compaction:** 2.1.307, 2.1.310, 2.1.311, 2.1.314, 2.1.317 (see 2e).
- **Resume by transcript:** 2.1.296 and 2.1.317 (interrupted turns), 2.1.309 (rename across resumes), 2.1.311 (agent-team task list on resume), 2.1.312 (first reply after resume missing on the next resume), 2.1.314 (`--resume` moved sessions to unknown models).
- **SDK-specific transcript fixes:** 2.1.311 (transcript order and taken-back messages in `-p` and SDK), 2.1.309 (disabled MCP servers reconnected by permission prompts in SDK), 2.1.316 (`register_repo_root` CLAUDE.md), 2.1.290 (org MCP servers relisted in headless and SDK).

### B3. Claude API and platform (usable from this stack)

Everything below is dated from the API release notes unless marked otherwise. Source: https://platform.claude.com/docs/en/release-notes/overview.md (Feb 2026 to Nov 2026 cross-checked against the page's full text).

#### 3a. Prompt caching

- Automatic caching (2026-02-19): one top-level `cache_control` field caches the last cacheable block; the breakpoint moves forward with the conversation. Not available on legacy Bedrock for Opus 4.6 and earlier.
- 1-hour cache duration needs no beta header (2025-08-13). TTL is set with `{"type":"ephemeral","ttl":"1h"}`. Write price 2x base, 5-minute write 1.25x, read 0.1x (but see the pricing conflict note in 3h).
- Read multipliers by model: 0.025x (Fable 5.x and Mythos 5.x), 0.05x (Opus 5.5 and Sonnet 5.5), 0.1x (others, including Haiku 5.5).
- Minimum cacheable length: 512 tokens on Fable 5.x, Mythos 5.x, Opus 5, Opus 5.5, Sonnet 5.5, Haiku 5.5; 1,024 on Opus 4.8, Sonnet 5, Sonnet 4.6 and older; 2,048 on Opus 4.7 and Mythos Preview; 4,096 on Opus 4.5, Opus 4.6, Haiku 4.5. Shorter prompts are silently not cached.
- Usage object gains `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` (per-TTL breakdown).
- `max_tokens: 0` pre-warms the cache with no output (rejected with streaming, extended thinking, structured outputs, forced `tool_choice`, or batches).
- Cache diagnostics: beta 2026-05-13 (`cache-diagnosis-2026-04-07`); out of beta 2026-09-23 (`diagnostics` object opts in; responses always include the field, 2026-09-18 change to `null`).
- Mid-conversation system messages (2026-05-28; `role: "system"`) add instructions without invalidating the cache on newer models.
- Mid-conversation tool changes (beta 2026-07-24; header `inline-tools-2026-09-15`) add or remove tools without invalidating the tools cache.
- Opus 4.5 and later, Sonnet 4.6 and later, and Haiku 5.5 keep thinking blocks by default, so the cache stays valid when non-tool-result user content is added.
- Caching is isolated per workspace on the Claude API, Claude Platform on AWS and Microsoft Foundry.
- CLI-side: cache-miss causes in `/cost` (2.1.260), per-TTL subagent cache (2.1.243, 2.1.248), `ENABLE_PROMPT_CACHING_1H` (2.1.108).

#### 3b. Context editing, memory tool and compaction

- Context editing (beta, launched 2025-09-29). Beta header `context-management-2025-06-27`. Strategies:
  - `clear_tool_uses_20250919`: clears old tool results (optionally inputs with `clear_tool_inputs`). Triggers, `keep`, `clear_at_least`, `exclude_tools`.
  - `clear_thinking_20251015` (2025-10-28): clears thinking blocks from earlier turns, `keep` set by thinking turns. Put it first in `edits` when combined.
  - `compact_20260112`: server-side compaction edit (recommended).
  - `compaction_control`: client-side, deprecated in TS and Ruby SDKs, removed in the Python SDK v1.0.
- Response: `context_management.applied_edits` (cleared counts). `count_tokens` accepts the same object.
- Memory tool (`memory_20250818`): beta launched 2025-09-29; no beta header required since 2026-02-17. Combines with the tool-clearing strategy; Claude gets a warning to save to memory before results are cleared.
- Compaction API (beta, 2026-02-05, server-side summarisation on Opus 4.6).
- Compaction on demand (beta, header `compact-2026-09-04`, 2026-09-14): `compaction` parameter.
- Thinking-block validity: Fable 5.2 and Mythos 5.2 enforce a prefix check (2026-11-09) and reject edited `system`, `tools` or earlier messages with a replayed thinking block (400).
- **Relevant to your app:** the SDK exposes only `betas: list[SdkBeta]` with the 1M literal. You cannot add `context-management-2025-06-27` through the typed options. Context editing applies only to direct Messages API calls you make yourself (for example, from your own code rather than through the CLI). **Whether `extra_args` or other escape hatches can pass this header is unverified.**

#### 3c. 1M context

- 1M context window GA for Opus 4.6 and Sonnet 4.6 at standard pricing, no beta header (2026-03-13). Beta retired for Sonnet 4.5 and Sonnet 4 on 2026-04-30; requests over 200K return an error there.
- Native 1M on Opus 4.8 (2026-05-28), Sonnet 5 (2026-06-30), Fable 5 and Mythos 5 (2026-06-09), Opus 5 (2026-07-24), Fable 5.1 and Mythos 5.1 (2026-09-01), Sonnet 5.5 (2026-09-28), Opus 5.5, Haiku 5.5, Fable 5.2 and Mythos 5.2 (2026-11-09).
- Media limit for 1M requests raised from 100 to 600 images or PDF pages (2026-03-13).
- Your `betas` literal `context-1m-2025-08-07` is retired for current models. It is no longer needed for them.

#### 3d. Effort, thinking and sampling

- Effort parameter: GA, no beta header, supports Opus 4.6 (2026-02-05). Set `output_config.effort` (Messages API) or `effort` on the agent's model object (Managed Agents). Levels: `low`, `medium`, `high`, `max`, and `xhigh` (Opus 4.7 and later, added 2026-04). Opus 5 uses effort as the primary control (2026-07-24). Opus 4.8 defaults to `high` (2026-05-28).
- Per-message effort (beta): `role: "system"` messages with `output_config.effort` (2026-09-01); Google Cloud on Fable 5.1, Mythos 5.1 and Opus 5 (2026-09-03).
- Adaptive thinking: `thinking: {type: "adaptive"}` recommended from Opus 4.6. Manual `budget_tokens` deprecated on Opus 4.6, removed on Sonnet 5 (2026-06-30, now 400), and rejected on Fable 5 and Mythos 5 (2026-06-09, 400).
- Thinking display: `thinking.display` (2026-03-16) with `"omitted"` (default on Fable 5 and Mythos 5, 2026-06-09) or `"summarized"`. `thinking.display: "updates"` (beta, 2026-09-01) returns progress between tool calls.
- Sampling: `temperature`, `top_p` and `top_k` non-default values return 400 on Opus 4.8 (2026-05-28) and Sonnet 5 (2026-06-30).
- Tokenizer: Sonnet 5 and Fable 5 and Mythos 5 produce roughly 30% more tokens for the same text (2026-06-09, 2026-06-30). **This affects your token budgets and tool payload sizes.**
- `tool_choice` `any` and `tool` return 400 on Fable 5.1 and Mythos 5.1 (2026-09-01).
- Sonnet 5.5 "changes how thinking is turned off and how forced tool use works" (2026-09-28). The release note does not detail the change beyond that sentence; treat as **to verify before relying on it**.
- `usage.output_tokens_details.thinking_tokens` reports billed thinking tokens (2026-05-27).

#### 3e. Code execution, programmatic tool calling, web tools

- Code execution v2 (beta, 2025-09-02): Bash and file operations in addition to Python.
- Free when used with web search or web fetch (2026-02-17). Web search and programmatic tool calling need no beta header (2026-02-17).
- Dynamic filtering for web search and fetch (2026-02-17).
- Programmatic tool calling beta (2025-11-24).
- `code_execution_20260120` (2026-06-18): REPL state persistence; needed for programmatic tool calling.
- `code_execution_20260521` (2026-06-11): discloses the 90-second per-cell limit.
- Tool search tool (beta 2025-11-24; no beta header 2026-02-17).
- Fine-grained tool streaming: no beta header on any model or platform (2026-02-05).
- Advisor tool (beta, 2026-04-09, header `advisor-tool-2026-03-01`): a faster executor with a higher-intelligence advisor. Managed Agents advisor role (2026-08-07). CLI `/advisor` (2.1.300).

#### 3f. Files API and skills

- Files API: beta launched 2025-05-22; out of beta 2026-08-19 with `expires_in_seconds`, pagination and `ids[]` filtering. `client.beta.files` drops beta headers (2026-08-27).
- Agent Skills and the Skills API: beta from 2025-10-16 (`skills-2025-10-02`); out of beta 2026-08-19 (header no longer required).
- Managed Agents loads skills from a GitHub repo's `.claude/skills` (2026-08-07).

#### 3g. Managed Agents (not what this SDK is)

- Managed Agents is Anthropic-hosted, with a per-session sandbox. Beta launched 2026-04-08 (`managed-agents-2026-04-01`). This is separate from the Agent SDK, which you host yourself.
- Milestones: memory for Managed Agents (beta 2026-04-23; `agent-memory-2026-07-22` header 2026-07-02); multiagent orchestration and outcomes (beta 2026-05-06); webhooks (2026-05-06); self-hosted sandboxes (2026-05-19); scheduled deployments and vault env-var credentials (2026-06-09); session budgets as a hard spend cap (2026-08-07); advisor role (2026-08-07); event deltas (2026-06-30, 2026-07-22); `initial_events` (2026-07-22); session overrides (2026-06-30); Dreams (memory refresh research preview, 2026-05-06, extended to Opus 5 on 2026-08-01 and Sonnet 5.5 on 2026-10-01).
- CLI `/claude-api` skill targets `multiagent_20261001` for subagent teams (2.1.309).

#### 3h. Models

Dates are the API release-note dates where given; otherwise the CLI version that added the model (npm date).

- Opus 4.6 (2026-02-05): adaptive thinking recommended; effort GA; compaction API beta; 1M beta (GA 2026-03-13); fast mode research preview 2026-02-07.
- Sonnet 4.6 (2026-02-17): 1M beta, later GA.
- Opus 4.7 (2026-04-16): updated tokenizer; API breaking changes; `xhigh` effort; task budgets beta (`task-budgets-2026-03-13`); high-resolution images.
- Opus 4.8 (2026-05-28): 1M by default; 128K max output; effort defaults to `high`; fast mode deprecated (removal date 2026-11-30 per 2.1.316).
- Fable 5 and Mythos 5 (2026-06-09): 1M; adaptive thinking only; `thinking.display` default `"omitted"`.
- Sonnet 5 (2026-06-30): 1M; adaptive thinking on by default; manual `budget_tokens` 400; sampling params 400.
- Opus 5 (2026-07-24): 1M; 128K max output; thinking on by default; effort primary; fast mode for Opus 5 (CLI 2.1.219, $10/$50).
- Fable 5.1 and Mythos 5.1 (2026-09-01): 1M; adaptive thinking; cache read 0.025x (CLI: Fable 5.1 at $10/$50 per Mtok, $0.25/Mtok cache reads).
- Opus 5.5 (CLI 2.1.280, 2026-09-22): default Opus. CLI pricing $4/$20, $0.20/Mtok cache reads.
- **Sonnet 5.5 (`claude-sonnet-5-5`, 2026-09-28):** default Sonnet on the Anthropic API (CLI 2.1.284, 2026-09-28). 1M context. CLI pricing $2/$10 per Mtok. Thinking-off and forced-tool-use behaviour changed (release-note sentence; details not read). Minimum cache length 512 tokens. Thinking blocks are account-bound; blocks from other accounts are dropped (2026-09-28). Your model is `claude-sonnet-5-5`, which exists and is current.
- Haiku 5.5 (CLI 2.1.293; date not checked): default Haiku on the API; 1M context; CLI pricing $0.10/$0.50 per Mtok ($0.50/$2.50 above 100K). Separate pricing tiers at 100K.
- Fable 5.2 and Mythos 5.2 (2026-11-09): 1M; 128K max output; always-on adaptive thinking; prefix check enforced; thinking blocks bound to producing account. CLI 2.1.321 says Fable 5.2 is default Fable, $8/$40 with $0.20/Mtok cache reads.
- Model deprecations to watch: Sonnet 4.5 retirement 2026-11-30 (announced 2026-09-30); Opus 4.1 retirement 2026-08-05; Sonnet 4 and Opus 4 retired 2026-06-15; Haiku 3 retired 2026-04-20.
- Batch processing: `max_tokens` up to 300K for Opus 4.6 and Sonnet 4.6 (beta header `output-300k-2026-03-24`, 2026-03-30).

**Pricing conflict (verify):** The CLI changelog (2.1.284) lists Sonnet 5.5 cache reads at $0.20/Mtok on a $2 base, which is 0.1x. The caching page lists a 0.05x read multiplier for Sonnet 5.5, which is $0.10/Mtok. Check https://platform.claude.com/docs/en/about-claude/pricing before using either figure.

#### 3i. Other API items

- Structured outputs GA on Claude API for Sonnet 4.5, Opus 4.5 and Haiku 4.5 (2026-01-29). `output_format` moved to `output_config.format`.
- Token counting and Models API capabilities (`max_input_tokens`, `max_tokens`, `capabilities`) (2026-03-18). Thinking capability types `disabled` (2026-10-05) and `between_tools` (2026-10-15).
- Message threads (beta `message-threads-2026-08-12`, 2026-11-02): server-held conversations, so later requests send only new messages.
- API version `2026-10-21` released (2026-10-21), with SDKs sending it by default. Migration page exists.
- Data residency: `inference_geo` (2026-02-05).

### B4. Pain points mapped to the changes above

| Pain point | Relevant changes | Notes |
|---|---|---|
| "Stream closed" on MCP calls mid-turn | SDK 0.2.127 (2026-07-24) and 0.2.160 (2026-09-25); CLI 2.1.219 and CLI 2.1.290 | Best match: background subagents closing stdin early. Upgrade the SDK. Not in the CLI changelog. |
| Tool payload size limits | CLI 2.1.91 (500K `maxResultSizeChars`); SDK 0.1.55; CLI 2.1.261 (`bashOutputMaxChars` up to 128K); CLI 2.1.265 (1 GB disk cap); SDK 0.2.111 (NDJSON 64 KiB buffer whitespace fix); CLI 2.1.296 (Read `allow_large`) | The NDJSON fix is the one that can corrupt payloads silently. |
| Context compaction | CLI 2.1.105, 2.1.76 (hooks); CLI 2.1.299, 2.1.314 (idle); SDK 0.2.153 (`snapshot`); API `compact_20260112`, `compact-2026-09-04` | You cannot set API context editing through the typed SDK options. |
| Resume by transcript | SDK 0.2.137 (`resume_session_at`, `resume_drops_turn`); SDK 0.2.139 and 0.2.170; CLI 2.1.296, 2.1.317, 2.1.319 | `resume_drops_turn` guards against silently dropping unobserved turns. |
| Background job concurrency | CLI 2.1.217 (20 concurrent subagents); CLI 2.1.212 (200-spawn cap, removed 2.1.224); CLI 2.1.285 and 2.1.288 (background Bash time limit in SDK sessions); SDK 0.2.101 | Your app's background jobs are unattended, so the 30-minute background Bash limit applies. |

### B5. Plugin eval and /skill-doctor (from the CLI changelog)

- `claude plugin eval`: added in CLI 2.1.269 (2026-09-11). Generally available, with `--disallow-tools` and `disallowed_tools` added in 2.1.299 and a git 2.31 minimum (2.1.283). Per the embedded reference, the availability check in this session is "available".
- `claude plugin eval init`: `claude plugin eval --help` in your build is the authority on flags.
- `/skill-doctor`: added in CLI 2.1.261 (approx. 2026-09-01 to 09-11). Relevant to your 10+ project skills.

### B6. Unverified items and gaps

- SDK 0.2.179 and 0.2.180 contents (release notes contain only the install line). Bundled CLI for both is unknown.
- SDK 0.2.157 has no changelog entry. Its bundled CLI is unknown.
- SDK 0.1.34 PyPI upload date (not in the index I queried).
- Versions for `rewind_files`, `set_model`, `set_permission_mode`, `reconnect_mcp_server`, `toggle_mcp_server`, `get_server_info`, `stop_task`, `get_mcp_status`, and the `load_timeout_ms` option. No changelog entry for these.
- Whether `hooks={...}` accepts CLI-only hook event names (PostCompact, StopFailure, etc.) at runtime. The SDK type literal lists only 10.
- Whether `output_format` maps to the API's `output_config.format`.
- Whether the typed SDK can pass `context-management-2025-06-27` or other beta headers.
- Sonnet 5.5 cache-read price (conflict between CLI and docs; see 3h).
- Sonnet 5.5 thinking-off and forced-tool-use changes, beyond the release-note sentence.
- Haiku 5.5 CLI date (2.1.293) and Opus 5.5 exact date (CLI 2.1.280, 2026-09-22 is the earliest evidence).
- CLI version dates marked "approx".
- Full CLI feature list: the 2.1.38 to 2.1.322 entries I reviewed are the feature-type bullets. The complete bullet list is in the files below.
