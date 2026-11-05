"""Limits on submission: user-level defaults and caps, project shares and a project budget (DESIGN.md 7.7, 10.1).

A policy file that cannot be read or has unknown keys stops writes (fail closed).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from .config import Settings
from .ledger import index_entries
from .util import MiyabiError, hms_to_seconds, parse_duration, read_json, within

DEFAULTS = {
    "require_plan": True,
    "auto_commit_below_unit_hours": 0.0,
    "reserve_accept_slots": 4,
    "maintenance": "refuse_if_crossing",
    "cancel_scope": "project",
}
PER_JOB_KEYS = {"max_nodes", "max_mig_instances", "max_walltime", "max_node_hours", "max_mig_instance_hours"}
WINDOW_KEYS = {"window", "max_jobs", "max_requested_node_hours", "max_requested_mig_instance_hours"}
TOP_KEYS = {"groups", "queues", "per_job", "max_active_jobs", "label_limits", "budget_windows", "budget",
            "reserve_accept_slots", "require_plan", "auto_commit_below_unit_hours", "maintenance", "cancel_scope",
            "user", "comment"}
USER_KEYS = {"max_active_jobs", "budget_windows", "comment"}
LEDGER_UNITS = ("node_hours", "mig_instance_hours")
BUDGET_KEYS = {"unit", "limit", "warn_at", "factors", "source", "comment"}


def _number(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise MiyabiError("invalid_policy", f"{where} must be a non-negative number")


def _windows(windows: Any, where: str) -> None:
    if not isinstance(windows, list):
        raise MiyabiError("invalid_policy", f"{where} must be a list")
    for index, window in enumerate(windows):
        if not isinstance(window, dict) or "window" not in window or set(window) - WINDOW_KEYS:
            raise MiyabiError("invalid_policy", f"{where}[{index}] needs `window` and only {sorted(WINDOW_KEYS)}")
        parse_duration(window["window"])
        for key in set(window) - {"window"}:
            _number(window[key], f"{where}[{index}].{key}")


def _budget(budget: Any, where: str) -> None:
    if not isinstance(budget, dict) or set(budget) - BUDGET_KEYS or not {"unit", "limit"} <= set(budget):
        raise MiyabiError("invalid_policy", f"{where} needs `unit` and `limit` and accepts only {sorted(BUDGET_KEYS)}")
    if not isinstance(budget["unit"], str) or not budget["unit"]:
        raise MiyabiError("invalid_policy", f"{where}.unit must be a unit name, e.g. node_hours")
    _number(budget["limit"], f"{where}.limit")
    warn_at = budget.get("warn_at", 0.8)
    if isinstance(warn_at, bool) or not isinstance(warn_at, (int, float)) or not 0 < warn_at <= 1:
        raise MiyabiError("invalid_policy", f"{where}.warn_at must be a fraction of the limit in (0, 1]")
    if "factors" not in budget and budget["unit"] not in LEDGER_UNITS:
        raise MiyabiError("invalid_policy", f"{where}.factors must convert the ledger units {list(LEDGER_UNITS)} "
                          f"to {budget['unit']}")
    factors = budget.get("factors", {})
    if not isinstance(factors, dict) or ("factors" in budget and not factors):
        raise MiyabiError("invalid_policy", f"{where}.factors must map ledger units to numbers")
    for unit, factor in factors.items():
        if unit not in LEDGER_UNITS:
            raise MiyabiError("invalid_policy", f"{where}.factors: unknown ledger unit {unit!r}; use {list(LEDGER_UNITS)}")
        _number(factor, f"{where}.factors.{unit}")
    for key in ("source", "comment"):
        if key in budget and not isinstance(budget[key], str):
            raise MiyabiError("invalid_policy", f"{where}.{key} must be a string")


def validate(data: Any, where: str) -> dict:
    if not isinstance(data, dict):
        raise MiyabiError("invalid_policy", f"{where} must be a JSON object")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise MiyabiError("invalid_policy", f"{where}: unknown keys {sorted(unknown)}")
    for key in ("groups", "queues"):
        if key in data and not (isinstance(data[key], list) and all(isinstance(v, str) for v in data[key])):
            raise MiyabiError("invalid_policy", f"{where}.{key} must be a list of strings")
    per_job = data.get("per_job", {})
    if not isinstance(per_job, dict) or set(per_job) - PER_JOB_KEYS:
        raise MiyabiError("invalid_policy", f"{where}.per_job accepts only {sorted(PER_JOB_KEYS)}")
    for key, value in per_job.items():
        if key == "max_walltime":
            if hms_to_seconds(value) is None:
                raise MiyabiError("invalid_policy", f"{where}.per_job.max_walltime must be HH:MM:SS")
        else:
            _number(value, f"{where}.per_job.{key}")
    for key in ("max_active_jobs", "reserve_accept_slots", "auto_commit_below_unit_hours"):
        if key in data:
            _number(data[key], f"{where}.{key}")
    if "label_limits" in data:
        limits = data["label_limits"]
        if not isinstance(limits, dict) or not all(isinstance(v, dict) and set(v) <= {"max_active_jobs"}
                                                   for v in limits.values()):
            raise MiyabiError("invalid_policy", f"{where}.label_limits must map a label to {{max_active_jobs}}")
    if "budget_windows" in data:
        _windows(data["budget_windows"], f"{where}.budget_windows")
    if "budget" in data:
        _budget(data["budget"], f"{where}.budget")
    if "require_plan" in data and not isinstance(data["require_plan"], bool):
        raise MiyabiError("invalid_policy", f"{where}.require_plan must be true or false")
    if data.get("maintenance", "warn") not in {"warn", "refuse_if_crossing"}:
        raise MiyabiError("invalid_policy", f"{where}.maintenance must be warn or refuse_if_crossing")
    if data.get("cancel_scope", "project") not in {"project", "any"}:
        raise MiyabiError("invalid_policy", f"{where}.cancel_scope must be project or any")
    user = data.get("user", {})
    if not isinstance(user, dict) or set(user) - USER_KEYS:
        raise MiyabiError("invalid_policy", f"{where}.user accepts only {sorted(USER_KEYS)}")
    if "budget_windows" in user:
        _windows(user["budget_windows"], f"{where}.user.budget_windows")
    if "max_active_jobs" in user:
        _number(user["max_active_jobs"], f"{where}.user.max_active_jobs")
    return data


def _read(path) -> Any:
    """The file's JSON as written; {} only when there is no file (null, [] or 0 must fail validation)."""
    try:
        return read_json(path, {})
    except json.JSONDecodeError as exc:
        raise MiyabiError("invalid_policy", f"{path}: {exc}") from None


def load(settings: Settings) -> dict:
    """Effective policy: defaults < user policy < project policy; `user` caps come from the user file only."""
    user = validate(_read(settings.user_dir / "policy.json"), "user policy")
    project = {}
    if settings.state_dir is not None:
        project = validate(_read(settings.state_dir / "policy.json"), "project policy")
        if "user" in project:
            raise MiyabiError("invalid_policy", "project policy cannot set user-wide caps (`user`)")
    effective = {**DEFAULTS, **{k: v for k, v in user.items() if k != "user"}, **project}
    effective["user"] = user.get("user", {})
    return effective


def requested_in_window(entries: Iterable[dict], window_s: int) -> dict:
    """Job count and requested unit-seconds (units x walltime) of submissions within the window."""
    totals = {"jobs": 0, "node_hours": 0, "mig_instance_hours": 0}
    for entry in entries:
        if not within(entry.get("submitted_at"), window_s):
            continue
        totals["jobs"] += 1
        unit = entry.get("unit")
        if unit in totals:
            totals[unit] += int(entry.get("units") or 0) * int(entry.get("walltime_s") or 0)
    return totals


def project_submissions(records: Iterable[dict]) -> list[dict]:
    result = []
    for record in records:
        if record.get("origin") != "submitted" or record.get("status") == "submit_failed":
            continue
        submit = record.get("submit") or {}
        result.append({"submitted_at": submit.get("submitted_at"), "unit": submit.get("unit"),
                       "units": submit.get("units"), "walltime_s": submit.get("walltime_s")})
    return result


def user_submissions(settings: Settings) -> list[dict]:
    return [{"submitted_at": e.get("submitted_at"), "unit": e.get("unit"), "units": e.get("units"),
             "walltime_s": e.get("walltime_s")} for e in index_entries(settings) if e.get("type") == "submitted"]


def window_checks(windows: list[dict], entries: list[dict], unit: str, unit_seconds: int, scope: str) -> list[dict]:
    """Budget windows compared in whole unit-seconds, so six 10-minute jobs fit exactly one unit-hour."""
    checks = []
    for window in windows:
        seconds = parse_duration(window["window"])
        used = requested_in_window(entries, seconds)
        label = f"{scope}.budget[{window['window']}]"
        if "max_jobs" in window and used["jobs"] + 1 > window["max_jobs"]:
            checks.append({"id": label, "status": "fail",
                           "detail": f"{used['jobs']} jobs already submitted; limit {window['max_jobs']}"})
            continue
        key = f"max_requested_{unit}"
        if key in window and used[unit] + unit_seconds > round(window[key] * 3600):
            checks.append({"id": label, "status": "fail", "detail": f"{used[unit] / 3600:.3f} + "
                           f"{unit_seconds / 3600:.3f} {unit} exceeds {window[key]}"})
            continue
        checks.append({"id": label, "status": "pass",
                       "detail": f"{used['jobs']} jobs, {used[unit] / 3600:.3f} {unit} requested in window"})
    return checks


# -- the project budget: measured use plus reservations ------------------------------------------------------------

def budget_status(budget: dict, records: Iterable[dict]) -> dict:
    """Projected use of a budget, in its unit: the measured usage of ended jobs plus a reservation of the
    requested units x walltime for every job that may still use resources (DESIGN.md 10.1).

    A rejected submission (submit_failed) counts nothing, nor does a job that ended without ever starting.
    Queued, running and unknown jobs, a qsub whose result is uncertain and an ended job whose measurement
    is missing each reserve their full request until the ledger knows better. A ledger unit without a
    factor is listed in `unconverted`: the budget cannot be judged then (fail closed).
    """
    factors = budget.get("factors") or {budget["unit"]: 1}
    reserving: Counter = Counter()  # ledger unit -> requested unit-seconds, summed exactly
    measured: Counter = Counter()  # ledger unit -> measured hours (floats from PBS)
    jobs = {"measured": 0, "reserved": 0, "never_started": 0, "rejected": 0}
    missing: list[str] = []
    for record in records:
        if record.get("status") == "submit_failed":
            jobs["rejected"] += 1
            continue
        state = record.get("state") or {}
        usage = record.get("usage") or {}
        if state.get("execution_state") == "ended":
            if usage.get("unit"):
                measured[usage["unit"]] += float(usage.get("quantity") or 0)
                jobs["measured"] += 1
                continue
            if state.get("raw_state") == "FINISH no-start":
                jobs["never_started"] += 1
                continue
            missing.append(record.get("job_id") or record.get("submit_id"))
        submit = record.get("submit") or {}
        jobs["reserved"] += 1
        reserving[submit.get("unit") or "unknown"] += int(submit.get("units") or 0) * int(submit.get("walltime_s") or 0)
    unconverted = sorted(unit for unit in set(measured) | set(reserving) if unit not in factors)
    used = sum(hours * factors.get(unit, 0) for unit, hours in measured.items())
    reserved = sum(value / 3600 * factors.get(unit, 0) for unit, value in reserving.items())
    limit = float(budget["limit"])
    total = used + reserved
    status = {"unit": budget["unit"], "limit": budget["limit"], "warn_at": budget.get("warn_at", 0.8),
              "used": round(used, 6), "reserved": round(reserved, 6), "total": round(total, 6),
              "fraction": round(total / limit, 4) if limit else None, "jobs": jobs,
              "missing_measurement": len(missing), "factors": factors}
    if missing:
        status["missing_measurement_jobs"] = missing[:20]
    if unconverted:
        status["unconverted"] = unconverted
    if budget.get("source"):
        status["source"] = budget["source"]
    return status


def budget_check(budget: dict, records: Iterable[dict], unit: str, unit_seconds: int) -> dict:
    """The project budget with this request added: fail above the limit, warn from warn_at x limit."""
    status = budget_status(budget, records)
    factors = status["factors"]
    name, limit = budget["unit"], float(budget["limit"])
    unconverted = sorted(set(status.get("unconverted", [])) | ({unit} if unit not in factors else set()))
    source = f" ({budget['source']})" if budget.get("source") else ""
    if unconverted:
        return {"id": "policy.budget", "status": "fail",
                "detail": f"budget.factors has no factor for {', '.join(unconverted)}: the budget in {name} cannot "
                          "be judged; add the factor to policy.json"}
    request = unit_seconds / 3600 * factors[unit]
    projected = status["total"] + request
    text = (f"{status['used']:.3f} used + {status['reserved']:.3f} reserved + {request:.3f} this job = "
            f"{projected:.3f} of {budget['limit']} {name}{source}")
    if status["missing_measurement"]:
        text += f"; {status['missing_measurement']} ended job(s) without a measurement stay reserved"
    if projected > limit + 1e-9:
        return {"id": "policy.budget", "status": "fail",
                "detail": text + ": over the limit. Continuing needs the user's consent: they raise budget.limit "
                                 "in policy.json"}
    if projected >= float(budget.get("warn_at", 0.8)) * limit - 1e-9:
        share = f"{projected / limit:.0%}, " if limit else ""
        return {"id": "policy.budget", "status": "warn",
                "detail": text + f" ({share}at or above {budget.get('warn_at', 0.8):.0%}): report the budget to the "
                                 "user"}
    return {"id": "policy.budget", "status": "pass", "detail": text}

