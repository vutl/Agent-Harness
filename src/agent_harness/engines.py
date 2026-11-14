"""Model calls through the vendor CLIs: ``codex exec`` and ``claude -p``.

Codex accounts rotate least-recently-used and cool down on usage limits; when
an engine is out of quota the role's ``fallback`` (usually the other engine)
takes over. Claude roles may run through an Anthropic-compatible gateway.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

DEFAULT_COOLDOWN_SECONDS = 20 * 60
NO_CREDITS_COOLDOWN_SECONDS = 6 * 3600
AUTH_COOLDOWN_SECONDS = 6 * 3600

_RATE_LIMIT_RE = re.compile(
    r"usage limit|rate[\s_-]?limit|too many requests|\b429\b|session limit|quota"
    r"|out of credits|requires usage credits|purchase more credits",
    re.IGNORECASE,
)
_NO_CREDITS_RE = re.compile(r"out of credits|requires usage credits", re.IGNORECASE)
_RESET_RE = re.compile(
    r"(?:reset|resets|try again)\s+(?:after|in)\s+(?:(\d+)\s*h(?:ours?)?\s*)?(?:(\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE,
)
_RESET_AT_RE = re.compile(
    r"try again at\s+([A-Z][a-z]{2})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*([AP]M)",
)
# A revoked or expired login: park the account for hours instead of retrying.
_AUTH_ERROR_RE = re.compile(r"token_revoked|invalidated oauth token|401 Unauthorized|refresh token", re.IGNORECASE)
_lock = threading.Lock()

_BUNDLED = {
    "codex": [
        "~/.vscode/extensions/openai.chatgpt-*/bin/*/codex",
        "~/.cursor/extensions/openai.chatgpt-*/bin/*/codex",
    ],
    "claude": [
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
        "~/.cursor/extensions/anthropic.claude-code-*/resources/native-binary/claude",
        "~/.claude/local/claude",
    ],
}


class QuotaExhausted(RuntimeError):
    """Every account for an engine is cooling down, or a gateway failed."""


@dataclass
class CallResult:
    ok: bool
    text: str
    role: str
    model: str
    account: str | None
    seconds: float
    log_path: Path | None
    rate_limited: bool = False


def binary(settings: Settings, name: str) -> str:
    configured = str(settings.section("binaries").get(name, "auto"))
    if configured != "auto":
        return os.path.expanduser(configured)
    found = shutil.which(name)
    if found:
        return found
    matches = [path for pattern in _BUNDLED.get(name, []) for path in glob.glob(os.path.expanduser(pattern))]
    if not matches:
        raise FileNotFoundError(f"{name} CLI not found on PATH or in editor extensions; install it or set "
                                f"[binaries] {name} = \"/path/to/{name}\"")
    return max(matches, key=os.path.getmtime)


def accounts(settings: Settings) -> list[dict[str, str]]:
    """Configured Codex accounts that are logged in (default: $CODEX_HOME or ~/.codex)."""
    configured = settings.data.get("accounts") or [
        {"name": "default", "codex_home": os.environ.get("CODEX_HOME") or "~/.codex"}]
    result = []
    for account in configured:
        home = os.path.expanduser(account["codex_home"])
        if Path(home, "auth.json").exists():
            result.append({"name": account["name"], "codex_home": home})
    return result


# --- shared state (cooldowns, call log) -------------------------------------

def _state_file(settings: Settings) -> Path:
    return settings.user_state_dir / "accounts.json"


def read_state(settings: Settings) -> dict[str, Any]:
    try:
        return json.loads(_state_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _update_state(settings: Settings, mutator) -> dict[str, Any]:
    directory = settings.user_state_dir
    directory.mkdir(parents=True, exist_ok=True)
    with _lock, open(directory / "accounts.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(settings)
        mutator(state)
        _state_file(settings).write_text(json.dumps(state, indent=1), encoding="utf-8")
        return state


def call_log_path(settings: Settings) -> Path:
    return settings.user_state_dir / "calls.jsonl"


def _log_call(settings: Settings, entry: dict[str, Any]) -> None:
    settings.user_state_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "project": settings.root.name if settings.root else None, **entry}
    with _lock, open(call_log_path(settings), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def pick_account(settings: Settings) -> dict[str, str]:
    """Least recently used account that is not cooling down; reserves it."""
    chosen: dict[str, str] = {}
    candidates = accounts(settings)
    if not candidates:
        raise QuotaExhausted("no logged-in Codex account (run `codex login`, or set CODEX_HOME per [[accounts]])")

    def reserve(state: dict[str, Any]) -> None:
        now = time.time()
        usable = [a for a in candidates if state.get(a["name"], {}).get("cooldown_until", 0) <= now]
        if not usable:
            return
        usable.sort(key=lambda a: state.get(a["name"], {}).get("last_used", 0))
        chosen.update(usable[0])
        state.setdefault(chosen["name"], {})["last_used"] = now

    state = _update_state(settings, reserve)
    if not chosen:
        wake = min(state.get(a["name"], {}).get("cooldown_until", 0) for a in candidates)
        raise QuotaExhausted(f"all Codex accounts cooling down until {time.ctime(wake)}")
    return chosen


def cooldown_seconds(text: str) -> int:
    """Seconds until a limit resets: "reset after 28m", "try again in 2h 5m",
    "try again at Oct 18th, 2026 3:42 AM" (local time)."""
    match = _RESET_RE.search(text or "")
    if match and (match.group(1) or match.group(2)):
        return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + 30
    if _NO_CREDITS_RE.search(text or ""):
        return NO_CREDITS_COOLDOWN_SECONDS
    at = _RESET_AT_RE.search(text or "")
    if at:
        month, day, year, hour, minute, half = at.groups()
        stamp = time.strptime(f"{month} {day} {year} {hour}:{minute} {half}", "%b %d %Y %I:%M %p")
        return max(60, int(time.mktime(stamp) - time.time()) + 30)
    return DEFAULT_COOLDOWN_SECONDS


def mark_cooldown(settings: Settings, account_name: str, text: str, seconds: int | None = None) -> None:
    seconds = seconds or cooldown_seconds(text)

    def apply(state: dict[str, Any]) -> None:
        state.setdefault(account_name, {})["cooldown_until"] = time.time() + seconds

    _update_state(settings, apply)


# --- codex -------------------------------------------------------------------

def _project_mcp_servers(settings: Settings) -> list[str]:
    import tomllib

    if settings.root is None:
        return []
    try:
        with open(settings.root / ".codex" / "config.toml", "rb") as handle:
            return sorted((tomllib.load(handle).get("mcp_servers") or {}).keys())
    except (OSError, tomllib.TOMLDecodeError):
        return []


def codex_command(settings: Settings, config: dict[str, Any], *, cwd: Path, out_file: Path,
                  schema: Path | None, add_dirs: list[Path]) -> list[str]:
    command = [binary(settings, "codex")]
    if config.get("web"):
        command.append("--search")  # a global flag: it must precede `exec`
    sandbox = config.get("sandbox", "read-only")
    command += [
        "exec", "-m", config["model"], "-s", sandbox,
        "-c", f'model_reasoning_effort="{config.get("effort", "medium")}"',
        "-C", str(cwd), "-o", str(out_file), "--skip-git-repo-check",
    ]
    if sandbox == "read-only":
        command.append("--ephemeral")
    if settings.section("codex").get("ignore_user_config", True):
        command.append("--ignore-user-config")
    allowed = set(config.get("mcp") or [])
    for server in _project_mcp_servers(settings):
        if server not in allowed:
            command += ["-c", f'mcp_servers.{server}={{ command = "true", enabled = false }}']
    if schema:
        command += ["--output-schema", str(schema)]
    for directory in add_dirs:
        command += ["--add-dir", str(directory)]
    command.append("-")
    return command


def run_codex(settings: Settings, role: str, prompt: str, *, cwd: Path, log_dir: Path,
              schema: Path | None = None, timeout: int = 3600, wait_for_quota: bool = True,
              add_dirs: list[Path] | None = None) -> CallResult:
    config = settings.role(role)
    log_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            account = pick_account(settings)
        except QuotaExhausted:
            if not wait_for_quota:
                raise
            time.sleep(120)
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_file = log_dir / f"{role}-{stamp}-{account['name']}.last.md"
        log_path = log_dir / f"{role}-{stamp}-{account['name']}.log"
        command = codex_command(settings, config, cwd=cwd, out_file=out_file, schema=schema, add_dirs=add_dirs or [])
        started = time.time()
        env = {**os.environ, "CODEX_HOME": account["codex_home"]}
        try:
            completed = subprocess.run(command, input=prompt, text=True, capture_output=True, env=env, timeout=timeout)
            output, code = (completed.stdout or "") + "\n" + (completed.stderr or ""), completed.returncode
        except subprocess.TimeoutExpired as exc:
            output, code = f"TIMEOUT after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}", -1
        log_path.write_text(output, encoding="utf-8")
        text = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
        seconds = round(time.time() - started, 1)
        limited = code != 0 and bool(_RATE_LIMIT_RE.search(output))
        auth_failed = not text.strip() and bool(_AUTH_ERROR_RE.search(output))
        _log_call(settings, {"role": role, "engine": "codex", "model": config["model"], "account": account["name"],
                             "seconds": seconds, "ok": code == 0 and bool(text.strip()),
                             "rate_limited": limited, "auth_failed": auth_failed})
        if auth_failed:
            mark_cooldown(settings, account["name"], output, AUTH_COOLDOWN_SECONDS)
            continue
        if limited:
            mark_cooldown(settings, account["name"], output)
            continue
        return CallResult(ok=code == 0 and bool(text.strip()), text=text, role=role, model=config["model"],
                          account=account["name"], seconds=seconds, log_path=log_path)


# --- claude ------------------------------------------------------------------

def _read_key_file(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def gateway_env(settings: Settings, name: str) -> dict[str, str]:
    """Environment for ``claude -p`` through an Anthropic-compatible gateway."""
    gateway = (settings.data.get("gateways") or {}).get(name)
    if not gateway:
        raise QuotaExhausted(f"unknown gateway {name!r}")
    token = os.environ.get(gateway.get("token_env", ""), "")
    if not token and gateway.get("token_file"):
        path = Path(os.path.expanduser(gateway["token_file"]))
        if not path.is_absolute() and settings.root is not None:
            path = settings.root / path
        token = _read_key_file(path, gateway.get("token_key", ""))
    env = {key: value for key, value in os.environ.items() if not key.startswith("ANTHROPIC_")}
    env.update({"ANTHROPIC_BASE_URL": gateway["base_url"], "ANTHROPIC_AUTH_TOKEN": token,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    return env


def claude_command(settings: Settings, config: dict[str, Any]) -> list[str]:
    command = [binary(settings, "claude"), "-p", "--model", config["model"],
               "--permission-mode", config.get("permission_mode", "plan"), "--output-format", "text"]
    tool_sets = settings.section("claude_tool_sets")
    allowed = list(tool_sets.get(config["tool_set"], [])) if config.get("tool_set") else []
    if config.get("web"):
        allowed += tool_sets.get("web", ["WebSearch", "WebFetch"])
    if allowed:
        command += ["--allowedTools", ",".join(dict.fromkeys(allowed))]
    return command


def run_claude(settings: Settings, role: str, prompt: str, *, cwd: Path, log_dir: Path,
               timeout: int = 3600) -> CallResult:
    """Claude has one login: no account failover. A gateway role raises
    QuotaExhausted on any failure so its fallback runs."""
    config = settings.role(role)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{role}-{time.strftime('%Y%m%d-%H%M%S')}-claude.log"
    command = claude_command(settings, config)
    gateway = config.get("gateway")
    account = f"gateway:{gateway}" if gateway else "claude"
    env = gateway_env(settings, gateway) if gateway else None
    started = time.time()
    try:
        completed = subprocess.run(command, input=prompt, text=True, capture_output=True, cwd=cwd,
                                   timeout=timeout, env=env)
        text, err, code = completed.stdout or "", completed.stderr or "", completed.returncode
    except subprocess.TimeoutExpired as exc:
        text, err, code = str(exc.stdout or ""), f"TIMEOUT after {timeout}s", -1
    log_path.write_text(f"{err}\n---\n{text}", encoding="utf-8")
    seconds = round(time.time() - started, 1)
    # Some limits arrive as a normal short reply with exit code 0.
    limited = bool(_RATE_LIMIT_RE.search(err + text)) and (code != 0 or len(text) < 400)
    ok = code == 0 and bool(text.strip())
    _log_call(settings, {"role": role, "engine": "claude", "model": config["model"], "account": account,
                         "seconds": seconds, "ok": ok, "rate_limited": limited})
    if limited or (gateway and not ok):
        raise QuotaExhausted(f"{account} {config['model']}: {(err or text)[:200]}")
    return CallResult(ok=ok, text=text, role=role, model=config["model"], account=account,
                      seconds=seconds, log_path=log_path, rate_limited=limited)


# --- roles -------------------------------------------------------------------

def extract_json(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def run_role(settings: Settings, role: str, prompt: str, *, cwd: Path | None = None, log_dir: Path | None = None,
             schema: Path | None = None, timeout: int = 3600, wait_for_quota: bool = True,
             add_dirs: list[Path] | None = None, _seen: tuple[str, ...] = ()) -> CallResult:
    """Run ``role``; when its engine is out of quota, its ``fallback`` takes over."""
    config = settings.role(role)
    fallback = config.get("fallback")
    if fallback in _seen:
        fallback = None
    cwd = Path(cwd or settings.root or Path.cwd())
    log_dir = Path(log_dir or settings.state_dir / "logs")
    try:
        if config.get("engine", "codex") == "claude":
            if schema:
                prompt += ("\n\nReturn only a JSON object matching this JSON Schema, no prose:\n"
                           + Path(schema).read_text(encoding="utf-8"))
            result = run_claude(settings, role, prompt, cwd=cwd, log_dir=log_dir, timeout=timeout)
            if schema:
                result.text = extract_json(result.text)
            return result
        return run_codex(settings, role, prompt, cwd=cwd, log_dir=log_dir, schema=schema, timeout=timeout,
                         wait_for_quota=wait_for_quota and not fallback, add_dirs=add_dirs)
    except (QuotaExhausted, FileNotFoundError):
        if not fallback:
            raise
        return run_role(settings, fallback, prompt, cwd=cwd, log_dir=log_dir, schema=schema, timeout=timeout,
                        wait_for_quota=wait_for_quota, add_dirs=add_dirs, _seen=(*_seen, role))
