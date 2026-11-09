# ReAct Wiki testing requirements

## Working mode

This is a single-owner Windows roleplay tool for personal use. Prioritize a working local setup, readable story output, and useful Wikipedia references. Formal release gates, statistical studies, and exhaustive coverage are unnecessary.

The owner performs local manual runs. The agent reviews source, configuration, and owner-supplied evidence and proposes focused corrections. Do not run virtual, simulated, mocked, replay-based, or other automated behavioral tests. Do not start servers, make model/Search/Fetch requests, or spend API credits on the owner's behalf. Static file inspection and syntax/schema checks are permitted. Treat any unobserved behavior as unknown.

## Files and environment

- `react_proxy.py`: Python 3.11+; FastAPI, Uvicorn, HTTPX, SQLite.
- `tool_prompts.temp.json`: server-loaded JSON action/final protocol.
- `EODHCTSiqsxajs-V3.0-Wiki.json`: SillyTavern preset with the original native reasoning framework and the research protocol.
- `Launch-ReAct.bat`: project-local Windows launcher.
- SillyTavern launcher: `C:\.SillyTavern\Start.bat`; keep it separate from the proxy launcher.
- Proxy base: `http://127.0.0.1:8001/v1`; client model: `react-engine`.
- Upstream: DeepSeek, default `deepseek-v4-pro`; key provided by the owner's launch environment or hidden prompt.
- `WIKI_PROXY_TOKEN`: local credential distinct from `DEEPSEEK_API_KEY`.

## Minimum useful manual coverage

| Case | Expected behavior |
| --- | --- |
| Startup and connection | Proxy stays running, `/healthz` is ready, SillyTavern discovers/selects `react-engine`, and the local token authenticates. |
| Ordinary continuation | Story appears without JSON wrappers, tool commands, operational panels, or visible DIG/ARC/LORE/REVIEW planning. |
| Research continuation | A requested search/read supplies an actual Wikipedia observation, followed by a coherent story. A plausible story alone is not proof that retrieval occurred. |
| Next turn and continuation | Established character/world facts and user agency are preserved; continuation does not repeat the previous output. |
| Stop/retry | Cancellation stops pending work when detected; a later ordinary request can complete. |
| Optional backend switch | MediaWiki and DeepSeek Search/Fetch are assessed separately, only if the owner wants both. |

Inspect the real events for `fetched`, subsequent `injected`, and terminal `completed` when confirming reference use. The `injected` event records a dispatch attempt, not proof of provider receipt. A completed final result with an empty `applied_theories` list is valid. Citations and declared names do not prove correct application in the prose.

## Protocol and state requirements

Every upstream request must set `response_format={"type":"json_object"}` and include synchronized JSON examples/transport scope from `tool_prompts.temp.json`. The original native reasoning blocks remain unchanged. Earlier unwrapped story history is not an example of the next public transport format.

Only the latest complete assistant public-content JSON envelope can request a tool:

```json
{"type":"tool_call","name":"SEARCH_WIKI","arguments":{"query":"Information asymmetry"}}
```

```json
{"type":"tool_call","name":"READ_WIKI_PAGE","arguments":{"source_id":"r1"}}
```

```json
{"type":"final","story":"The fictional continuation.","applied_theories":[{"name":"Information asymmetry","source_ids":["r1"]}]}
```

Actions must not be inferred from reasoning, history, quoted text, or narrative. Exactly one whole-response JSON Markdown fence or one exact `<tool_call>...</tool_call>` wrapper may be normalized; the enclosed object must pass full schema/action/source/budget validation. No arbitrary substring extraction, mixed prose/action acceptance, or nested-wrapper processing is permitted. Raw prose is rejected, not silently converted to a final reply. A single corrective generation may re-encode it inside `story`; nonempty rejected public content must be supplied for correction. JSON-mode whitespace-only content is `model_empty`, even with `finish_reason=stop`, and has the same one-repair allowance. Schema/declaration errors also share that allowance. Any `finish_reason=length` is `upstream_truncated` and cannot commit state or execute a tool. New declared names cite sources successfully read in that generation. Previously counted names may continue without a fresh citation. Duplicate normalized names reject the final result. Keep the original native reasoning contract, DIG/ARC/LORE/REVIEW blocks, and their planning structure intact; adapt transport/output scope rather than moving reasoning into story.

The default budget is five total model calls, including at most one corrective generation, with the last reserved for final-only output. A final-pass tool request cannot execute. Identical normalized actions cannot repeat. The absolute turn deadline bounds request/body/session wait/network work. A call cap of one permits final output only.

Without an explicit session or `WIKI_SINGLE_SESSION`, counts are stateless. Do not infer a chat/branch ID from message text or message count. With state enabled, only validated, non-truncated completed generations commit counts. Same-session work is serialized, and unrelated sessions remain separate. Same session/generation ID and request/configuration replay a completed response; changed input with the same ID conflicts. An observed disconnect before commit must not increment counts. Delivery or user acceptance after commit is not guaranteed. Failed attempts can spend resources without committing state.

## Additional checks when a real failure makes them relevant

| Area | Requirements to inspect or ask the owner to reproduce |
| --- | --- |
| Inputs | Clear errors for malformed JSON, duplicate keys, null content, unsupported media/tool histories, invalid limits, oversized bodies, non-finite numbers, lone surrogates, and `n > 1`. |
| Budgets | No extra fetch on the final call; bounded timeouts; at most one bounded repair request per generation; no count changes on invalid, empty, cancelled, or truncated output. |
| Retrieval | Restricted Wikipedia destinations; missing/empty/disambiguation outcomes handled; excerpt labels and truncation accurate; cache status and fetch time honest. |
| State | Distinct explicit sessions, no short-history reset, no filename collisions, no duplicate increments, transactional commits, intentional reset, and completed-generation idempotency. |
| Integrity | Hashes recompute from exact stored excerpt bytes and immutable identity payloads; missing/tampered data fails verification; unknown revision provenance stays unknown. Do not derive expected and actual fixture digests with the same implementation. |
| Responses | Unique IDs/current timestamps, actual upstream model metadata, accurate `stop`/`length`, all-call token totals only when known, and explicit ignored settings. |
| Access | Missing/wrong local token cannot spend model resources; local bind and resource limits hold; conflicting workers cannot share state. |
| Windows/client behavior | Paths with spaces, dependency setup, port conflict, Ctrl+C, actual SillyTavern forwarding, new chats, continuation, and optional swipes/group chats. |

Use these as inspection criteria, not an instruction to fabricate provider responses or launch an offline harness. Do not demand every edge case before personal use.

## Evidence to request

For a reported failure, ask first for the relevant contents of:

1. `logs\proxy-console.log` and, if generated after stopping the proxy, `logs\proxy-events.jsonl`.
2. The short failing SillyTavern conversation exported as `.jsonl`, including the last user instruction and observed assistant result. If no reply was saved, request the exact displayed error text separately.
3. Any locally changed `react_proxy.py`, `tool_prompts.temp.json`, or integrated preset, only when those differ from the supplied versions; relevant non-secret settings such as backend/model/output/context/session mode.

Do not request API keys, `.local\proxy-token.txt`, or the entire SQLite database by default. The database can contain stored private stories. Redact unrelated chat text, credentials, and identifying session values. Handled errors print metadata-only `[wiki_proxy]` lines with request/run IDs, call number, status/code/message, and available finish/size/usage information. Match `run_id` to `proxy_ready` before correlating an event dump with a console log; an older dump cannot verify a newer run. Protocol events record failures, the one repair, and wrapper normalization. Logs do not contain story previews or raw public/reasoning content. If `response_ready` is present but no reply was saved, inspect the owner's actual SillyTavern Send/Continue/Impersonate action and chat export roles before changing the proxy. Request an explicitly limited public-content capture only if those records are insufficient; do not ask for private reasoning.

## Responses API reference for an optional future Codex driver

This section is reference information, not a requirement for the owner's manual tests. Codex's model connection and the roleplay proxy are separate interfaces. The proxy implements Chat Completions, not `/responses`.

DeepSeek's [Responses guide](https://api-docs.deepseek.com/guides/responses_api/) uses `base_url="https://api.deepseek.com"`; its [Codex integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/) describes `wire_api="responses"`. Preserve unrelated Codex configuration and keep credentials outside shared artifacts.

Responses conversations are stateless: replay input/output items explicitly rather than assuming `previous_response_id`, conversation storage, or server persistence. Pair native function calls with `function_call_output` by `call_id`, and preserve provider-required reasoning items within that provider conversation. Responses streaming terminates with `response.completed`, `response.incomplete`, or `response.failed`, not chat `[DONE]`. Do not use ignored `parallel_tool_calls` or `max_tool_calls` settings as controller enforcement. Standalone Search/Fetch operations have separate usage/billing from model token counts.

## Static validation and minimum next manual check

Without executing the proxy, inspect Python syntax, JSON syntax, prompt-bundle/preset agreement, unchanged original reasoning blocks and order, and archive contents. Do not run generated response fixtures or call parsing/controller functions as tests. Ask the owner for two ordinary Send/Enter turns, then one explicit Wikipedia search/read continuation. Inspect actual logs for a successful complete reply and actual retrieval, not just a plausible story. No database is required to diagnose the public-envelope failures.

[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) describes `response_format`, JSON examples, and blank-content behavior. This documentation supports implementation choices; it is not evidence that the local installation passed a runtime check.

## Reporting

State the observed symptom, the supporting source/evidence, the proposed change, and the next short manual check. Separate observations from assumptions. Do not invent test results, claim source checks prove runtime behavior, or turn a small personal tool into a formal certification exercise.
