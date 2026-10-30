"""PBS facts -> execution_state, outcome, raw_state, pbs_time and usage (DESIGN.md 7.3–7.5)."""

from __future__ import annotations

import re
from typing import Any

from .util import hms_to_seconds, pbs_time

# Status words of the `qstat -ll` table -> the class used to notice a change.
TABLE_CLASS = {
    "QUEUED": "queued", "WAITING": "queued", "TRANSITING": "queued", "HELD": "held",
    "RUNNING": "running", "EXITING": "running", "BEGUN": "running", "SUSPENDED": "running",
    "FINISH": "ended", "EXPIRED": "ended",
}


def state_class(state: dict | None) -> str | None:
    """The change-detection class of a recorded state: queued, held, running, ended or unknown."""
    if not state:
        return None
    if state.get("execution_state") == "queued":
        return "held" if state.get("raw_state") == "H" else "queued"
    return state.get("execution_state")


def usage(fields: dict[str, str], mig_hardware: str | None = None) -> dict | None:
    """Measured usage of a finished job; None when PBS recorded no walltime (never zero)."""
    walltime = (fields.get("resources_used.walltime") or "").strip()
    seconds = hms_to_seconds(walltime)
    if seconds is None:
        return None
    vnode = fields.get("exec_vnode", "")
    g_nodes = sum(int(n) for n in re.findall(r"nvnodes-g=(\d+)", vnode))
    mig = sum(int(n) for n in re.findall(r"nvnodes-mig=(\d+)", vnode))
    if g_nodes:
        return {"quantity": round(seconds * g_nodes / 3600, 6), "unit": "node_hours", "resource_type": "gpu_node",
                "hardware": "GH200", "basis": "measured",
                "source_ref": f"resources_used.walltime={walltime} x nvnodes-g={g_nodes}"}
    if mig:
        return {"quantity": round(seconds * mig / 3600, 6), "unit": "mig_instance_hours",
                "resource_type": "gpu_mig_instance", "hardware": mig_hardware, "basis": "measured",
                "source_ref": f"resources_used.walltime={walltime} x nvnodes-mig={mig}"}
    nodect = (fields.get("Resource_List.nodect") or "").strip()
    if nodect.isdigit():
        return {"quantity": round(seconds * int(nodect) / 3600, 6), "unit": "node_hours", "resource_type": "node",
                "hardware": None, "basis": "measured",
                "source_ref": f"resources_used.walltime={walltime} x nodect={nodect}"}
    return None


def token(fields: dict[str, str]) -> float | None:
    value = (fields.get("TOKEN") or "").strip().replace(",", "")
    try:
        return float(value) if value not in {"", "-"} else None
    except ValueError:
        return None


def exit_status(fields: dict[str, str]) -> int | None:
    value = (fields.get("Exit_status") or "").strip()
    return int(value) if re.fullmatch(r"-?\d+", value) else None


def from_detail(fields: dict[str, str], *, history: bool, cancel_requested: bool,
                mig_hardware: str | None = None) -> dict[str, Any]:
    """One `qstat [-H] -f` record -> an observation (without event bookkeeping)."""
    job_state = (fields.get("job_state") or "").strip()
    if history or job_state in {"F", "X"}:
        started = bool((fields.get("stime") or "").strip())
        status = exit_status(fields)
        if not started:
            outcome, raw = "cancelled", "FINISH no-start"
        elif status is None:
            outcome, raw = "unknown", "F"
        else:
            raw = f"F Exit_status={status}"
            if status == 0:
                outcome = "success"
            elif 0 < status < 256:
                outcome = "failed"
            else:  # 256 + signal, or a negative PBS execution code (e.g. walltime)
                outcome = "cancelled" if cancel_requested else "failed"
        return {"kind": "ended", "execution_state": "ended", "outcome": outcome, "raw_state": raw,
                "pbs_time": pbs_time(fields.get("obittime")) or pbs_time(fields.get("mtime")),
                "exit_status": status, "usage": usage(fields, mig_hardware), "token": token(fields)}
    if job_state == "H":
        return {"kind": "held", "execution_state": "queued", "outcome": "unknown", "raw_state": "H",
                "pbs_time": None}
    if job_state in {"Q", "W", "T"}:
        return {"kind": "queued", "execution_state": "queued", "outcome": "unknown", "raw_state": job_state,
                "pbs_time": pbs_time(fields.get("qtime"))}
    if job_state in {"R", "E", "B", "S", "U"}:
        return {"kind": "running", "execution_state": "running", "outcome": "unknown", "raw_state": job_state,
                "pbs_time": pbs_time(fields.get("stime"))}
    return {"kind": "unknown", "execution_state": "unknown", "outcome": "unknown",
            "raw_state": f"job_state={job_state or '?'}", "pbs_time": None}


def detail_summary(fields: dict[str, str]) -> dict[str, Any]:
    """The fields of a `qstat -f` record an agent usually needs, with times in ISO 8601."""
    def path(value: str | None) -> str | None:
        return value.split(":", 1)[1] if value and ":" in value else value

    keep = ("Job_Name", "job_state", "queue", "exec_host", "exec_vnode", "comment", "Submit_arguments",
            "Resource_List.select", "Resource_List.walltime", "Resource_List.nodect", "Resource_List.ngpus",
            "resources_used.walltime", "resources_used.cput", "resources_used.mem", "eligible_time", "run_count",
            "Exit_status", "group_list", "Job_Owner")
    summary: dict[str, Any] = {key: fields[key] for key in keep if key in fields}
    for key in ("qtime", "etime", "stime", "obittime", "mtime", "estimated.start_time"):
        if key in fields:
            summary[key] = pbs_time(fields[key]) or fields[key]
    summary["output_path"] = path(fields.get("Output_Path"))
    summary["error_path"] = path(fields.get("Error_Path"))
    for key, value in fields.items():
        if key.startswith("resources_used.gpu_"):
            summary[key] = value.strip("'")
    return summary
