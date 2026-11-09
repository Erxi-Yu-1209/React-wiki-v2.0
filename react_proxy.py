#!/usr/bin/env python3
"""Temporary Wikipedia roleplay proxy. Python 3.11+. See adjacent README.md.

No network work occurs on import. Run one process; configuration is read at startup.
The public protocol is pseudo-tool JSON, not native provider function calling.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

VERSION = "2.0.3-temporary"
ROOT = Path(__file__).resolve().parent
ALIAS = "react-engine"
DEEPSEEK = "https://api.deepseek.com"


class Failure(Exception):
    def __init__(self, status: int, code: str, message: str, upstream_status=None):
        self.status, self.code, self.message = status, code, message
        self.upstream_status = upstream_status


def require(ok, message, status=400, code="invalid_request"):
    if not ok:
        raise Failure(status, code, message)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def diagnostic(request, event, **details):
    """Print status/size metadata only; never supply prompts, stories, or reasoning here."""
    if not hasattr(request.state, "trace_id"):
        request.state.trace_id = uuid.uuid4().hex[:12]
        request.state.started_at = time.monotonic()
    store = getattr(request.app.state, "store", None)
    value = {"event": event, "request_id": request.state.trace_id,
             "run_id": getattr(store, "run_id", None),
             "elapsed_ms": int((time.monotonic() - request.state.started_at) * 1000), **details}
    print("[wiki_proxy] " + compact(value), file=sys.stderr, flush=True)
    return request.state.trace_id


def failure_response(request, status, code, message, **details):
    request_id = diagnostic(request, "request_failed", status=status, code=code, message=message,
                            call=getattr(request.state, "model_call", None), **details)
    return JSONResponse({"error": {"message": message, "type": "proxy_error", "code": code,
                                   "request_id": request_id}}, status_code=status)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Non-finite JSON number")
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON number")
        return result
    def validate(value, depth=0):
        if depth > 100:
            raise ValueError("JSON nesting limit")
        if isinstance(value, str):
            value.encode("utf-8")  # Reject lone Unicode surrogate escapes.
        elif isinstance(value, dict):
            for key, item in value.items():
                validate(key, depth + 1)
                validate(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                validate(item, depth + 1)
    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    validate(result)
    return result


def bounded_string(value, maximum, label):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
            f"{label} must be a nonempty string of at most {maximum} characters.")
    require(not any(ord(c) < 32 for c in value), f"{label} contains a control character.")
    return value.strip()


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, f"{label} must be an integer in [{low}, {high}].")
    return value


@dataclass(frozen=True)
class Config:
    key: str
    token: str
    model: str
    backend: str
    language: str
    calls: int
    deadline: int
    output: int
    context: int
    excerpt: int
    concurrency: int
    port: int
    user_agent: str
    data_dir: Path
    single_session: str | None
    theory_limit: int

    @classmethod
    def load(cls):
        def envint(name, default, low, high):
            try:
                return integer(int(os.getenv(name, str(default))), low, high, name)
            except ValueError as exc:
                raise Failure(500, "configuration", f"{name} must be an integer.") from exc
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        token = os.getenv("WIKI_PROXY_TOKEN", "").strip()
        require(bool(key) and len(token) >= 24 and token != key,
                "Set DEEPSEEK_API_KEY and a distinct WIKI_PROXY_TOKEN (at least 24 characters).", 500, "configuration")
        backend = os.getenv("WIKI_BACKEND", "mediawiki")
        require(backend in ("mediawiki", "deepseek"), "WIKI_BACKEND must be mediawiki or deepseek.", 500)
        language = os.getenv("WIKI_LANGUAGE", "en")
        require(len(language) <= 12 and bool(re.fullmatch(r"[a-z]{2,12}(?:-[a-z]{2,12})?", language)), "Invalid WIKI_LANGUAGE.", 500)
        model = bounded_string(os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"), 128, "DEEPSEEK_MODEL")
        single_session = os.getenv("WIKI_SINGLE_SESSION") or None
        if single_session:
            single_session = bounded_string(single_session, 128, "WIKI_SINGLE_SESSION")
        return cls(key, token, model, backend, language,
                   envint("WIKI_MAX_CALLS", 5, 1, 8), envint("WIKI_TURN_SECONDS", 240, 10, 600),
                   envint("WIKI_MAX_OUTPUT", 8192, 256, 32768),
                   envint("WIKI_CONTEXT_BUDGET", 262144, 4096, 1048576),
                   envint("WIKI_EXCERPT_CHARS", 4000, 256, 12000),
                   envint("WIKI_CONCURRENCY", 2, 1, 8), envint("WIKI_PORT", 8001, 1024, 65535),
                   bounded_string(os.getenv("WIKI_USER_AGENT", "ReActWiki/2.0 (local reference-guided fiction)"), 256, "WIKI_USER_AGENT"),
                   Path(os.getenv("WIKI_DATA_DIR", str(ROOT / "runtime-data"))).expanduser().resolve(),
                   single_session, envint("WIKI_THEORY_MAX_USES", 0, 0, 1000))


class ProcessGuard:
    """Prevent workers/processes sharing this state directory from making stale decisions."""
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (directory / "proxy.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise Failure(500, "state_in_use", "State directory is locked; run one proxy process.") from exc

    def close(self):
        self.file.close()


def theory_identity(name):
    key = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    identity = {"schema": "declared-lens-v1", "namespace": "roleplay-lens", "key": key}
    encoded = compact(identity)
    return key, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Store:
    """Short local transactions; no transaction is held during any network await."""
    def __init__(self, directory):
        self.run_id = uuid.uuid4().hex[:12]
        self.db = sqlite3.connect(directory / "state.sqlite3", timeout=0, isolation_level=None)
        require(self.db.execute("PRAGMA user_version").fetchone()[0] in (0, 1), "Unsupported state schema version.", 500, "state_schema")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS sessions(session TEXT PRIMARY KEY, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS counts(session TEXT, key TEXT, display TEXT, uses INTEGER NOT NULL,
                identity_json TEXT, theory_hash TEXT, PRIMARY KEY(session,key));
            CREATE TABLE IF NOT EXISTS generations(session TEXT, generation TEXT, fingerprint TEXT,
                response_json TEXT NOT NULL, PRIMARY KEY(session,generation));
            CREATE TABLE IF NOT EXISTS snapshots(digest TEXT PRIMARY KEY, excerpt BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time INTEGER, event_json TEXT NOT NULL);
            PRAGMA user_version=1;
        """)

    def state(self, session):
        if session is None:
            return {}, 0
        row = self.db.execute("SELECT version FROM sessions WHERE session=?", (session,)).fetchone()
        values = self.db.execute("SELECT key,uses FROM counts WHERE session=?", (session,)).fetchall()
        return dict(values), row[0] if row else 0

    def replay(self, session, generation, fingerprint):
        if session is None:
            return None
        row = self.db.execute("SELECT fingerprint,response_json FROM generations WHERE session=? AND generation=?",
                              (session, generation)).fetchone()
        if row:
            require(row[0] == fingerprint, "Generation ID was already used with a different request or configuration.", 409, "idempotency_conflict")
            response = strict_json(row[1])
            response["wiki_proxy"]["replayed"] = True
            return response
        return None

    def event(self, session, generation, event, **details):
        value = {"schema": 1, "run_id": self.run_id, "session": session,
                 "generation": generation, "event": event, **details}
        self.db.execute("INSERT INTO events(time,event_json) VALUES(?,?)", (int(time.time()), compact(value)))

    def snapshot(self, reference):
        self.db.execute("INSERT OR IGNORE INTO snapshots(digest,excerpt) VALUES(?,?)",
                        (reference["excerpt_sha256"], reference["excerpt"].encode("utf-8")))

    def commit(self, session, generation, fingerprint, version, declarations, result):
        if session is None:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            _, actual = self.state(session)
            require(actual == version, "Session state changed; retry with a new generation ID.", 409, "state_conflict")
            self.db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(session) DO UPDATE SET version=excluded.version",
                            (session, version + 1))
            for declaration in declarations:
                key, identity, digest = theory_identity(declaration["name"])
                old = self.db.execute("SELECT uses FROM counts WHERE session=? AND key=?", (session, key)).fetchone()
                before = old[0] if old else 0
                self.db.execute("INSERT INTO counts VALUES(?,?,?,?,?,?) ON CONFLICT(session,key) DO UPDATE SET uses=excluded.uses",
                                (session, key, declaration["name"], before + 1, identity, digest))
                self.event(session, generation, "declared_applied", key=key, theory_hash=digest,
                           before=before, after=before + 1, source_ids=declaration["source_ids"])
            self.db.execute("INSERT INTO generations VALUES(?,?,?,?)", (session, generation, fingerprint, compact(result)))
            self.event(session, generation, "committed", version=version + 1)
            self.event(session, generation, "completed", model_calls=result["wiki_proxy"]["model_calls"])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reset(self, session):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for table in ("counts", "generations", "sessions"):
                self.db.execute(f"DELETE FROM {table} WHERE session=?", (session,))
            self.event(session, None, "explicit_reset")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise


class SessionLocks:
    def __init__(self):
        self.entries = {}

    @asynccontextmanager
    async def hold(self, session):
        if session is None:
            yield
            return
        lock, users = self.entries.get(session, (asyncio.Lock(), 0))
        self.entries[session] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self.entries[session]
            if users == 1:
                del self.entries[session]
            else:
                self.entries[session] = (lock, users - 1)


def identity_from(request, data):
    cfg = request.app.state.cfg
    identity = data.get("wiki_proxy", {})
    require(isinstance(identity, dict) and set(identity) <= {"session_id", "generation_id"}, "Invalid wiki_proxy identity fields.")
    def choose(header, key):
        first, second = request.headers.get(header), identity.get(key)
        require(first is None or second is None or first == second, f"Conflicting {key} values.")
        value = first if first is not None else second
        return bounded_string(value, 128, key) if value is not None else None
    session = choose("X-Wiki-Session", "session_id") or cfg.single_session
    generation = choose("Idempotency-Key", "generation_id")
    require(generation is None or session is not None, "Idempotency requires an explicit session identity.")
    return session, generation or uuid.uuid4().hex


def authorize(request):
    cfg = request.app.state.cfg
    value = request.headers.get("authorization", "")
    require(secrets.compare_digest(value.encode(), ("Bearer " + cfg.token).encode()),
            "A valid local proxy bearer token is required.", 401, "unauthorized")
    # SillyTavern's backend connects without Origin. Browser cross-origin access is disabled.
    require(not request.headers.get("origin"), "Direct browser requests are not supported.", 403, "origin_rejected")


async def body_json(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        require(len(raw) <= 2_000_000, "Request exceeds 2,000,000 bytes.", 413, "body_too_large")
    try:
        data = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Failure(400, "invalid_json", "Body must be valid JSON with unique keys.") from exc
    require(isinstance(data, dict), "Body must be a JSON object.")
    return data


def messages_from(data, legacy_blocks=()):
    raw = data.get("messages")
    require(isinstance(raw, list) and 1 <= len(raw) <= 1000, "messages must contain 1–1000 entries.")
    messages = []
    for item in raw:
        require(isinstance(item, dict), "Every message must be an object.")
        require(item.get("role") in ("system", "user", "assistant"), "Only system/user/assistant messages are supported.")
        require(not item.get("tool_calls") and not item.get("function_call"), "Client-native tool histories are unsupported.")
        content = item.get("content")
        if isinstance(content, list):
            require(all(isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str) for p in content),
                    "Only text content parts are supported.")
            content = "\n".join(p["text"] for p in content)
        require(isinstance(content, str), "Message content must be a string or text-part list; null is unsupported.")
        if item["role"] == "system":
            for block in legacy_blocks:
                content = content.replace(block, "")
        # Remove only the exact identified legacy proxy prefix, never arbitrary user HTML.
        panel_start = '<details>\n<summary><span style="color: #00FF00; font-family: monospace; cursor: pointer;">[+] EODHCTSiqsxajs Cognitive Matrix (Click to Expand)</span></summary>'
        panel_end = '</blockquote>\n</details>\n<hr style="border: 1px dashed #555; margin: 15px 0;">\n\n'
        if item["role"] == "assistant" and content.startswith(panel_start) and panel_end in content:
            content = content.split(panel_end, 1)[1]
        msg = {"role": item["role"], "content": content}
        if "name" in item:
            msg["name"] = bounded_string(item["name"], 64, "message.name")
        messages.append(msg)
    return messages


def settings_from(data, cfg):
    require(data.get("model", ALIAS) == ALIAS, f"Select the local model alias {ALIAS}.")
    require(type(data.get("stream", False)) is bool, "stream must be boolean.")
    require(data.get("n", 1) == 1 and type(data.get("n", 1)) is int, "Only n=1 is supported.")
    require(not data.get("tools") and not data.get("functions"), "Client tools are unsupported; use the proxy research protocol.")
    output = integer(data.get("max_tokens", cfg.output), 1, cfg.output, "max_tokens")
    settings = {"model": cfg.model, "max_tokens": output, "stream": False,
                "response_format": {"type": "json_object"},
                "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    # This application intentionally does not send `tools`: reasoning replay is unnecessary.
    # Temperature/penalties have no effect in DeepSeek thinking mode and are reported as ignored.
    ignored = sorted(set(data) - {"model", "messages", "stream", "n", "max_tokens", "stream_options", "wiki_proxy"})
    if "stream_options" in data:
        opts = data["stream_options"]
        require(isinstance(opts, dict) and set(opts) <= {"include_usage"}
                and type(opts.get("include_usage", False)) is bool, "Unsupported stream_options.")
    return settings, ignored


class Network:
    def __init__(self, client, cfg):
        self.client, self.cfg = client, cfg

    async def json(self, method, url, *, payload=None, params=None, model=False):
        # Model POSTs are never automatically retried: their completion/billing may be ambiguous.
        for attempt in range(1 if model else 2):
            try:
                return await self.once(method, url, payload=payload, params=params, model=model)
            except Failure as exc:
                transient = exc.upstream_status in (408, 429, 500, 502, 503, 504)
                if model or attempt == 1 or not transient:
                    raise
                await asyncio.sleep(1)

    async def once(self, method, url, *, payload=None, params=None, model=False):
        headers = {"User-Agent": self.cfg.user_agent}
        if url.startswith(DEEPSEEK + "/"):
            headers["Authorization"] = "Bearer " + self.cfg.key
        try:
            async with self.client.stream(method, url, json=payload, params=params, headers=headers,
                                          timeout=120.0 if model else 20.0) as response:
                status = response.status_code
                if status != 200:
                    raise Failure(429 if status == 429 else 502, "upstream_http",
                                  f"{'Model' if model else 'Retrieval'} provider returned HTTP {status}.", status)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    require(len(raw) <= (8_000_000 if model else 2_000_000), "Provider response too large.", 502, "upstream_size")
                value = strict_json(raw)
                require(isinstance(value, dict), "Provider returned an invalid object.", 502, "upstream_schema")
                return value
        except httpx.TimeoutException as exc:
            raise Failure(504, "upstream_timeout", "Provider operation timed out.") from exc
        except (httpx.HTTPError, ValueError, UnicodeError, RecursionError, KeyError, TypeError) as exc:
            raise Failure(502, "upstream_transport", "Provider transport or JSON decoding failed.") from exc


class Retriever:
    def __init__(self, net, cfg):
        self.net, self.cfg = net, cfg
        self.host = cfg.language + ".wikipedia.org"
        self.api = "https://" + self.host + "/w/api.php"
        self.cache = OrderedDict()

    def safe_url(self, url):
        require(isinstance(url, str), "Invalid source URL.", 502, "source_rejected")
        p = urlsplit(url)
        require(p.scheme == "https" and p.netloc == self.host and p.path.startswith("/wiki/")
                and not p.query and not p.fragment and not any(ord(c) < 32 for c in url),
                "Source is outside the configured Wikipedia article endpoint.", 502, "source_rejected")
        return url

    async def cached(self, key, operation):
        now = time.monotonic()
        if key in self.cache:
            expires, value = self.cache.pop(key)
            if expires > now:
                self.cache[key] = (expires, value)
                return copy.deepcopy(value), True
        value = await operation()
        self.cache[key] = (now + 300, copy.deepcopy(value))
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return value, False

    async def search(self, query):
        async def operation():
            if self.cfg.backend == "deepseek":
                data = await self.net.json("POST", DEEPSEEK + "/v1/tools/search", payload={
                    "query": f"{query} site:{self.host}", "count": 5, "need_content": False})
                raw = data.get("results")
                require(isinstance(raw, list), "Invalid search result list.", 502, "retrieval_schema")
                results = []
                for row in raw:
                    require(isinstance(row, dict), "Invalid search result.", 502, "retrieval_schema")
                    try:
                        url = self.safe_url(row.get("url"))
                    except (Failure, ValueError):
                        continue
                    results.append({"title": str(row.get("title", ""))[:300], "url": url,
                                    "snippet": str(row.get("summary") or row.get("snippet") or "")[:700]})
                return results[:3]
            data = await self.net.json("GET", self.api, params={"action": "query", "list": "search",
                "srsearch": query, "srlimit": 3, "srnamespace": 0, "format": "json", "formatversion": 2})
            require("error" not in data, "Wikipedia search failed.", 502, "retrieval_schema")
            raw = data.get("query", {}).get("search")
            require(isinstance(raw, list), "Invalid Wikipedia search result.", 502, "retrieval_schema")
            return [{"title": row["title"], "pageid": row["pageid"],
                     "url": self.safe_url("https://" + self.host + "/wiki/" + quote(row["title"].replace(" ", "_"), safe="")),
                     "snippet": html.unescape(re.sub(r"<[^>]*>", "", row.get("snippet", "")))[:700]}
                    for row in raw[:3]]
        return await self.cached(("search", query), operation)

    async def read(self, source):
        url = self.safe_url(source["url"])
        async def operation():
            if self.cfg.backend == "deepseek":
                data = await self.net.json("POST", DEEPSEEK + "/v1/tools/fetch", payload={"url": url})
                title, text, pageid = data.get("title") or source["title"], data.get("content"), None
                kind = "page_markdown_excerpt"
            else:
                data = await self.net.json("GET", self.api, params={"action": "query",
                    "prop": "extracts|pageprops", "exintro": 1, "explaintext": 1,
                    "redirects": 1, "pageids": source["pageid"], "format": "json", "formatversion": 2})
                pages = data.get("query", {}).get("pages", [])
                require("error" not in data and isinstance(pages, list) and len(pages) == 1,
                        "Invalid Wikipedia page result.", 502, "retrieval_schema")
                page = pages[0]
                require("missing" not in page and "invalid" not in page, "Wikipedia page is missing.", 502, "missing_page")
                require("disambiguation" not in page.get("pageprops", {}), "Select a specific article instead of a disambiguation page.", 502, "disambiguation")
                title, text, pageid = page.get("title"), page.get("extract"), page.get("pageid")
                kind = "introductory_excerpt"
                url_canonical = "https://" + self.host + "/wiki/" + quote(title.replace(" ", "_"), safe="")
            require(isinstance(text, str) and bool(text.strip()), "No usable page text was returned.", 502, "empty_page")
            normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
            excerpt = normalized[:self.cfg.excerpt]
            return {"title": title, "url": url if self.cfg.backend == "deepseek" else self.safe_url(url_canonical),
                    "pageid": pageid, "language": self.cfg.language, "backend": self.cfg.backend,
                    "kind": kind, "revision_id": None, "revision_provenance": "unknown",
                    "fetched_at": int(time.time()), "extractor_version": "nfc-lf-prefix-v1",
                    "truncated": len(normalized) > len(excerpt), "excerpt": excerpt,
                    "excerpt_chars": len(excerpt), "excerpt_bytes": len(excerpt.encode("utf-8")),
                    "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()}
        return await self.cached(("read", url), operation)


def parse_reply(content):
    text = content.strip()
    require(bool(text), "Model returned blank public content in JSON mode.", 502, "model_empty")
    normalization = "none"
    # Normalize only one wrapper around the ENTIRE latest public response.
    # Never extract a call from narrative, history, reasoning, or an arbitrary substring.
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*)\r?\n```", text, re.S | re.I)
    wrapped = re.fullmatch(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
        normalization = "json_fence"
    elif wrapped:
        text = wrapped.group(1).strip()
        normalization = "tool_call_wrapper"
    try:
        value = strict_json(text)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Failure(502, "model_protocol", "Model response must be one complete JSON envelope.") from exc
    require(isinstance(value, dict), "Invalid model envelope.", 502, "model_protocol")
    require(normalization != "tool_call_wrapper" or value.get("type") == "tool_call",
            "A tool_call wrapper must contain a tool_call envelope.", 502, "model_protocol")
    return value, normalization


def action_arguments(value, sources):
    name, args = value["name"], value["arguments"]
    require(name in ("SEARCH_WIKI", "READ_WIKI_PAGE") and isinstance(args, dict), "Unknown action.", 502, "model_protocol")
    try:
        if name == "SEARCH_WIKI":
            require(set(args) == {"query"}, "SEARCH_WIKI needs query only.")
            argument = bounded_string(args["query"], 96, "query")
            require(not any(c in argument for c in (":", "|", "\n", "\r"))
                    and not re.search(r"\bOR\b", argument), "Use plain topic words, without operators.")
            signature = name + ":" + " ".join(unicodedata.normalize("NFKC", argument).casefold().split())
        else:
            require(set(args) == {"source_id"}, "READ_WIKI_PAGE needs source_id only.")
            argument = bounded_string(args["source_id"], 20, "source_id")
            require(argument in sources, "Unknown source_id; search first.")
            signature = name + ":" + sources[argument]["url"]
    except Failure as exc:
        raise Failure(502, "model_protocol", exc.message) from exc
    return name, argument, signature


def validate_reply(value, sources, read_sources, counts, cfg, final_only, seen):
    """Validate the complete envelope before any retrieval or state change."""
    if value.get("type") == "tool_call":
        require(not final_only, "Tools are not permitted on the final call.", 502, "tool_budget")
        require(set(value) == {"type", "name", "arguments"},
                "Unexpected action envelope fields.", 502, "model_protocol")
        name, argument, signature = action_arguments(value, sources)
        require(signature not in seen, "Repeated identical research action.", 502, "repeated_action")
        return {"type": "tool_call", "name": name, "argument": argument, "signature": signature}
    require(value.get("type") == "final" and set(value) == {"type", "story", "applied_theories"},
            "Invalid final envelope.", 502, "model_protocol")
    story, declarations = value["story"], value["applied_theories"]
    require(isinstance(story, str) and bool(story.strip()) and isinstance(declarations, list),
            "Empty story or invalid applied_theories.", 502, "model_protocol")
    require(not re.search(r"</?(?:think|thinking|analysis|reasoning)\b|EODHCTSiqsxajs Cognitive Matrix", story, re.I),
            "Reasoning tags or proxy panels are not allowed in story.", 502, "reasoning_in_story")
    require(len(declarations) <= 12, "Too many declared lenses.", 502, "model_protocol")
    normalized, used_keys = [], set()
    for declaration in declarations:
        require(isinstance(declaration, dict) and set(declaration) == {"name", "source_ids"},
                "Invalid theory declaration.", 502, "model_protocol")
        try:
            name = bounded_string(declaration["name"], 160, "theory.name")
        except Failure as exc:
            raise Failure(502, "model_protocol", exc.message) from exc
        key, _, digest = theory_identity(name)
        require(key not in used_keys, "Duplicate normalized theory name.", 502, "duplicate_declaration")
        used_keys.add(key)
        require(not cfg.theory_limit or counts.get(key, 0) < cfg.theory_limit,
                "Declared theory has reached the configured limit.", 502, "theory_limit")
        ids = declaration["source_ids"]
        require(isinstance(ids, list) and len(ids) <= 12
                and all(isinstance(sid, str) and sid in read_sources for sid in ids)
                and (bool(ids) or key in counts),
                "New lenses must cite pages read in this generation.", 502, "ungrounded_declaration")
        normalized.append({"name": name, "source_ids": list(dict.fromkeys(ids)), "theory_hash": digest})
    return {"type": "final", "story": story.strip(), "declarations": declarations,
            "normalized_declarations": normalized}


async def run_turn(request, data, session, generation, fingerprint):
    state, cfg = request.app.state, request.app.state.cfg
    messages = messages_from(data, state.legacy_blocks)
    settings, ignored = settings_from(data, cfg)
    counts, state_version = state.store.state(session)
    messages += [{"role": "system", "content": state.prompt}]
    sources, read_sources, seen, usages, refs = {}, {}, set(), [], []
    repaired, normalizations = False, []
    for step in range(cfg.calls):
        require(not await request.is_disconnected(), "Client disconnected.", 499, "cancelled")
        request.state.model_call = step + 1
        final_only = step == cfg.calls - 1
        control = {"final_only": final_only, "call": step + 1, "maximum_calls": cfg.calls,
                   "counts": counts, "maximum_uses": cfg.theory_limit,
                   "output_format": "one complete JSON object; narrative belongs inside the story string",
                   "read_source_ids": list(read_sources),
                   "state_mode": "explicit_session" if session else "stateless"}
        dispatched = messages + [{"role": "system", "content": "WIKI_CONTROL " + compact(control)}]
        payload = {**settings, "messages": dispatched}
        # This byte-based admission heuristic intentionally overcounts input tokens.
        request_bytes = len(compact(payload).encode("utf-8"))
        margin = 4096
        estimated = request_bytes + margin + settings["max_tokens"]
        if estimated > cfg.context:
            details = {"call": step + 1, "request_utf8_bytes": request_bytes,
                       "safety_margin": margin, "output_reservation": settings["max_tokens"],
                       "estimated_units": estimated, "budget": cfg.context}
            state.store.event(session, generation, "context_budget_exceeded", **details)
            diagnostic(request, "context_budget_exceeded", **details)
            raise Failure(413, "context_budget",
                          f"Local proxy context guard exceeded: {request_bytes} request UTF-8 bytes + "
                          f"{margin} margin + {settings['max_tokens']} output reservation = {estimated} "
                          f"> WIKI_CONTEXT_BUDGET={cfg.context}. This is a byte-based estimate, not a "
                          "measured model token count. Adjust WIKI_CONTEXT_BUDGET in Launch-ReAct.bat "
                          "and restart the proxy, or shorten the request.")
        for sid, reference in read_sources.items():
            state.store.event(session, generation, "injected", call=step + 1, source_id=sid,
                              excerpt_sha256=reference["excerpt_sha256"], delivery="dispatch_attempt")
        diagnostic(request, "model_call_started", call=step + 1, final_only=final_only,
                   request_utf8_bytes=request_bytes, estimated_units=estimated,
                   context_budget=cfg.context, max_tokens=settings["max_tokens"], response_format="json_object")
        response = await state.net.json("POST", DEEPSEEK + "/chat/completions", payload=payload, model=True)
        choices = response.get("choices")
        require(isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict),
                "Invalid model choices.", 502, "upstream_schema")
        choice = choices[0]
        finish, message = choice.get("finish_reason"), choice.get("message")
        require(isinstance(message, dict) and not message.get("tool_calls"),
                "Model returned unsupported content.", 502, "upstream_schema")
        content, reasoning, usage = message.get("content"), message.get("reasoning_content"), response.get("usage")
        require(content is None or isinstance(content, str), "Invalid public content type.", 502, "upstream_schema")
        usage_counts = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if type(usage.get(k)) is int} if isinstance(usage, dict) else {}
        reply_details = {"call": step + 1, "finish": finish if isinstance(finish, str) else None,
                         "content_chars": len(content) if isinstance(content, str) else 0,
                         "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
                         "usage": usage_counts}
        diagnostic(request, "model_reply", **reply_details)
        state.store.event(session, generation, "model_reply", **reply_details)
        usages.append(usage)
        require(finish in ("stop", "length"), "Model did not produce a supported completion.", 502, "upstream_finish")
        try:
            require(finish != "length", "Model reached its output limit; no state committed.",
                    502, "upstream_truncated")
            value, normalization = parse_reply(content or "")
            validated = validate_reply(value, sources, read_sources, counts, cfg, final_only, seen)
        except Failure as exc:
            details = {"call": step + 1, "code": exc.code, "finish": finish,
                       "content_chars": len(content) if isinstance(content, str) else 0,
                       "blank_content": not bool(content and content.strip())}
            state.store.event(session, generation, "protocol_failure", **details)
            diagnostic(request, "protocol_failure", **details)
            if finish == "stop" and not repaired and not final_only:
                # Exactly one corrective generation within the existing total call budget.
                # Public rejected content is supplied for correction; native reasoning is not replayed.
                repaired = True
                state.store.event(session, generation, "repair", call=step + 1, code=exc.code)
                diagnostic(request, "repair", call=step + 1, code=exc.code)
                if content and content.strip():
                    messages.append({"role": "assistant", "content": content})
                messages.append({"role": "system", "content": "WIKI_REPAIR " + compact({
                    "reason": exc.code, "detail": exc.message,
                    "instruction": ("Correct the immediately preceding public response into exactly one JSON object. "
                                    "Earlier narrative-only and continuation instructions apply solely to the story string. "
                                    "If the rejected response is narrative prose, encode that narrative inside story; do not "
                                    "start a different scene. Remove any private planning from the story. If it is a research "
                                    "action, use the allowed action schema and valid arguments. Do not invent sources or "
                                    "claims of retrieval. With no grounded declarations, use applied_theories=[]. No XML "
                                    "tags, Markdown fences, commentary, or unescaped newlines outside JSON strings. "
                                    "Obey the latest WIKI_CONTROL, including final_only."),
                    "final_example": {"type": "final", "story": "The narrative continuation.", "applied_theories": []},
                    "search_example": {"type": "tool_call", "name": "SEARCH_WIKI",
                                       "arguments": {"query": "Information asymmetry"}},
                    "read_example": {"type": "tool_call", "name": "READ_WIKI_PAGE",
                                     "arguments": {"source_id": "r1"}}})})
                continue
            raise
        if normalization != "none":
            normalizations.append({"call": step + 1, "wrapper": normalization})
            state.store.event(session, generation, "envelope_normalized", call=step + 1, wrapper=normalization)
            diagnostic(request, "envelope_normalized", call=step + 1, wrapper=normalization)
        if validated["type"] == "tool_call":
            name, argument = validated["name"], validated["argument"]
            seen.add(validated["signature"])
            diagnostic(request, "tool_started", call=step + 1, tool=name)
            try:
                if name == "SEARCH_WIKI":
                    rows, hit = await state.retriever.search(argument)
                    found = []
                    for row in rows:
                        sid = "r" + str(len(sources) + 1)
                        sources[sid] = row
                        found.append({"source_id": sid, **row})
                    observation = {"results": found, "cache_hit": hit}
                else:
                    sid = argument
                    reference, hit = await state.retriever.read(sources[sid])
                    read_sources[sid] = reference
                    state.store.snapshot(reference)
                    state.store.event(session, generation, "fetched", call=step + 1, source_id=sid,
                                      cache_hit=hit, **{k: v for k, v in reference.items() if k != "excerpt"})
                    observation = {"source_id": sid, **reference, "cache_hit": hit}
                    refs.append({"source_id": sid, **{k: v for k, v in reference.items() if k != "excerpt"}})
                diagnostic(request, "tool_finished", call=step + 1, tool=name, cache_hit=hit)
            except (KeyError, TypeError, ValueError, AttributeError):
                observation = {"error": {"code": "retrieval_schema", "message": "Retrieval provider returned an invalid source object."}}
                state.store.event(session, generation, "tool_failed", call=step + 1, code="retrieval_schema")
                diagnostic(request, "tool_failed", call=step + 1, tool=name, code="retrieval_schema")
            except Failure as exc:
                observation = {"error": {"code": exc.code, "message": exc.message}}
                state.store.event(session, generation, "tool_failed", call=step + 1, code=exc.code)
                diagnostic(request, "tool_failed", call=step + 1, tool=name, code=exc.code,
                           status=exc.status, upstream_status=exc.upstream_status)
            # Replay the validated envelope without its compatibility wrapper.
            messages += [{"role": "assistant", "content": compact(value)},
                         {"role": "user", "content": "WIKI_OBSERVATION " + compact(observation)}]
            continue
        result = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
                  "model": ALIAS, "choices": [{"index": 0, "message": {"role": "assistant", "content": validated["story"]},
                                             "finish_reason": finish}],
                  "wiki_proxy": {"version": VERSION, "upstream_model": response.get("model", cfg.model), "model_calls": step + 1,
                                 "ignored_request_fields": ignored, "effective_settings": settings,
                                 "references": refs, "applied_theories": validated["normalized_declarations"], "usage_per_call": usages,
                                 "usage_complete": False, "state_mode": "explicit_session" if session else "stateless",
                                 "generation_id": generation, "replayed": False, "state_committed": False,
                                 "protocol_repaired": repaired, "envelope_normalizations": normalizations,
                                 "context_estimate_method": "serialized_UTF8_bytes_plus_4096_plus_output_reservation"}}
        known = all(isinstance(u, dict) and all(type(u.get(k)) is int and u[k] >= 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")) for u in usages)
        result["wiki_proxy"]["usage_complete"] = known
        if known:
            result["usage"] = {k: sum(u[k] for u in usages) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        require(not await request.is_disconnected(), "Client disconnected before commit.", 499, "cancelled")
        # This transaction is the commit point; no await occurs before it finishes.
        result["wiki_proxy"]["state_committed"] = session is not None
        state.store.commit(session, generation, fingerprint, state_version, validated["declarations"], result)
        if session is None:
            state.store.event(session, generation, "completed", model_calls=step + 1)
        return result
    raise Failure(502, "tool_budget", "Model call budget exhausted.")


@asynccontextmanager
async def lifespan(app):
    cfg = Config.load()
    bundle = strict_json((ROOT / "tool_prompts.temp.json").read_text(encoding="utf-8"))
    require(isinstance(bundle, dict) and bundle.get("schema_version") == 2
            and isinstance(bundle.get("prompts"), list) and bool(bundle["prompts"]), "Invalid prompt bundle.", 500)
    require(all(isinstance(p, dict) and p.get("role") == "system" and isinstance(p.get("content"), str)
                and bool(p["content"]) for p in bundle["prompts"]), "Invalid prompt blocks.", 500)
    prompt = "\n\n".join(p["content"] for p in bundle["prompts"])
    legacy_blocks = []
    if os.getenv("WIKI_LEGACY_PRESET_FILE"):
        legacy_path = Path(os.environ["WIKI_LEGACY_PRESET_FILE"]).expanduser().resolve()
        require(legacy_path.stat().st_size <= 2_000_000, "Legacy preset file is too large.", 500)
        legacy = strict_json(legacy_path.read_text(encoding="utf-8"))
        legacy_blocks = [p["content"] for p in legacy["prompts"] if p.get("identifier") in
                         {"EODHCTSiqsxajs_simulation", "entity_status_tracker", "theory_continuation_protocol"}
                         and isinstance(p.get("content"), str) and p["content"]]
        require(len(legacy_blocks) == 3, "Expected exactly three legacy protocol blocks.", 500)
    guard = ProcessGuard(cfg.data_dir)
    store = None
    try:
        store = Store(cfg.data_dir)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                   limits=httpx.Limits(max_connections=16, max_keepalive_connections=8)) as client:
            app.state.cfg, app.state.prompt = cfg, prompt
            app.state.legacy_blocks = legacy_blocks
            app.state.store, app.state.sessions = store, SessionLocks()
            app.state.code_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            app.state.net = Network(client, cfg)
            app.state.retriever = Retriever(app.state.net, cfg)
            app.state.active = 0
            ready = {"version": VERSION, "run_id": store.run_id, "code_sha256": app.state.code_digest,
                     "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
            store.event(None, None, "server_started", **{k: v for k, v in ready.items() if k != "run_id"})
            print("[wiki_proxy] " + compact({"event": "proxy_ready", **ready}), file=sys.stderr, flush=True)
            yield
    finally:
        if store:
            store.db.close()
        guard.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])


@app.exception_handler(Failure)
async def handle_failure(request, exc):
    return failure_response(request, exc.status, exc.code, exc.message, upstream_status=exc.upstream_status)


@app.exception_handler(sqlite3.Error)
async def handle_storage_failure(request, exc):
    return failure_response(request, 503, "storage_unavailable",
                            "State storage is unavailable; inspect the local database before retrying.")


@app.exception_handler(TimeoutError)
async def handle_timeout(request, exc):
    return failure_response(request, 504, "deadline", "Operation deadline exceeded.")


@app.exception_handler(Exception)
async def handle_internal_error(request, exc):
    return failure_response(request, 500, "internal", "Internal proxy error.", exception_type=type(exc).__name__)


@app.get("/healthz")
async def health():
    return {"status": "ready", "version": VERSION}


@app.get("/v1/models")
async def models(request: Request):
    authorize(request)
    return {"object": "list", "data": [{"id": ALIAS, "object": "model", "created": 0, "owned_by": "local-wiki-proxy"}]}


@app.post("/v1/wiki/reset")
async def reset(request: Request):
    authorize(request)
    async with asyncio.timeout(10):
        data = await body_json(request)
        require(set(data) == {"session_id"}, "Reset requires only an explicit session_id.")
        session = bounded_string(data["session_id"], 128, "session_id")
        async with request.app.state.sessions.hold(session):
            request.app.state.store.reset(session)
    return {"reset": True, "scope": "counts_and_replay_ledger", "public_snapshots_retained": True}


async def generation(request, data):
    state, cfg = request.app.state, request.app.state.cfg
    # Validate before waiting for a session or spending resources.
    messages = messages_from(data, state.legacy_blocks)
    settings, _ = settings_from(data, cfg)
    session, gid = identity_from(request, data)
    require(session is not None or not cfg.theory_limit, "An enforced cross-turn theory limit requires an explicit session.")
    diagnostic(request, "generation_started", model=cfg.model, backend=cfg.backend,
               stream=data.get("stream", False), max_tokens=settings["max_tokens"],
               message_count=len(messages), user_messages=sum(m["role"] == "user" for m in messages),
               assistant_messages=sum(m["role"] == "assistant" for m in messages),
               last_history_role=next((m["role"] for m in reversed(messages) if m["role"] != "system"), None),
               state_mode="explicit_session" if session else "stateless")
    canonical_request = {k: v for k, v in data.items() if k not in ("stream", "stream_options", "wiki_proxy")}
    fingerprint = hashlib.sha256(compact({"request": canonical_request, "model": cfg.model,
        "backend": cfg.backend, "language": cfg.language, "calls": cfg.calls, "output": cfg.output,
        "context": cfg.context, "deadline": cfg.deadline, "excerpt": cfg.excerpt,
        "maximum_uses": cfg.theory_limit, "prompt": state.prompt, "legacy_blocks": state.legacy_blocks,
        "code_digest": state.code_digest}).encode("utf-8")).hexdigest()
    async with state.sessions.hold(session):
        cached = state.store.replay(session, gid, fingerprint)
        if cached:
            diagnostic(request, "generation_replayed")
            return cached
        try:
            return await run_turn(request, data, session, gid, fingerprint)
        except Failure as exc:
            state.store.event(session, gid, "failed", code=exc.code, status=exc.status,
                              message=exc.message, call=getattr(request.state, "model_call", None),
                              request_id=request.state.trace_id, upstream_status=exc.upstream_status)
            raise
        except asyncio.CancelledError:
            state.store.event(session, gid, "cancelled")
            diagnostic(request, "generation_cancelled", call=getattr(request.state, "model_call", None))
            raise


async def until_disconnected(request):
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.25)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    diagnostic(request, "request_received", version=VERSION)
    authorize(request)
    require(request.app.state.active < request.app.state.cfg.concurrency, "Proxy is busy; retry later.", 503, "busy")
    request.app.state.active += 1
    try:
        async with asyncio.timeout(request.app.state.cfg.deadline):
            data = await body_json(request)
            work = asyncio.create_task(generation(request, data))
            disconnect = asyncio.create_task(until_disconnected(request))
            try:
                finished, _ = await asyncio.wait({work, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                if work in finished:
                    result = work.result()
                else:
                    raise Failure(499, "cancelled", "Client disconnected before completion.")
            finally:
                for task in (work, disconnect):
                    if not task.done():
                        task.cancel()
                for task in (work, disconnect):
                    with suppress(asyncio.CancelledError, Failure, sqlite3.Error):
                        await task
    except TimeoutError as exc:
        raise Failure(504, "turn_deadline", "Absolute turn deadline exceeded.") from exc
    finally:
        request.app.state.active -= 1
    diagnostic(request, "response_ready", model_calls=result["wiki_proxy"]["model_calls"],
               story_chars=len(result["choices"][0]["message"]["content"]),
               finish=result["choices"][0]["finish_reason"], stream=data.get("stream", False))
    if not data.get("stream", False):
        return JSONResponse(result)
    async def buffered():
        base = {k: result[k] for k in ("id", "created", "model")}
        base["object"] = "chat.completion.chunk"
        yield "data: " + compact({**base, "choices": [{"index": 0, "delta": result["choices"][0]["message"], "finish_reason": None}]}) + "\n\n"
        yield "data: " + compact({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": result["choices"][0]["finish_reason"]}], "wiki_proxy": result["wiki_proxy"]}) + "\n\n"
        if data.get("stream_options", {}).get("include_usage") and "usage" in result:
            yield "data: " + compact({**base, "choices": [], "usage": result["usage"]}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(buffered(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def verify_database(path):
    """Read-only verifier: independently serialize stored identity payloads and hash BLOBs."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    totals = {"identity_match": 0, "identity_mismatch": 0, "excerpt_match": 0,
              "excerpt_mismatch": 0, "missing_snapshot": 0, "revision_provenance": "unknown"}
    try:
        for payload, expected in db.execute("SELECT identity_json,theory_hash FROM counts"):
            value = json.loads(payload)
            exact = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            ok = hashlib.sha256(exact).hexdigest() == expected
            totals["identity_match" if ok else "identity_mismatch"] += 1
        for expected, blob in db.execute("SELECT digest,excerpt FROM snapshots"):
            ok = hashlib.sha256(bytes(blob)).hexdigest() == expected
            totals["excerpt_match" if ok else "excerpt_mismatch"] += 1
        for event, in db.execute("SELECT event_json FROM events"):
            value = json.loads(event)
            if value.get("event") == "injected":
                if not db.execute("SELECT 1 FROM snapshots WHERE digest=?", (value["excerpt_sha256"],)).fetchone():
                    totals["missing_snapshot"] += 1
    finally:
        db.close()
    print(json.dumps(totals, sort_keys=True))
    return int(any(totals[k] for k in ("identity_mismatch", "excerpt_mismatch", "missing_snapshot")))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--verify":
    raise SystemExit(verify_database(sys.argv[2]))


if __name__ == "__main__":
    import uvicorn
    try:
        configuration = Config.load()
    except Failure as error:
        raise SystemExit(error.message)
    uvicorn.run(app, host="127.0.0.1", port=configuration.port, workers=1, access_log=False)
