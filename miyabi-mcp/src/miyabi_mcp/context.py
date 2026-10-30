"""Host and allocation evidence (ported from miyabi-pbs-cmd/scripts/context.py)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

ARCHITECTURES = {"Miyabi-G": "aarch64", "Miyabi-C": "x86_64"}


def identify_host(hostname: str) -> tuple[str, str | None]:
    """Classify known short/FQDN hostnames; unknown names never grant execution."""
    short = hostname.split(".", 1)[0]
    for suffix, system in (("g", "Miyabi-G"), ("c", "Miyabi-C")):
        if short.startswith(("miyabi-" + suffix, "interact-" + suffix)):
            return "login", system
        if re.fullmatch("m" + suffix + r"[0-9]+", short):
            return "compute", system
    return "unknown", None


def inspect(hostname: str | None = None, architecture: str | None = None,
            environ: Mapping[str, str] | None = None, target_system: str | None = None) -> dict:
    """Combine host, architecture, job ID and nodefile evidence conservatively."""
    uname = os.uname()
    hostname = hostname or uname.nodename
    architecture = architecture or uname.machine
    environ = os.environ if environ is None else environ
    role, system = identify_host(hostname)
    job_id = environ.get("PBS_JOBID") or None
    nodefile = environ.get("PBS_NODEFILE") or None
    hosts: list[str] = []
    reasons: list[str] = []
    if nodefile:
        try:
            hosts = list(dict.fromkeys(Path(nodefile).read_text().split()))
        except (OSError, UnicodeError) as exc:
            reasons.append(f"Cannot read PBS_NODEFILE: {exc}")
    if role != "compute":
        reasons.append("Current host is not a recognized Miyabi compute node")
    host_arch_matches = system is not None and architecture == ARCHITECTURES[system]
    if system is not None and not host_arch_matches:
        reasons.append("Observed architecture disagrees with the host's Miyabi system")
    valid_job_id = bool(job_id and re.fullmatch(r"[0-9]+(?:\[[0-9]*\])?(?:\.[A-Za-z0-9_.-]+)?", job_id))
    if not valid_job_id:
        reasons.append("PBS_JOBID is missing or malformed")
    if not hosts:
        reasons.append("PBS_NODEFILE is missing, unreadable or empty")
    member = hostname.split(".", 1)[0] in {host.split(".", 1)[0] for host in hosts}
    if hosts and not member:
        reasons.append("Current host is absent from PBS_NODEFILE")
    confirmed = role == "compute" and host_arch_matches and valid_job_id and member
    target_matches = (target_system is not None and system == target_system
                      and architecture == ARCHITECTURES.get(target_system))
    if target_system is None:
        reasons.append("Set the project's target system before running application/runtime checks")
    elif not target_matches:
        reasons.append("Current system/architecture does not match the project's target")
    return {
        "hostname": hostname, "architecture": architecture, "node_role": role, "system": system,
        "target_system": target_system, "target_architecture": ARCHITECTURES.get(target_system or ""),
        "pbs_job_id": job_id, "pbs_nodefile": nodefile, "allocated_hosts": hosts,
        "allocation_confirmed": confirmed, "runtime_execution_allowed": confirmed and target_matches,
        "reasons": reasons,
    }
