"""Limits on submission: user-level defaults and caps, project shares (DESIGN.md 7.7, 10.1).

A policy file that cannot be read or has unknown keys stops writes (fail closed).
"""

from __future__ import annotations

import json
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
TOP_KEYS = {"groups", "queues", "per_job", "max_active_jobs", "label_limits", "budget_windows",
            "reserve_accept_slots", "require_plan", "auto_commit_below_unit_hours", "maintenance", "cancel_scope",
            "user", "comment"}
USER_KEYS = {"max_active_jobs", "budget_windows", "comment"}


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


def _read(path) -> dict:
    try:
        return read_json(path, {}) or {}
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
