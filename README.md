# ReAct Wiki for SillyTavern

A local reverse proxy that lets DeepSeek look up Wikipedia references before continuing a roleplay scene. It preserves the preset's native DIG / ARC / LORE / REVIEW planning framework and returns the story to SillyTavern without tool envelopes or operational panels.

## Files

| File | Purpose |
| --- | --- |
| `react_proxy.py` | Python proxy server |
| `tool_prompts.temp.json` | Tool protocol loaded by the server; keep this file beside the Python file |
| `EODHCTSiqsxajs-V3.0-Wiki.json` | Complete integrated SillyTavern preset to import |
| `Launch-ReAct.bat` | Windows proxy launcher, dependency setup, and local feedback logs |
| `Agent.md` | Optional instructions for an AI reviewing source or manual-test feedback; not needed to run the tool |

The complete preset and server prompt bundle have different purposes. Import the complete preset into SillyTavern; leave the smaller bundle beside the proxy. The original template's optional modules, native reasoning contract, operators, and prompt order are preserved. A transport bridge places the existing visible narrative format inside the final JSON `story` field. Research requests remain separate public JSON actions.

## Windows setup

Install Python 3.11 or later with the Windows Python launcher or `python` available. Put the files in a separate directory, for example `C:\.SillyTavern\wiki-proxy`.

Do not replace `C:\.SillyTavern\Start.bat`. That script starts SillyTavern/Node.js. The proxy has its own launcher and Python environment.

1. Double-click `Launch-ReAct.bat`. On first use it creates `.venv` and installs FastAPI, Uvicorn, and HTTPX. Internet access is required for that installation.
2. Enter the DeepSeek API key at the hidden prompt, unless it is already set in the launch environment. The launcher does not save this key.
3. Copy the **local proxy token** displayed by the launcher. If no token was supplied in the environment, the launcher creates and reuses `.local\proxy-token.txt`.
4. Keep that window open and start SillyTavern separately using `C:\.SillyTavern\Start.bat`.
5. Import `EODHCTSiqsxajs-V3.0-Wiki.json` and configure the connection below. Keep the original preset if you want to switch back later.

The proxy launcher starts only the proxy. It does not start, reinstall, or terminate SillyTavern. Press Ctrl+C in the proxy window to stop it. Avoid running a second proxy instance against the same port/data directory.

## SillyTavern connection

| Setting | Value |
| --- | --- |
| API type | Chat Completion |
| Source | Custom (OpenAI-compatible) |
| Base URL | `http://127.0.0.1:8001/v1` |
| Model | `react-engine` |
| API key | The local proxy token printed by the launcher |
| Prompt post-processing | None |
| Function calling / built-in web search | Disabled; the proxy manages research itself |

Do not append `/chat/completions` to the base URL. The upstream DeepSeek key belongs to the proxy, not this local provider connection.

The integrated preset uses an 8,192-token output limit and a 16,384-token client context setting. Continuation prefill is disabled so each upstream response can use a complete JSON envelope. Other narrative options and the native reasoning framework remain available.

The proxy explicitly uses DeepSeek thinking at `high` effort. A preset's displayed reasoning-effort option does not change this server setting. Provider reasoning is used internally and is not forwarded to SillyTavern; enabling “show thoughts” in the preset cannot make it appear through this proxy.

Both ordinary JSON responses and buffered chat SSE are supported. With streaming enabled, SillyTavern receives the story only after research and final generation complete; this is not progressive token streaming. For a simpler first connection, turn streaming off in SillyTavern.

## Retrieval and narrative behavior

The model can finish immediately or request a Wikipedia search followed by a page read. It reads only pages identified by results in the current generation. Up to five model calls are available by default; the final call is reserved for finishing the story, so at most four actions can execute.

The proxy requests DeepSeek JSON Output with `response_format={"type":"json_object"}` on every model call. The complete public response is either a `tool_call` JSON envelope or a `final` envelope containing `story` and `applied_theories`. The proxy validates the schema, executes allowed actions, and unwraps the final story. JSON mode controls serialization; it does not replace validation of action names, source IDs, final fields, or declarations. Never put actions inside a visible thinking block. Quoted action text in a story does not dispatch a tool.

The bundled `tool_prompts.temp.json` includes the same transport bridge used in the complete preset. Earlier assistant stories are treated as already-unwrapped history. Instructions to start directly with prose apply to the `story` value; every new public response still uses a complete JSON object. Native reasoning and DIG / ARC / LORE / REVIEW planning remain separate from the story.

For compatibility, the parser accepts one complete JSON Markdown fence, or one exact `<tool_call>...</tool_call>` wrapper containing a valid action envelope, around the entire latest public response. It then applies all ordinary validations before execution. It never searches prose, chat history, or reasoning for actions. Nested wrappers, surrounding commentary, and narrative mixed with actions are rejected.

A malformed/blank response or invalid envelope can receive one corrective model call within the existing total-call and time budgets. When nonempty, the rejected public response is included for correction. A prose response must be re-encoded by the model inside `story`; the proxy does not silently accept raw prose or invent declarations. No repair can spend beyond the call cap or dispatch a tool on the reserved final call. Successful replies report whether repair or wrapper normalization occurred in `wiki_proxy` metadata, and token usage includes the corrective call when available.

DeepSeek documents that JSON mode may return whitespace-only `content`, even with `finish_reason=stop`; the proxy treats this as `model_empty` and applies the same single-repair allowance. A second invalid response fails with its specific code. Any `finish_reason=length` fails immediately with `upstream_truncated`, including a syntactically complete envelope. Failed or truncated generations do not commit counts. See [JSON Output](https://api-docs.deepseek.com/guides/json_mode/).

| Backend | Behavior |
| --- | --- |
| `mediawiki` (default) | Wikipedia search and introductory plain-text excerpts |
| `deepseek` | Official DeepSeek Search & Fetch, restricted to the configured Wikipedia language host; additional retrieval charges may apply |

Retrieved text is reference material, not an instruction source or proof that a theory is correct. New declared lenses cite pages successfully read in the current generation. No declared lens is also a valid outcome. Character continuity, established world facts, and player agency remain the narrative priorities.

The proxy uses asynchronous network requests and a small five-minute cache. Excerpts have accurate labels/truncation metadata and saved content digests. It never claims an introductory excerpt is a full article.

## Language and length behavior

The preset's `Module: Output Language and Narrative Form` keeps English as the default visible narrative language and now adds a conditional exception: a character explicitly defined as non-English speaking, or able to use other languages, speaks in that language when appropriate. The same applies when the scene is set in a region where English is not spoken, or where several languages are in use; those languages must then appear in the output. The surrounding prose guidance—concrete, mixed, scene-bound writing rather than abstract summary—is unchanged.

`Module: Optional Length Target - OP` raises the soft target for main narrative from 250 to 500 English words. It is explicitly not a hard single-turn cap: when the pace, the scene, or the user's request calls for more, the reply may exceed it, and a shorter reply is equally acceptable. Padding or compressing an arc just to hit the number remains prohibited. The preset's 8,192-token output limit and the proxy's `WIKI_MAX_OUTPUT` still bound the actual response size.

## Configuration

The launcher respects existing environment variables. No `.env` reader is included. For simple personal customization, add PowerShell assignments in the launcher's defaults section, such as `$env:WIKI_BACKEND = 'deepseek'`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | Hidden prompt if absent | Upstream API credential |
| `WIKI_PROXY_TOKEN` | Generated/reused local token | SillyTavern's local API credential; at least 24 characters and different from the upstream key |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | Upstream model |
| `WIKI_BACKEND` | `mediawiki` | `mediawiki` or `deepseek` |
| `WIKI_LANGUAGE` | `en` | Wikipedia language |
| `WIKI_PORT` | `8001` | Local port; change the SillyTavern URL too if changed |
| `WIKI_MAX_CALLS` | `5` | Total model-call cap, including finalization |
| `WIKI_MAX_OUTPUT` | `8192` | Maximum allowed per-call `max_tokens`; includes reasoning generation |
| `WIKI_TURN_SECONDS` | `240` | Absolute request deadline in seconds |
| `WIKI_CONTEXT_BUDGET` | `262144` | Conservative local admission budget; UTF-8 request bytes + 4,096 margin + output reservation, not an exact model token count |
| `WIKI_EXCERPT_CHARS` | `4000` | Maximum normalized excerpt characters |
| `WIKI_CONCURRENCY` | `2` | Concurrent admitted generations |
| `WIKI_DATA_DIR` | `runtime-data` beside the script | Local state and reference snapshots |
| `WIKI_SINGLE_SESSION` | Unset | Optional persistent identity for one active story |
| `WIKI_THEORY_MAX_USES` | `0` | Advisory counts; a positive value enforces a declared-name cap and requires a session |
| `WIKI_USER_AGENT` | Project-identifying string | Descriptive Wikipedia client identifier |

The integrated preset does not need `WIKI_LEGACY_PRESET_FILE`; its launcher clears that old adapter setting for this process. Keep `tool_prompts.temp.json` synchronized with any intentional protocol changes in the complete preset.

### Local context-budget errors

`context_budget` is the proxy's local guard. The blocked model call has not been sent to DeepSeek. The guard adds serialized request UTF-8 bytes, a 4,096-unit margin, and the requested output-token reservation. It deliberately overestimates input size, particularly for Chinese text; a new chat can exceed the old 65,536-unit default because the preset, character, and world-book material are included before any history.

The default is now 262,144 in both the server and launcher. This changes the local admission limit; it does not change SillyTavern's context setting, increase the requested output, or alter the CoT prompts. The upstream provider still enforces its own token limit. DeepSeek's current [model table](https://api-docs.deepseek.com/quick_start/pricing/) lists a 1M-token context for `deepseek-v4-pro`; check that table if you change models.

If an existing environment variable still supplies the old value, replace it or add this assignment in the launcher's PowerShell defaults section, then restart the proxy:

```powershell
$env:WIKI_CONTEXT_BUDGET = '262144'
```

Future rejections report the numeric breakdown in the displayed error, `logs\proxy-console.log`, and a `context_budget_exceeded` operational event. Send the full error and that event/log line if it still fails. If research calls occurred before a later call was blocked, those earlier calls may already have incurred charges. For requests that exceed the revised budget, review the breakdown before raising it again or reduce included history/world-book material. Do not increase output above `WIKI_MAX_OUTPUT` in the preset without changing the server limit too.

## Optional persistent counts

The default is stateless: different chats do not silently share a theory-count file. For one persistent story, uncomment the launcher's `WIKI_SINGLE_SESSION` assignment and choose its name. Restart the proxy after changing that name. The launcher does not automatically follow chat switches or swipes; use a different name for an independent story/branch.

Counts describe normalized names declared by completed model generations, not independently verified theory application or user acceptance. They normally advise variety while allowing continuity. Setting `WIKI_THEORY_MAX_USES=3` enforces a maximum for those names; it cannot detect disguised semantic reuse.

Clients with an explicit bridge can send `X-Wiki-Session` and `Idempotency-Key`, or equivalent `wiki_proxy.session_id` and `wiki_proxy.generation_id` body fields. A completed identical generation can then be replayed without another count increment/model call. Ordinary SillyTavern chat requests are not assumed to contain those IDs.

State commits only after a validated, non-truncated final result. Invalid output or a cancellation observed before commit does not increment counts. An authenticated `POST /v1/wiki/reset` with `{"session_id":"your-story-id"}` intentionally resets that session's counts and response-replay ledger.

## Local files and feedback records

- `.local\proxy-token.txt`: the local connection token. Keep it private.
- `logs\proxy-console.log`: latest launch header and proxy console output. `[wiki_proxy]` records include startup version/digests, a run ID, request ID, elapsed time, model-call number, finish reason, content/reasoning character counts, available token counts, and error codes. Diagnostic lines do not include prompts, story previews, reasoning text, or credentials.
- `logs\proxy-events.jsonl`: up to eighty recent operational events from the latest recorded run, exported when the launcher gets to its shutdown cleanup. If the window is forcibly closed, it may not be generated. Compare its `run_id` with the console's `proxy_ready` line before treating it as evidence for that run.
- `runtime-data\state.sqlite3`: counts, operational events, public reference snapshots, and completed-response replay data. Replay data can contain private story text. SQLite may also use WAL/SHM files while running.

The logs are feedback aids, not a transcript of the model's public JSON envelope or its private reasoning. A successful research path can show `fetched`, then `injected`, then `completed`. `injected` means a dispatch attempt, not independently confirmed provider receipt. `protocol_failure`, `repair`, and `envelope_normalized` record envelope handling using metadata only. `response_ready` means the proxy constructed a valid reply; delivery and saving in SillyTavern remain separate steps.

### Feedback when a reply does not appear

Restart after replacing project files. Check that `proxy_ready` identifies version `2.0.3-temporary`. Import the updated complete preset and select it. Keep the prompt bundle beside the server. Use ordinary Send/Enter for a new reply; SillyTavern's Continue action extends an existing message. Turning streaming off for the first manual check makes HTTP errors easier to read.

A short check is enough: send two ordinary turns in a new chat, then ask explicitly for a Wikipedia search and page read before continuing. If a reply fails, send the displayed error and the new `logs\proxy-console.log`; optionally include `logs\proxy-events.jsonl` after Ctrl+C. These identify the failing request/call and distinguish blank output, invalid JSON/schema, output limits, retrieval failures, deadlines, and cancellations. Include the short chat export if the reply reached the proxy's `response_ready` stage but was not saved correctly. No full database is needed for an envelope-format failure.

The database format remains compatible with the earlier version. Keep your existing `runtime-data` and `.local` directories; replacing the six project files does not require a state reset.

To recompute saved excerpt and identity hashes locally, the executable also provides `.\.venv\Scripts\python.exe react_proxy.py --verify "runtime-data\state.sqlite3"`. A checksum match does not establish a source revision or academic correctness; unavailable revision provenance stays unknown.

The server binds to loopback and requires a local bearer token. Keep runtime data out of shared/public folders. Stopping the proxy and switching SillyTavern's connection/preset restores your other setup; the original SillyTavern startup script is independent.

## API references

- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek Search & Fetch](https://api-docs.deepseek.com/guides/search/)
- [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [SillyTavern Custom Chat Completions](https://docs.sillytavern.app/usage/api-connections/openai/)
