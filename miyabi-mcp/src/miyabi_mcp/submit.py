"""job_submit: a dry run produces a checked plan and its digest; a commit with that digest runs qsub.

DESIGN.md 8.3. Checks fail closed; the user-level submit lock serializes the volatile re-checks and qsub
across projects (7.7); an idempotency key returns the job of an earlier call; an uncertain qsub result is
reconciled through MIYABI_SUBMIT_ID in the job's Variable_List before anything is resubmitted.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import observe, parse, policy as policies, states
from .config import Settings
from .ledger import Store, index_append, job_num, new_submit_id, register_project, submit_lock
from .pbs import Pbs
from .util import (MiyabiError, digest_bytes, digest_json, hms_to_seconds, now_iso, ok, pbs_time, read_json,
                   seconds_to_hms, within, write_json)

PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_:./ -]*>")
JOB_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{0,229}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
JOB_ID_LINE = re.compile(r"\d+(?:\[\])?\.[A-Za-z0-9_.-]+")
FORBIDDEN_OUTPUT = ("/tmp", "/var/tmp", "/dev/shm", "/local")
PLAN_TTL_S = 600
MAX_SCRIPT_BYTES = 1 << 20
RESERVED_ENV = {"MIYABI_SUBMIT_ID", "MIYABI_CLIENT_REF", "MIYABI_PROJECT_DIR", "MIYABI_JOB_RECORD"}
STABLE = ("project_dir", "script_digest", "script_source", "queue", "group", "select", "walltime", "name", "workdir",
          "output", "join", "env", "depend", "array", "labels", "client_ref", "request_key")


class Checks(list):
    def add(self, check_id: str, status: str, detail: str) -> None:
        self.append({"id": check_id, "status": status, "detail": detail})

    @property
    def failed(self) -> list[dict]:
        return [check for check in self if check["status"] == "fail"]


def select_units(select: str) -> int:
    total = 0
    for chunk in select.split("+"):
        match = re.match(r"\s*(\d+)", chunk)
        total += int(match.group(1)) if match else 1
    return total


def directives(text: str) -> list[str]:
    """PBS directive arguments: `#PBS` lines before the first executable line, as qsub reads them."""
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#PBS"):
            found.append(stripped[4:].strip())
        elif stripped.startswith("#"):
            continue
        else:
            break
    return found


def directive_keys(arguments: list[str]) -> dict[str, str]:
    keys = {}
    for argument in arguments:
        try:
            words = shlex.split(argument)
        except ValueError:
            words = argument.split()
        for flag, value in zip(words, words[1:] + [""]):
            if flag in {"-l", "-W"}:
                keys[f"{flag} {value.split('=', 1)[0]}"] = value
            elif flag.startswith("-") and len(flag) == 2:
                keys[flag] = value
    return keys


def env_string(env: dict[str, str]) -> str:
    parts = []
    for name, value in env.items():
        if "," in value or '"' in value or "'" in value:
            if "'" in value:
                raise MiyabiError("invalid_arguments", f"env {name}: a value with commas cannot contain '")
            value = f"'{value}'"
        parts.append(f"{name}={value}")
    return ",".join(parts)


def _route(queues: list[dict], queue: str, units: int, checks: Checks) -> dict | None:
    top = [q for q in queues if q["name"] == queue and not q.get("parent")]
    if not top:
        child = next((q for q in queues if q["name"] == queue and q.get("parent")), None)
        if child:
            checks.add("queue.name", "fail", f"{queue} is a routing destination; submit to {child['parent']}")
        else:
            checks.add("queue.name", "fail", f"unknown queue {queue}")
        return None
    entry = top[0]
    if entry.get("routing"):
        candidates = [q for q in queues if q.get("parent") == queue and q["min_nodes"] <= units <= q["max_nodes"]]
        if not candidates:
            checks.add("queue.shape", "fail", f"no destination of {queue} accepts {units} units")
            return None
        route = candidates[0]
    else:
        route = entry
        if not route["min_nodes"] <= units <= route["max_nodes"]:
            checks.add("queue.shape", "fail",
                       f"{queue} accepts {route['min_nodes']}–{route['max_nodes']} units, not {units}")
            return None
    checks.add("queue.shape", "pass", f"{units} units -> {route['name']}")
    if not route["enabled"]:
        checks.add("queue.status", "fail", f"{route['name']} is not enabled ({route['status']})")
    elif not route["started"]:
        checks.add("queue.status", "warn", f"{route['name']} is not started ({route['status']}); the job will wait")
    else:
        checks.add("queue.status", "pass", route["status"])
    return route


def _code_paths_writable(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths if path.exists() and os.access(path, os.W_OK)]


def build_plan(settings: Settings, store: Store, args: dict, policy: dict) -> tuple[dict, Checks, dict]:
    checks = Checks()
    extra: dict[str, Any] = {}
    # -- script
    if bool(args.get("script")) == bool(args.get("script_text")):
        raise MiyabiError("invalid_arguments", "give exactly one of script (absolute path) or script_text")
    script_source = None
    if args.get("script"):
        path = Path(args["script"])
        if not path.is_absolute():
            raise MiyabiError("invalid_arguments", "script must be an absolute path")
        if not path.is_file():
            raise MiyabiError("invalid_arguments", f"script {path} does not exist")
        if path.stat().st_size > MAX_SCRIPT_BYTES:
            raise MiyabiError("invalid_arguments", "script is larger than 1 MiB")
        text = path.read_text(encoding="utf-8", errors="replace")
        script_source = str(path)
    else:
        text = args["script_text"]
        if len(text.encode()) > MAX_SCRIPT_BYTES:
            raise MiyabiError("invalid_arguments", "script_text is larger than 1 MiB")
    extra["script_text"] = text
    syntax = subprocess.run([settings.command("bash"), "-n"], input=text, capture_output=True, text=True,
                            timeout=10, check=False)
    checks.add("script.syntax", "pass" if syntax.returncode == 0 else "fail",
               "bash -n" if syntax.returncode == 0 else syntax.stderr.strip()[-500:])
    placeholders = sorted(set(PLACEHOLDER.findall(text)))
    if placeholders and not args.get("allow_placeholders"):
        checks.add("script.placeholders", "fail", "unfilled placeholders: " + ", ".join(placeholders[:10]))
    elif placeholders:
        checks.add("script.placeholders", "warn", "allowed by caller: " + ", ".join(placeholders[:10]))
    found = directive_keys(directives(text))
    overridden = sorted(key for key in found if key in {"-q", "-N", "-o", "-j", "-J", "-l select", "-l walltime",
                                                         "-W group_list", "-W depend"})
    if overridden:
        checks.add("script.directives", "warn", "the command line overrides #PBS " + ", ".join(overridden))
    # -- resources
    queue = args.get("queue")
    walltime = args.get("walltime")
    if not queue or not walltime:
        raise MiyabiError("invalid_arguments", "queue and walltime are required")
    walltime_s = hms_to_seconds(walltime)
    if not walltime_s:
        raise MiyabiError("invalid_arguments", "walltime must be HH:MM:SS and above zero")
    walltime = seconds_to_hms(walltime_s)
    if args.get("interactive"):
        raise MiyabiError("invalid_arguments", "interactive jobs need a terminal: use pi-env-mcp terminal_open "
                          "and qsub -I there")
    select = args.get("select") or f"{int(args.get('nodes', 1))}:mpiprocs=1"
    units = select_units(select)
    queue_data = observe.queues(settings)
    route = _route(queue_data["queues"], queue, units, checks)
    unit = "mig_instance_hours" if route and route.get("mig") else "node_hours"
    requested = round(units * walltime_s / 3600, 6)
    system = route["system"] if route else None
    if route:
        if walltime_s > route["max_elapse_s"]:
            checks.add("queue.walltime", "fail", f"{walltime} exceeds {route['name']} maximum "
                       f"{seconds_to_hms(route['max_elapse_s'])}")
        else:
            checks.add("queue.walltime", "pass", f"≤ {seconds_to_hms(route['max_elapse_s'])}")
        stop = observe.snapshot(settings, max_age=60).get("scheduled_stop")
        if route.get("remain_elapse_s") is not None and walltime_s > route["remain_elapse_s"]:
            status = "fail" if policy["maintenance"] == "refuse_if_crossing" else "warn"
            checks.add("maintenance", status, f"walltime crosses the scheduled stop {stop}; the job will not start "
                       "before it")
        else:
            checks.add("maintenance", "pass", f"ends before the scheduled stop {stop}")
    # -- group
    projects = route["projects"] if route else []
    group = args.get("group")
    if not group:
        allowed = [p for p in projects if not policy.get("groups") or p in policy["groups"]]
        group = allowed[0] if len(allowed) == 1 else None
    if not group:
        checks.add("group", "fail", f"specify group; eligible: {projects}")
    elif projects and group not in projects:
        checks.add("group", "fail", f"{group} is not eligible for {queue} (eligible: {projects})")
    elif policy.get("groups") and group not in policy["groups"]:
        checks.add("group", "fail", f"policy allows groups {policy['groups']}")
    else:
        checks.add("group", "pass", group)
    # -- policy: queue and per-job caps
    if policy.get("queues") and queue not in policy["queues"]:
        checks.add("policy.queue", "fail", f"policy allows queues {policy['queues']}")
    per_job = policy.get("per_job", {})
    caps = []
    if unit == "node_hours" and "max_nodes" in per_job and units > per_job["max_nodes"]:
        caps.append(f"{units} nodes > {per_job['max_nodes']}")
    if unit == "mig_instance_hours" and "max_mig_instances" in per_job and units > per_job["max_mig_instances"]:
        caps.append(f"{units} MIG instances > {per_job['max_mig_instances']}")
    if "max_walltime" in per_job and walltime_s > hms_to_seconds(per_job["max_walltime"]):
        caps.append(f"walltime {walltime} > {per_job['max_walltime']}")
    cap_key = f"max_{unit}"
    if cap_key in per_job and requested > per_job[cap_key] + 1e-9:
        caps.append(f"{requested} {unit} > {per_job[cap_key]}")
    checks.add("policy.per_job", "fail" if caps else "pass", "; ".join(caps) or f"{requested} {unit} requested")
    # -- name, workdir, output, env, labels
    # Default job name: the client's reference (e.g. an attempt id), else the script's file name.
    name = args.get("name") or re.sub(r"[^A-Za-z0-9_.+-]", "-", args.get("client_ref") or Path(script_source or "job").stem)
    if not JOB_NAME.fullmatch(name) and not args.get("name"):
        name = "j-" + name[:228]
    if not JOB_NAME.fullmatch(name):
        raise MiyabiError("invalid_arguments", "name must start with a letter and use letters, digits, _ . + -")
    workdir = Path(args.get("workdir") or store.project_dir)
    if not workdir.is_absolute() or not workdir.is_dir():
        raise MiyabiError("invalid_arguments", f"workdir {workdir} must be an existing absolute directory")
    output = args.get("output")
    if output:
        if not output.startswith("/"):
            raise MiyabiError("invalid_arguments", "output must be an absolute path")
        if output.startswith(FORBIDDEN_OUTPUT):
            checks.add("output", "fail", f"{output} is node-local or temporary; PBS output there is lost")
        elif output.startswith("/home"):
            checks.add("output", "warn", "/home is small on Miyabi; prefer /work")
        else:
            checks.add("output", "pass", output)
    else:
        checks.add("output", "pass", f"default: {store.state}/logs/{name}-<stamp>-<id>.log")
    env = args.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise MiyabiError("invalid_arguments", "env must map names to string values")
    for key, value in env.items():
        if not ENV_NAME.fullmatch(key) or key in RESERVED_ENV or "\n" in value:
            raise MiyabiError("invalid_arguments", f"env {key!r} is not allowed")
    env_string(env)  # validates quoting
    labels = args.get("labels") or {}
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise MiyabiError("invalid_arguments", "labels must map names to string values")
    if args.get("depend"):
        checks.add("depend", "warn", "afterok chains break when a parent fails; prefer a notification group")
    if args.get("notify") is not None:
        from .notify import check_spec

        checks.add("notify", *check_spec(store, args["notify"]))
    writable = sorted(set(_code_paths_writable([workdir] + ([Path(script_source).parent] if script_source else []))))
    recorded = bool(args.get("client_ref") or (args.get("labels") or {}).get("code_commit"))
    if writable and recorded:
        checks.add("code.mutable", "warn", "this run is tied to a recorded version, but the code it reads at run "
                   "time can still change: writable " + ", ".join(writable) + ". Run from a read-only snapshot "
                   "(e.g. git archive into <project>/snapshots/<commit>, chmod -R a-w) and set workdir to it")
    elif writable:
        checks.add("code.mutable", "pass", "code is read from writable directories (fine for development): "
                   + ", ".join(writable))
    else:
        checks.add("code.mutable", "pass", "code is read from read-only directories")
    plan = {
        "project_dir": str(store.project_dir), "system": system,
        "script_source": script_source, "script_digest": digest_bytes(text.encode("utf-8")),
        "directives": directives(text), "queue": queue, "route": route["name"] if route else None,
        "group": group, "select": select, "units": units, "unit": unit, "walltime": walltime,
        "walltime_s": walltime_s, "requested_unit_hours": requested, "name": name, "workdir": str(workdir),
        "output": output, "join": args.get("join") or "oe", "env": dict(sorted(env.items())),
        "depend": args.get("depend"), "array": args.get("array"), "labels": dict(sorted(labels.items())),
        "client_ref": args.get("client_ref"), "request_key": args.get("request_key"),
        "expected_seconds": args.get("expected_seconds"), "note": args.get("note"),
    }
    plan["plan_digest"] = digest_json({key: plan[key] for key in STABLE})
    return plan, checks, extra


def volatile_checks(settings: Settings, store: Store, plan: dict, policy: dict) -> Checks:
    """Checks that depend on live state; repeated inside the submit lock."""
    checks = Checks()
    pbs = Pbs(settings)
    limits = pbs.limits()["limits"]
    row = next((r for r in limits if r["system"] == plan["system"] and r["project"] == plan["group"]), None)
    reserve = int(policy.get("reserve_accept_slots", 0))
    if row is None:
        checks.add("limit.accept", "warn", f"no --limit row for {plan['group']} on {plan['system']}")
    else:
        accept = row.get("accept") or {}
        room = accept.get("limit", 0) - accept.get("used", 0) - reserve
        checks.add("limit.accept", "pass" if room > 0 else "fail",
                   f"{accept.get('used')}/{accept.get('limit')} accepted, {reserve} reserved")
        run = row.get("run") or {}
        if run and run.get("used", 0) >= run.get("limit", 0):
            checks.add("limit.run", "warn", f"{run['used']}/{run['limit']} running: the job will queue")
    snap = observe.snapshot(settings, max_age=15)
    active_ids = {job_num(row["job_id"]) for row in snap["jobs"]}
    project_active = [r for r in store.records() if r.get("job_num") in active_ids]
    if "max_active_jobs" in policy:
        count = len(project_active)
        checks.add("policy.max_active_jobs", "pass" if count < policy["max_active_jobs"] else "fail",
                   f"{count} active in this project; limit {policy['max_active_jobs']}")
    for key, limit in (policy.get("label_limits") or {}).items():
        value = plan["labels"].get(key)
        if value is None:
            continue
        count = sum(1 for r in project_active if (r.get("labels") or {}).get(key) == value)
        checks.add(f"policy.label.{key}", "pass" if count < limit["max_active_jobs"] else "fail",
                   f"{count} active with {key}={value}; limit {limit['max_active_jobs']}")
    checks.extend(policies.window_checks(policy.get("budget_windows", []),
                                         policies.project_submissions(store.records()),
                                         plan["unit"], plan["units"] * plan["walltime_s"], "project"))
    user = policy.get("user") or {}
    if "max_active_jobs" in user:
        count = len(snap["jobs"])
        checks.add("user.max_active_jobs", "pass" if count < user["max_active_jobs"] else "fail",
                   f"{count} unfinished jobs of {settings.user}; limit {user['max_active_jobs']}")
    checks.extend(policies.window_checks(user.get("budget_windows", []), policies.user_submissions(settings),
                                         plan["unit"], plan["units"] * plan["walltime_s"], "user"))
    return checks


def build_argv(plan: dict, output: str, env: dict[str, str], script: Path) -> list[str]:
    argv = ["-q", plan["queue"], "-W", f"group_list={plan['group']}", "-l", f"select={plan['select']}",
            "-l", f"walltime={plan['walltime']}", "-N", plan["name"], "-j", plan["join"], "-o", output]
    if plan.get("depend"):
        argv += ["-W", f"depend={plan['depend']}"]
    if plan.get("array"):
        argv += ["-J", plan["array"]]
    return argv + ["-v", env_string(env), str(script)]


def parse_job_id(stdout: str) -> str | None:
    for line in reversed(stdout.strip().splitlines()):
        if JOB_ID_LINE.fullmatch(line.strip()):
            return line.strip()
    return None


def reconcile(settings: Settings, record: dict) -> str | None:
    """Find a job carrying this record's MIYABI_SUBMIT_ID (after an uncertain qsub)."""
    pbs = Pbs(settings)
    name = record["name"][:64]
    snap = observe.snapshot(settings, max_age=0, force=True)
    candidates = [row["job_id"] for row in snap["jobs"] if row["job_name"] == name]
    details = pbs.detail(candidates) if candidates else {}
    history = [row["job_id"] for row in pbs.history(1, 200) if row["job_name"] == name]
    if history:
        details.update(pbs.detail(history, history=True))
    for job_id, fields in details.items():
        if parse.variable_list(fields.get("Variable_List")).get("MIYABI_SUBMIT_ID") == record["submit_id"]:
            return job_id
    return None


def _bind(settings: Settings, store: Store, record: dict, job_id: str) -> dict:
    store.bind(record["submit_id"], job_id)
    record.update(job_id=job_id, job_num=job_num(job_id), status="submitted")
    record["submit"]["accepted_ts"] = time.time()
    store.write(record)
    if record.get("request_key"):
        write_json(store.request_path(record["request_key"]),
                   {"request_key": record["request_key"], "submit_id": record["submit_id"], "job_id": job_id,
                    "status": "submitted", "updated_at": now_iso()})
    submit = record["submit"]
    index_append(settings, {"type": "submitted", "job_id": job_id, "project_dir": str(store.project_dir),
                            "submit_id": record["submit_id"], "name": record["name"], "queue": submit["queue"],
                            "units": submit["units"], "unit": submit["unit"], "walltime_s": submit["walltime_s"],
                            "requested": submit["requested_unit_hours"], "submitted_at": submit["submitted_at"],
                            "labels": record.get("labels") or {}})
    return record


def _first_event(settings: Settings, store: Store, job_id: str) -> list[dict]:
    """The `submitted` event, confirmed by one `qstat -f` when possible (DESIGN.md 7.3)."""
    details = {}
    try:
        details = Pbs(settings).detail([job_id])
    except MiyabiError:
        pass
    fields = details.get(job_id) or next(iter(details.values()), None)
    events = []
    if fields and fields.get("job_state") in {"Q", "H", "W", "T"}:
        observation = {"kind": "submitted", "execution_state": "queued", "outcome": "unknown",
                       "raw_state": fields["job_state"], "pbs_time": pbs_time(fields.get("qtime"))}
        events.append(store.apply(job_id, observation, "qsub"))
    else:
        observation = {"kind": "submitted", "execution_state": "queued", "outcome": "unknown",
                       "raw_state": "submitted", "pbs_time": None}
        events.append(store.apply(job_id, observation, "qsub"))
        if fields:
            later = states.from_detail(fields, history=False, cancel_requested=False,
                                       mig_hardware=settings.config.get("mig_hardware"))
            events.append(store.apply(job_id, later, "qstat -f"))
    return [event for event in events if event]


def submit(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    register_project(settings)
    policy = policies.load(settings)
    plan, checks, extra = build_plan(settings, store, args, policy)
    if args.get("dry_run", True):
        if not checks.failed:
            checks.extend(volatile_checks(settings, store, plan, policy))
        valid = not checks.failed
        if valid:
            write_json(store.plan_path(plan["plan_digest"]), {"created_at": now_iso(), "plan": plan})
        preview = build_argv(plan, plan["output"] or f"{store.state}/logs/{plan['name']}-<stamp>-<id>.log",
                             {**plan["env"], "MIYABI_SUBMIT_ID": "<id>"}, Path("<snapshot>"))
        return ok(dry_run=True, valid=valid, plan=plan, plan_digest=plan["plan_digest"] if valid else None,
                  checks=checks, qsub_preview=["qsub", *preview], expires_in_s=PLAN_TTL_S if valid else None,
                  next="call job_submit again with the same arguments, dry_run=false and this plan_digest"
                  if valid else "fix the failed checks")
    if checks.failed:
        raise MiyabiError("plan_rejected", "the plan has failed checks", checks=checks)
    exempt = plan["requested_unit_hours"] < float(policy.get("auto_commit_below_unit_hours", 0))
    if policy.get("require_plan", True) and not exempt:
        given = args.get("plan_digest")
        if not given:
            raise MiyabiError("plan_required", "run job_submit with dry_run=true first and pass its plan_digest")
        if given != plan["plan_digest"]:
            raise MiyabiError("plan_mismatch", "the arguments differ from the planned ones", expected=given,
                              actual=plan["plan_digest"])
        saved = read_json(store.plan_path(given))
        if not saved or not within(saved.get("created_at"), PLAN_TTL_S):
            raise MiyabiError("plan_expired", "the plan is older than 10 minutes or unknown; run the dry run again")
    pbs = Pbs(settings)
    with submit_lock(settings):
        key = plan.get("request_key")
        if key:
            request = read_json(store.request_path(key))
            if request and request.get("job_id"):
                record = store.find(request["job_id"])
                return ok(idempotent=True, job_id=request["job_id"], submit_id=request["submit_id"],
                          record=store.locator(request["job_id"]), state=(record or {}).get("state"))
            if request and request.get("status") in {"submitting", "uncertain"}:
                previous = store.read_submission(request["submit_id"])
                found = reconcile(settings, previous) if previous else None
                if found and previous:
                    _bind(settings, store, previous, found)
                    return ok(idempotent=True, reconciled=True, job_id=found, submit_id=previous["submit_id"],
                              record=store.locator(found), events=_first_event(settings, store, found))
                if not args.get("resubmit"):
                    raise MiyabiError("submit_uncertain", "an earlier qsub with this request_key has an unknown "
                                      "result and no job carries its submit ID; pass resubmit=true to submit again",
                                      submit_id=request.get("submit_id"))
        live = volatile_checks(settings, store, plan, policy)
        if live.failed:
            raise MiyabiError("plan_rejected", "a live check failed", checks=live)
        submit_id = new_submit_id()
        directory = store.submission_dir(submit_id)
        output = plan["output"] or str(store.state / "logs" / f"{plan['name']}-"
                                       f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{submit_id[-6:]}.log")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        env = {**plan["env"], "MIYABI_SUBMIT_ID": submit_id, "MIYABI_PROJECT_DIR": plan["project_dir"],
               "MIYABI_JOB_RECORD": str(directory)}
        if plan.get("client_ref"):
            env["MIYABI_CLIENT_REF"] = plan["client_ref"]
        record = {
            "schema_version": 1, "submit_id": submit_id, "job_id": None, "job_num": None, "origin": "submitted",
            "status": "submitting", "project_dir": plan["project_dir"], "system": plan["system"],
            "name": plan["name"], "labels": plan["labels"], "client_ref": plan.get("client_ref"),
            "request_key": key, "seq": 0, "state": None, "usage": None, "cancel_requested": False, "notes": [],
            "verdict": {"status": "unreviewed"}, "notifications": [],
            "submit": {**{k: plan[k] for k in ("queue", "route", "group", "select", "units", "unit", "walltime",
                                                 "walltime_s", "requested_unit_hours", "workdir", "join", "depend",
                                                 "array", "plan_digest", "expected_seconds", "note",
                                                 "script_source", "script_digest")},
                       "submitted_at": now_iso(), "host": os.uname().nodename.split(".")[0], "output": output,
                       "env_names": sorted(env), "actor": args.get("_actor")},
        }
        store.new_submission(record)
        script = directory / "script.sh"
        script.write_text(extra["script_text"], encoding="utf-8")
        argv = build_argv(plan, output, env, script)
        record["submit"]["argv"] = ["qsub", *argv]
        store.write(record)
        if key:
            write_json(store.request_path(key), {"request_key": key, "submit_id": submit_id, "status": "submitting",
                                                 "updated_at": now_iso()})
        try:
            result = pbs.qsub(argv, cwd=Path(plan["workdir"]))
        except MiyabiError as exc:
            return _uncertain(settings, store, record, str(exc))
        if result.returncode != 0:
            record.update(status="submit_failed", error=result.stderr.strip()[-2000:])
            store.write(record)
            if key:
                write_json(store.request_path(key), {"request_key": key, "submit_id": submit_id, "status": "failed",
                                                     "updated_at": now_iso()})
            raise MiyabiError("qsub_failed", "qsub rejected the job", returncode=result.returncode,
                              stdout=result.stdout[-2000:], stderr=result.stderr[-2000:], submit_id=submit_id)
        job_id = parse_job_id(result.stdout)
        if job_id is None:
            return _uncertain(settings, store, record, f"unparsable qsub output: {result.stdout[-500:]!r}")
        _bind(settings, store, record, job_id)
    events = _first_event(settings, store, job_id)
    from .notify import export_command

    response = ok(job_id=job_id, submit_id=submit_id, record=store.locator(job_id), output=output,
                  events=events, checks=checks + live, export_command=export_command(settings, store.project_dir))
    if args.get("notify"):
        from .notify import join

        try:
            response["notify"] = join(settings, store, args["notify"], job_id)
        except MiyabiError as exc:  # the job exists: report the notification problem, do not hide the job
            response["notify_error"] = exc.to_dict()
    return response


def _uncertain(settings: Settings, store: Store, record: dict, reason: str) -> dict:
    record.update(status="submit_uncertain", error=reason)
    store.write(record)
    if record.get("request_key"):
        write_json(store.request_path(record["request_key"]),
                   {"request_key": record["request_key"], "submit_id": record["submit_id"], "status": "uncertain",
                    "updated_at": now_iso()})
    found = reconcile(settings, record)
    if found:
        _bind(settings, store, record, found)
        return ok(job_id=found, submit_id=record["submit_id"], reconciled=True, record=store.locator(found),
                  events=_first_event(settings, store, found))
    raise MiyabiError("submit_uncertain", f"qsub result unknown ({reason}); no job carries this submit ID yet. "
                      "Check again later with the same request_key before resubmitting",
                      submit_id=record["submit_id"])
