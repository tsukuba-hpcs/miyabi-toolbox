#!/usr/bin/env python3
"""Inspect Miyabi host/allocation evidence without executing a project environment."""

import argparse
import json
import os
from pathlib import Path
import re
import sys


ARCHITECTURES = {"Miyabi-G": "aarch64", "Miyabi-C": "x86_64"}


def identify_host(hostname):
    """Classify known short/FQDN hostnames; unknown names never grant execution."""
    short = hostname.split(".", 1)[0]
    for suffix, system in (("g", "Miyabi-G"), ("c", "Miyabi-C")):
        if short.startswith(("miyabi-" + suffix, "interact-" + suffix)):
            return "login", system
        if re.fullmatch("m" + suffix + r"[0-9]+", short):
            return "compute", system
    return "unknown", None


def inspect_context(hostname, architecture, environ, target_system=None):
    """Combine host, architecture, job ID and nodefile evidence conservatively."""
    role, system = identify_host(hostname)
    job_id = environ.get("PBS_JOBID") or None
    nodefile = environ.get("PBS_NODEFILE") or None
    hosts, reasons = [], []
    if nodefile:
        try:
            hosts = list(dict.fromkeys(Path(nodefile).read_text().split()))
        except (OSError, UnicodeError) as exc:
            reasons.append("Cannot read PBS_NODEFILE: {}".format(exc))
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
    target_matches = target_system is not None and system == target_system and architecture == ARCHITECTURES[target_system]
    if target_system is None:
        reasons.append("Set the project's target system before running application/runtime checks")
    elif not target_matches:
        reasons.append("Current system/architecture does not match the project's target")
    return {
        "schema_version": 1, "ok": True, "hostname": hostname,
        "architecture": architecture, "node_role": role, "system": system,
        "target_system": target_system,
        "target_architecture": ARCHITECTURES.get(target_system),
        "pbs_job_id": job_id, "pbs_nodefile": nodefile, "allocated_hosts": hosts,
        "allocation_confirmed": confirmed,
        "runtime_execution_allowed": confirmed and target_matches,
        "reasons": reasons,
    }


def main(argv=None):
    """Emit context JSON; an explicit compute guard exits nonzero on refusal."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--target-system", choices=tuple(ARCHITECTURES))
    parser.add_argument("--require-compute", action="store_true",
                        help="require a matching allocation; needs --target-system")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.require_compute and args.target_system is None:
        parser.error("--require-compute requires --target-system")
    host = os.uname()
    result = inspect_context(host.nodename, host.machine, os.environ, args.target_system)
    if args.require_compute and not result["runtime_execution_allowed"]:
        result["ok"] = False
        result["error"] = {"type": "execution_context", "message": "Project execution requires a matching compute allocation"}
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None,
                     separators=None if args.pretty else (",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
