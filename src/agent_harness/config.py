"""Layered configuration: package defaults < user config < project config.

- package: ``agent_harness/defaults.toml``
- user (this machine: accounts, gateways, model choices):
  ``$AGENT_HARNESS_HOME`` or ``$XDG_CONFIG_HOME/agent-harness`` or ``~/.config/agent-harness``
- project (committed with the repository: rules, tests, safety, git policy):
  ``<repo>/.agent-harness/config.toml``
"""

from __future__ import annotations

import copy
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ".agent-harness"


class ConfigError(RuntimeError):
    pass


def user_dir() -> Path:
    if os.environ.get("AGENT_HARNESS_HOME"):
        return Path(os.environ["AGENT_HARNESS_HOME"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent-harness"


def project_root(start: Path | None = None) -> Path | None:
    """Main checkout of the repository containing ``start`` (also from inside a worktree)."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=start or Path.cwd(), check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None
    common_path = Path(common)
    if common_path.name == ".git":
        return common_path.parent
    # Bare repository or unusual layout: fall back to the current top level.
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start or Path.cwd(),
                         capture_output=True, text=True).stdout.strip()
    return Path(top) if top else None


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge; tables merge key by key, everything else (lists too) replaces."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


@dataclass
class Settings:
    data: dict[str, Any]
    root: Path | None
    sources: list[Path] = field(default_factory=list)

    @property
    def state_dir(self) -> Path:
        """Per-project runs, logs and worktrees (git-ignored)."""
        if self.root is None:
            return user_dir() / "state" / "no-project"
        return self.root / PROJECT_DIR / "state"

    @property
    def user_state_dir(self) -> Path:
        """Account cooldowns and the call log: quota is per account, shared by projects."""
        return user_dir() / "state"

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name) or {})

    def role(self, name: str) -> dict[str, Any]:
        roles = self.data.get("roles") or {}
        if name not in roles:
            raise ConfigError(f"unknown role {name!r}; known: {', '.join(sorted(roles))}")
        return dict(roles[name])

    def protect(self) -> list[str]:
        safety = self.section("safety")
        return list(safety.get("protect") or []) + list(safety.get("protect_extra") or [])

    def rules_text(self) -> str:
        """Project rules for prompts: the files to read first plus inline notes."""
        project = self.section("project")
        lines = []
        if self.root is not None:
            files = [name for name in project.get("rules") or [] if (self.root / name).is_file()]
            if files:
                lines.append("Read these project rule files first: " + ", ".join(files) + ".")
        if project.get("notes"):
            lines.append(str(project["notes"]).strip())
        return "\n".join(lines) or "No project-specific rules were configured."

    @property
    def project_name(self) -> str:
        name = self.section("project").get("name")
        return str(name or (self.root.name if self.root else "this repository"))


def load(start: Path | None = None) -> Settings:
    root = project_root(start)
    paths = [PACKAGE_DIR / "defaults.toml", user_dir() / "config.toml"]
    if root is not None:
        paths.append(root / PROJECT_DIR / "config.toml")
    data: dict[str, Any] = {}
    used = []
    for path in paths:
        layer = _load(path)
        if layer:
            used.append(path)
        data = merge(data, layer)
    return Settings(data=data, root=root, sources=used)
