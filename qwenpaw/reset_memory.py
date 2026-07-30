#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QwenPaw native memory reset — wipe ReMe Light memory subdirs between runs.

Locates the agent workspace and removes the ReMe Light memory subdirectories
(memory/ digest/ mem_metadata/ mem_session/ mem_agent/ resource/) so each
bench case starts from a clean memory state.

Safety: only deletes directories located UNDER the resolved WORKING_DIR.

Two modes:
  1. Host mode (default): delete on the host filesystem. Point --working-dir at
     the WORKING_DIR (e.g. a bind mount, or a docker volume path such as
     /var/lib/docker/volumes/qwenpaw-data/_data).
  2. Docker mode (--docker CONTAINER): delete inside a running container via
     `docker exec`, using --container-working-dir (default /app/working). Use
     this when WORKING_DIR lives in a named volume not easily reachable on host.

Examples:
  # Host mode against a docker named volume (run as root):
  python3 reset_memory.py --agent sccs-t1-base \
    --working-dir /var/lib/docker/volumes/qwenpaw-data/_data

  # Docker mode (no host path needed):
  python3 reset_memory.py --agent sccs-t1-base --docker qwenpaw
"""
import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


# ReMe Light memory subdir defaults (config.py ReMeLightMemoryConfig).
DEFAULT_MEMORY_SUBDIRS = {
    "daily_dir": "memory",
    "digest_dir": "digest",
    "metadata_dir": "mem_metadata",
    "session_dir": "mem_session",
    "mem_session_dir": "mem_agent",
    "resource_dir": "resource",
}

# Default WORKING_DIR inside the QwenPaw docker image (see docker volume mount
# qwenpaw-data -> /app/working).
DEFAULT_CONTAINER_WORKING_DIR = "/app/working"


def resolve_working_dir(explicit: Optional[str] = None) -> Path:
    """Resolve QwenPaw WORKING_DIR.

    Priority:
      1. explicit --working-dir
      2. QWENPAW_WORKING_DIR / COPAW_WORKING_DIR env var
      3. ~/.copaw (legacy) if it exists
      4. ~/.qwenpaw
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    for env_var in ("QWENPAW_WORKING_DIR", "COPAW_WORKING_DIR"):
        val = os.environ.get(env_var)
        if val:
            return Path(val).expanduser().resolve()
    legacy = Path("~/.copaw").expanduser()
    if legacy.exists():
        return legacy.resolve()
    return Path("~/.qwenpaw").expanduser().resolve()


def resolve_agent_workspace(working_dir: Path, agent: str) -> Path:
    """Resolve the agent workspace dir.

    Default layout: <WORKING_DIR>/workspaces/<agent>.
    """
    return (working_dir / "workspaces" / agent).resolve()


def load_memory_subdir_names(workspace_dir: Path) -> Dict[str, str]:
    """Read subdir overrides from agent.json, fall back to defaults."""
    names = dict(DEFAULT_MEMORY_SUBDIRS)
    agent_json = workspace_dir / "agent.json"
    if not agent_json.exists():
        return names
    try:
        with open(agent_json, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        reme_cfg = (
            cfg.get("running", {})
            .get("reme_light_memory_config", {})
        )
        for key in names:
            if key in reme_cfg and isinstance(reme_cfg[key], str) and reme_cfg[key].strip():
                names[key] = reme_cfg[key].strip()
    except Exception as e:
        print(f"[reset-memory] WARN: failed to read agent.json ({e}); using defaults", file=sys.stderr)
    return names


def ensure_within(target: Path, base: Path) -> None:
    """Raise if target is not under base (path-traversal safety)."""
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        raise ValueError(
            f"Refusing to delete '{target}': not under WORKING_DIR '{base}'"
        )


def _docker_exec(container: str, argv: List[str], dry_run: bool = False) -> subprocess.CompletedProcess:
    """Run a command inside the container via `docker exec`."""
    cmd = ["docker", "exec", container] + argv
    if dry_run:
        print(f"[reset-memory] (dry-run) would run: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def load_memory_subdir_names_docker(
    container: str, container_working_dir: str, agent: str
) -> Dict[str, str]:
    """Read agent.json from inside the container, fall back to defaults."""
    names = dict(DEFAULT_MEMORY_SUBDIRS)
    agent_json = posixpath.join(container_working_dir, "workspaces", agent, "agent.json")
    proc = _docker_exec(container, ["cat", agent_json])
    if proc.returncode != 0:
        return names
    try:
        cfg = json.loads(proc.stdout)
        reme_cfg = cfg.get("running", {}).get("reme_light_memory_config", {})
        for key in names:
            val = reme_cfg.get(key)
            if isinstance(val, str) and val.strip():
                names[key] = val.strip()
    except Exception as e:
        print(f"[reset-memory] WARN: failed to parse agent.json in container ({e}); using defaults", file=sys.stderr)
    return names


def reset_memory_docker(
    agent: str,
    container: str,
    container_working_dir: str = DEFAULT_CONTAINER_WORKING_DIR,
    dry_run: bool = False,
) -> List[str]:
    """Remove ReMe Light memory subdirs inside a running docker container."""
    workspace = posixpath.join(container_working_dir, "workspaces", agent)

    # Verify the workspace exists inside the container.
    check = _docker_exec(container, ["test", "-d", workspace])
    if check.returncode != 0:
        print(
            f"[reset-memory] workspace not found in container '{container}': {workspace}",
            file=sys.stderr,
        )
        return []

    subdir_names = load_memory_subdir_names_docker(container, container_working_dir, agent)
    removed: List[str] = []

    for _key, subdir in subdir_names.items():
        target = posixpath.join(workspace, subdir)
        # Safety: target must stay under the container workspace.
        if posixpath.commonpath([workspace, posixpath.normpath(target)]) != posixpath.normpath(workspace):
            print(f"[reset-memory] Refusing to delete '{target}': not under workspace", file=sys.stderr)
            continue
        exists = _docker_exec(container, ["test", "-e", target])
        if exists.returncode != 0:
            continue
        proc = _docker_exec(container, ["rm", "-rf", target], dry_run=dry_run)
        if dry_run:
            removed.append(target)
            continue
        if proc.returncode == 0:
            print(f"[reset-memory] removed (in container): {target}")
            removed.append(target)
        else:
            print(f"[reset-memory] ERROR removing {target}: {proc.stderr.strip()}", file=sys.stderr)

    if not removed:
        print(f"[reset-memory] nothing to remove under {workspace} (container '{container}')")

    return removed


def reset_memory(
    agent: str,
    working_dir: Optional[str] = None,
    dry_run: bool = False,
) -> List[str]:
    """Remove ReMe Light memory subdirs for the given agent.

    Returns the list of removed (or would-be-removed) directory paths.
    """
    wd = resolve_working_dir(working_dir)
    workspace = resolve_agent_workspace(wd, agent)

    if not workspace.exists():
        print(f"[reset-memory] workspace not found: {workspace}", file=sys.stderr)
        return []

    subdir_names = load_memory_subdir_names(workspace)
    removed: List[str] = []

    for key, subdir in subdir_names.items():
        target = workspace / subdir
        if not target.exists():
            continue
        # Safety: must be under WORKING_DIR.
        ensure_within(target, wd)
        if dry_run:
            print(f"[reset-memory] (dry-run) would remove: {target}")
            removed.append(str(target))
            continue
        try:
            shutil.rmtree(target)
            print(f"[reset-memory] removed: {target}")
            removed.append(str(target))
        except OSError as e:
            print(f"[reset-memory] ERROR removing {target}: {e}", file=sys.stderr)

    if not removed:
        print(f"[reset-memory] nothing to remove under {workspace}")

    return removed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset QwenPaw native (ReMe Light) memory for an agent"
    )
    parser.add_argument("--agent", default="default", help="QwenPaw agent ID")
    parser.add_argument(
        "--working-dir",
        help="QwenPaw WORKING_DIR on host (or use QWENPAW_WORKING_DIR env). "
        "For host-side deletion (e.g. a bind mount or docker volume path).",
    )
    parser.add_argument(
        "--docker",
        metavar="CONTAINER",
        help="Delete inside a running container via `docker exec` "
        "(name or ID, e.g. qwenpaw). Use when WORKING_DIR is not accessible from host.",
    )
    parser.add_argument(
        "--container-working-dir",
        default=DEFAULT_CONTAINER_WORKING_DIR,
        help=f"WORKING_DIR path inside the container (default: {DEFAULT_CONTAINER_WORKING_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting",
    )
    args = parser.parse_args()

    if args.docker:
        reset_memory_docker(
            agent=args.agent,
            container=args.docker,
            container_working_dir=args.container_working_dir,
            dry_run=args.dry_run,
        )
    else:
        reset_memory(
            agent=args.agent,
            working_dir=args.working_dir,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
