"""Where things live: user, group, /work roots, the project directory and the site commands.

Nothing is written under $HOME. Project outputs go to <project>/.miyabi-mcp/; the few user-level
files go to /work/<group>/<user>/.miyabi-mcp/ (or $MIYABI_MCP_HOME).
"""

from __future__ import annotations

import getpass
import grp
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .util import MiyabiError, read_json, write_json

STATE_NAME = ".miyabi-mcp"
LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "miyabi-mcp"
DEFAULT_COMMANDS = {
    "qstat": "/usr/local/bin/qstat",
    "qsub": "/opt/pbs/bin/qsub",
    "qdel": "/usr/local/bin/qdel",
    "show_token": "/usr/local/bin/show_token",
    "bash": "/bin/bash",
    "git": "/usr/bin/git",
}
DEFAULT_CONFIG = {
    "remote_alias": "miyabi-g",
    "poll_seconds": 60,
    "interactive_max_age_s": 15,
    "job_wait_max_s": 45,
    "submit_grace_s": 120,
    "missing_s": 300,
    "deliver_commands": {},
}


@dataclass(frozen=True)
class Settings:
    user: str
    group: str | None
    work_root: Path | None
    user_dir: Path
    project_dir: Path | None
    project_source: str
    config: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)

    @property
    def state_dir(self) -> Path | None:
        return self.project_dir / STATE_NAME if self.project_dir else None

    def require_project(self) -> Path:
        """The project's state directory, created on first use. Fails when no project is set."""
        if self.project_dir is None:
            raise MiyabiError("no_project", "no project directory is set: pass project, start the server with "
                              "--project/--project-dir, or set MIYABI_MCP_PROJECT_DIR")
        if not self.project_dir.is_dir():
            raise MiyabiError("no_project", f"project directory {self.project_dir} does not exist")
        ensure_state_dir(self.project_dir)
        return self.project_dir / STATE_NAME

    def with_project(self, project: str | None) -> "Settings":
        if not project:
            return self
        return replace(self, project_dir=expand_project(project, self.work_root), project_source="argument")

    def command(self, name: str) -> str:
        return self.commands[name]


def ensure_state_dir(project_dir: Path) -> Path:
    state = project_dir / STATE_NAME
    if not state.is_dir():
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(state, 0o700)
    ignore = state / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    if not (state / "project.json").exists():
        write_json(state / "project.json", {"schema_version": 1, "project_dir": str(project_dir)})
    return state


def expand_project(value: str, work_root: Path | None) -> Path:
    if "/" in value:
        return Path(value).expanduser().resolve()
    if work_root is None:
        raise MiyabiError("no_project", f"cannot expand project name {value!r}: /work/<group>/<user> is unknown; "
                          "pass --project-dir or set --group")
    return work_root / value


def work_base(env: Mapping[str, str]) -> Path:
    return Path(env.get("MIYABI_MCP_WORK", "/work"))


def detect_group(user: str, explicit: str | None, env: Mapping[str, str]) -> str | None:
    group = explicit or env.get("MIYABI_MCP_GROUP")
    if group:
        return group
    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    candidates = sorted(name for name in names if (work_base(env) / name / user).is_dir())
    return candidates[0] if len(candidates) == 1 else None


def project_from_cwd(cwd: Path, work_root: Path | None, git: str) -> Path | None:
    """The main repository of a git checkout or worktree, else /work/<group>/<user>/<name> above cwd."""
    if work_root is None:
        return None
    try:
        result = subprocess.run([git, "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip())
            top = common.parent if common.name == ".git" else None
            if top and top.is_relative_to(work_root) and top != work_root:
                return top
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        relative = cwd.resolve().relative_to(work_root)
    except ValueError:
        return None
    return work_root / relative.parts[0] if relative.parts and not relative.parts[0].startswith(".") else None


def resolve_commands(config: dict, env: Mapping[str, str]) -> dict:
    commands = {}
    override_dir = env.get("MIYABI_MCP_PBS_BIN")
    for name, default in DEFAULT_COMMANDS.items():
        configured = (config.get("commands") or {}).get(name)
        if override_dir and (Path(override_dir) / name).exists():
            commands[name] = str(Path(override_dir) / name)
        elif configured:
            commands[name] = configured
        elif Path(default).exists():
            commands[name] = default
        else:
            commands[name] = shutil.which(name) or default
    return commands


def load(project_dir: str | None = None, project: str | None = None, group: str | None = None,
         cwd: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    user = env.get("USER") or getpass.getuser()
    group = detect_group(user, group, env)
    work_root = work_base(env) / group / user if group else None
    home = env.get("MIYABI_MCP_HOME")
    if home:
        user_dir = Path(home)
    elif work_root is not None:
        user_dir = work_root / STATE_NAME
    else:
        raise MiyabiError("no_group", "cannot find /work/<group>/<user>: pass --group, set MIYABI_MCP_GROUP "
                          "or MIYABI_MCP_HOME")
    config = {**DEFAULT_CONFIG, **(read_json(user_dir / "config.json", {}) or {})}
    commands = resolve_commands(config, env)
    source = "none"
    resolved: Path | None = None
    if project_dir:
        resolved, source = Path(project_dir).expanduser().resolve(), "option"
    elif project:
        resolved, source = expand_project(project, work_root), "option"
    elif env.get("MIYABI_MCP_PROJECT_DIR"):
        resolved, source = Path(env["MIYABI_MCP_PROJECT_DIR"]).expanduser().resolve(), "environment"
    else:
        found = project_from_cwd(cwd or Path.cwd(), work_root, commands["git"])
        if found is not None:
            resolved, source = found, "cwd"
    return Settings(user=user, group=group, work_root=work_root, user_dir=user_dir, project_dir=resolved,
                    project_source=source, config=config, commands=commands)


def describe(settings: Settings) -> dict[str, Any]:
    return {
        "user": settings.user, "group": settings.group,
        "work_root": str(settings.work_root) if settings.work_root else None,
        "user_dir": str(settings.user_dir),
        "project_dir": str(settings.project_dir) if settings.project_dir else None,
        "project_source": settings.project_source,
        "state_dir": str(settings.state_dir) if settings.state_dir else None,
        "commands": settings.commands,
    }
