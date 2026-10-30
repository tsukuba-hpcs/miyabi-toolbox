"""Query, cancel, annotate and ledger handlers (DESIGN.md 8.2, 8.4, 8.5)."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from . import __version__, config, context, export, observe, parse, policy as policies, states
from .config import Settings
from .ledger import Store, host, index_append, index_owner, job_num, register_project
from .pbs import Pbs
from .util import MiyabiError, digest_json, hms_to_seconds, now_iso, ok, pbs_time, read_json, tail_text

MAX_IDS = 40
MAX_TAIL = 200


def _store(settings: Settings) -> Store | None:
    if settings.project_dir is not None and settings.project_dir.is_dir():
        return Store(settings)
    return None


def _match(details: dict, job_id: str) -> tuple[str, dict] | tuple[None, None]:
    if job_id in details:
        return job_id, details[job_id]
    for key, fields in details.items():
        if job_num(key) == job_num(job_id):
            return key, fields
    return None, None


# -- pbs_context, pbs_queues --------------------------------------------------------------------------------------

def pbs_context(settings: Settings, args: dict) -> dict:
    result = ok(server={"name": "miyabi-mcp", "version": __version__, "python": sys.version.split()[0],
                        "host": host()},
                context=context.inspect(target_system=args.get("target_system")),
                settings=config.describe(settings))
    if args.get("include_pbs", True):
        try:
            limits = Pbs(settings).limits()
            result["pbs_projects"] = sorted({row["project"] for row in limits["limits"]})
            result["scheduled_stop"] = observe.snapshot(settings, max_age=60).get("scheduled_stop")
        except MiyabiError as exc:
            result["pbs_error"] = exc.to_dict()
    try:
        result["policy"] = policies.load(settings)
    except MiyabiError as exc:
        result["policy_error"] = exc.to_dict()
    return result


def pbs_queues(settings: Settings, args: dict) -> dict:
    include = args.get("include") or ["rsc", "limit", "rscuse", "token"]
    system = args.get("system")

    def keep(rows: list[dict]) -> list[dict]:
        return [row for row in rows if not system or row.get("system") in (system, None)]

    pbs = Pbs(settings)
    result = ok()
    errors = {}
    parts = {
        "rsc": lambda: observe.queues(settings),
        "limit": lambda: pbs.limits(),
        "rscuse": lambda: pbs.occupancy(),
        "token": lambda: pbs.tokens(),
    }
    for part in include:
        if part not in parts:
            raise MiyabiError("invalid_arguments", f"unknown part {part}; use rsc, limit, rscuse, token")
        try:
            data = parts[part]()
        except MiyabiError as exc:
            errors[part] = exc.to_dict()
            continue
        if part == "rsc":
            result["queues"] = keep(data["queues"])
        elif part == "limit":
            result["limits"] = keep(data["limits"])
        elif part == "rscuse":
            result["occupancy"] = keep(data)
        else:
            result["tokens"] = data
    if errors:
        result["errors"] = errors
        if len(errors) == len(include):
            raise MiyabiError("command_error", "all queue queries failed", errors=errors)
    return result


# -- job_list, job_get --------------------------------------------------------------------------------------------

def _ledger_view(record: dict | None) -> dict | None:
    if not record:
        return None
    return {"submit_id": record.get("submit_id"), "labels": record.get("labels"), "client_ref": record.get("client_ref"),
            "state": record.get("state"), "usage": record.get("usage"), "verdict": record.get("verdict"),
            "notifications": record.get("notifications"), "output": (record.get("submit") or {}).get("output")}


def job_list(settings: Settings, args: dict) -> dict:
    store = _store(settings)
    ledger_records = [r for r in store.records() if r.get("job_id")] if store else []
    snap = observe.snapshot(settings, max_age=settings.config["interactive_max_age_s"],
                            newer_than=observe.accepted_after(ledger_records))
    if store is not None and args.get("refresh_ledger", True):
        observe.refresh(store, ledger_records, snap)
    jobs = []
    for row in snap["jobs"]:
        job = dict(row, finished=False)
        record = store.find(row["job_id"]) if store else None
        job["ledger"] = _ledger_view(record)
        job["project"] = str(settings.project_dir) if record else index_owner(settings, row["job_id"])
        jobs.append(job)
    history = args.get("history")
    if history:
        days = min(int(history.get("days", 3)), 7)
        limit = min(int(history.get("limit", 100)), 500)
        for row in Pbs(settings).history(days, limit):
            record = store.find(row["job_id"]) if store else None
            jobs.append(dict(row, finished=True, ledger=_ledger_view(record),
                             project=str(settings.project_dir) if record else None))
    statuses = {s.upper() for s in args.get("status") or []}
    prefix = args.get("name_prefix")
    labels = args.get("labels") or {}
    if statuses:
        jobs = [job for job in jobs if job["status"] in statuses]
    if prefix:
        jobs = [job for job in jobs if job["job_name"].startswith(prefix)]
    if labels:
        jobs = [job for job in jobs if job["ledger"] and all((job["ledger"]["labels"] or {}).get(k) == v
                                                               for k, v in labels.items())]
    if args.get("project_only"):
        jobs = [job for job in jobs if job["ledger"]]
    result = ok(observed_at=snap["observed_at"], cache_age_s=snap.get("cache_age_s"),
                scheduled_stop=snap.get("scheduled_stop"), count=len(jobs))
    if args.get("summary"):
        result["status_counts"] = dict(sorted(Counter(job["status"] for job in jobs).items()))
    else:
        fields = args.get("fields")
        result["jobs"] = [{k: job.get(k) for k in fields} for job in jobs] if fields else jobs
    return result


def _owner_record(settings: Settings, project_dir: str | None, job_id: str) -> dict | None:
    if not project_dir:
        return None
    return read_json(Path(project_dir) / config.STATE_NAME / "jobs" / job_num(job_id) / "record.json")


def job_get(settings: Settings, args: dict) -> dict:
    ids = args.get("job_ids") or []
    if not ids or len(ids) > MAX_IDS:
        raise MiyabiError("invalid_arguments", f"give 1–{MAX_IDS} job_ids")
    tail = min(int(args.get("tail_lines") or 0), MAX_TAIL)
    store = _store(settings)
    records = [r for r in (store.find(i) for i in ids) if r] if store else []
    snap = observe.snapshot(settings, max_age=settings.config["interactive_max_age_s"],
                            newer_than=observe.accepted_after(records))
    if store is not None:
        if records:
            observe.refresh(store, records, snap)
    active = {job_num(row["job_id"]) for row in snap["jobs"]}
    pbs = Pbs(settings)
    live = [i for i in ids if job_num(i) in active]
    done = [i for i in ids if job_num(i) not in active]
    details = pbs.detail(live) if live else {}
    if done:
        details.update(pbs.detail(done, history=True))
    jobs = []
    for job_id in ids:
        key, fields = _match(details, job_id)
        record = store.find(job_id) if store else None
        variables = parse.variable_list((fields or {}).get("Variable_List"))
        owner = (str(settings.project_dir) if record else variables.get("MIYABI_PROJECT_DIR")
                 or index_owner(settings, job_id))
        if record is None and owner:
            record = _owner_record(settings, owner, job_id)
        names = set((record or {}).get("submit", {}).get("env_names", []))
        entry = {"job_id": key or job_id, "active": job_num(job_id) in active, "found": fields is not None,
                 "project": owner, "ledger": _ledger_view(record),
                 "detail": states.detail_summary(fields) if fields else None,
                 "variables": {k: v for k, v in variables.items() if k.startswith("MIYABI_") or k in names}}
        if tail and fields:
            path = entry["detail"].get("output_path")
            if path and Path(path).is_file():
                try:
                    entry["output_tail"] = tail_text(Path(path), tail)
                except OSError as exc:
                    entry["output_tail_error"] = str(exc)
            else:
                entry["output_tail_error"] = f"output file {path} is not readable here (yet)"
        jobs.append(entry)
    return ok(observed_at=snap["observed_at"], jobs=jobs)


# -- job_cancel ---------------------------------------------------------------------------------------------------

def job_cancel(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    policy = policies.load(settings)
    ids = list(args.get("job_ids") or [])
    selector = args.get("label_selector")
    if bool(ids) == bool(selector):
        raise MiyabiError("invalid_arguments", "give exactly one of job_ids or label_selector")
    if selector:
        ids = [r["job_id"] for r in store.records() if r.get("job_id")
               and (r.get("state") or {}).get("execution_state") not in {"ended"}
               and all((r.get("labels") or {}).get(k) == v for k, v in selector.items())]
    snap = observe.snapshot(settings, max_age=settings.config["interactive_max_age_s"],
                            newer_than=observe.accepted_after([r for r in (store.find(i) for i in ids) if r]))
    active = {job_num(row["job_id"]): row for row in snap["jobs"]}
    external_ok = bool(args.get("allow_external")) and policy.get("cancel_scope") == "any"
    targets = []
    for job_id in ids:
        record = store.find(job_id)
        row = active.get(job_num(job_id))
        full = record["job_id"] if record else job_id
        target = {"job_id": full, "in_ledger": record is not None, "active": row is not None,
                  "status": row["status"] if row else "not active"}
        if record is None and not external_ok:
            target.update(action="refused", reason="not in this project's ledger (cancel_scope=project)")
        elif row is None:
            target.update(action="skip", reason="not an unfinished job")
        else:
            target["action"] = "qdel"
        targets.append(target)
    chosen = [t["job_id"] for t in targets if t["action"] == "qdel"]
    digest = digest_json(sorted(chosen))
    if args.get("dry_run", True):
        return ok(dry_run=True, targets=targets, targets_digest=digest,
                  next="call again with dry_run=false, a reason and this targets_digest")
    if not args.get("reason"):
        raise MiyabiError("invalid_arguments", "a reason is required to cancel")
    if args.get("targets_digest") != digest:
        raise MiyabiError("targets_changed", "the targets differ from the dry run; run it again",
                          targets=targets, targets_digest=digest)
    if not chosen:
        return ok(cancelled=[], targets=targets)
    for target in targets:
        if target["action"] == "qdel" and target["in_ledger"]:
            store.note(target["job_id"], "cancel_requested", "qdel requested", note=args["reason"])
    result = Pbs(settings).qdel(chosen)
    snap = observe.snapshot(settings, max_age=0, force=True)
    records = [r for r in (store.find(i) for i in chosen) if r]
    events = observe.refresh(store, records, snap) if records else []
    return ok(cancelled=chosen, targets=targets, events=events,
              qdel={"returncode": result.returncode, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]})


# -- job_annotate -------------------------------------------------------------------------------------------------

VERDICTS = {"unreviewed", "verified_success", "verified_failure"}


def _adopt(settings: Settings, store: Store, job_id: str, args: dict) -> dict:
    pbs = Pbs(settings)
    details = pbs.detail([job_id])
    history = False
    if not details:
        details, history = pbs.detail([job_id], history=True), True
    key, fields = _match(details, job_id)
    if fields is None:
        raise MiyabiError("not_found", f"PBS does not know job {job_id}")
    owner = parse.variable_list(fields.get("Variable_List")).get("MIYABI_PROJECT_DIR") or index_owner(settings, key)
    if owner and owner != str(store.project_dir):
        raise MiyabiError("owned_elsewhere", f"job {key} belongs to project {owner}")
    number = job_num(key)
    select = fields.get("Resource_List.select", "")
    walltime_s = hms_to_seconds(fields.get("Resource_List.walltime"))
    mig = "nvnodes-mig" in fields.get("exec_vnode", "") or fields.get("queue", "").endswith("mig")
    units = int(fields.get("Resource_List.nodect") or 1)
    record = {
        "schema_version": 1, "submit_id": f"adopted-{number}", "job_id": key, "job_num": number, "origin": "adopted",
        "status": "adopted", "project_dir": str(store.project_dir), "system": None,
        "name": fields.get("Job_Name"), "labels": args.get("labels") or {}, "client_ref": args.get("client_ref"),
        "request_key": None, "seq": 0, "state": None, "usage": None, "cancel_requested": False, "notes": [],
        "verdict": {"status": "unreviewed"}, "notifications": [],
        "submit": {"queue": fields.get("queue"), "select": select, "units": units,
                   "unit": "mig_instance_hours" if mig else "node_hours", "walltime_s": walltime_s,
                   "submitted_at": pbs_time(fields.get("qtime")), "adopted_at": now_iso(),
                   "submit_arguments": fields.get("Submit_arguments"),
                   "output": (fields.get("Output_Path") or "").split(":", 1)[-1] or None, "env_names": []},
    }
    store.new_submission(record)
    store.bind(record["submit_id"], key)
    index_append(settings, {"type": "adopted", "job_id": key, "project_dir": str(store.project_dir),
                            "submit_id": record["submit_id"], "name": record["name"]})
    observation = states.from_detail(fields, history=history, cancel_requested=False,
                                     mig_hardware=settings.config.get("mig_hardware"))
    store.apply(key, observation, "qstat -H -f" if history else "qstat -f")
    return store.find(key)


def job_annotate(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    register_project(settings)
    job_id = args.get("job_id")
    if not job_id:
        raise MiyabiError("invalid_arguments", "job_id is required")
    record = store.find(job_id)
    adopted = False
    if record is None:
        if not args.get("adopt"):
            raise MiyabiError("not_in_ledger", f"job {job_id} is not in this project's ledger; pass adopt=true")
        record = _adopt(settings, store, job_id, args)
        adopted = True
    verdict = args.get("verdict")
    if verdict and verdict.get("status") not in VERDICTS:
        raise MiyabiError("invalid_arguments", f"verdict.status must be one of {sorted(VERDICTS)}")
    labels = args.get("labels") or {}

    def change(current: dict) -> None:
        current.setdefault("labels", {}).update(labels)
        if args.get("note"):
            current.setdefault("notes", []).append({"at": now_iso(), "text": args["note"]})
        if verdict:
            current["verdict"] = {**verdict, "at": now_iso()}

    if labels or args.get("note") or verdict:
        record = store.update(record["job_id"], change)
        store.note(record["job_id"], "annotated", "annotation",
                   note=args.get("note") or (verdict or {}).get("status") or "labels")
    return ok(adopted=adopted, record=record)


# -- ledger_query -------------------------------------------------------------------------------------------------

def _selected(record: dict, args: dict) -> bool:
    labels = args.get("labels") or {}
    if any((record.get("labels") or {}).get(k) != v for k, v in labels.items()):
        return False
    if args.get("client_ref") and record.get("client_ref") != args["client_ref"]:
        return False
    if args.get("job_ids") and job_num(record.get("job_id") or "") not in {job_num(i) for i in args["job_ids"]}:
        return False
    states_wanted = set(args.get("state") or [])
    if states_wanted and (record.get("state") or {}).get("execution_state") not in states_wanted:
        return False
    submitted = (record.get("submit") or {}).get("submitted_at") or ""
    if args.get("since") and submitted < args["since"]:
        return False
    if args.get("until") and submitted > args["until"]:
        return False
    return True


def ledger_query(settings: Settings, args: dict) -> dict:
    store = Store(settings)
    fmt = args.get("format", "records")
    if args.get("refresh", True):
        ledger_records = [r for r in store.records() if r.get("job_id")]
        snap = observe.snapshot(settings, max_age=settings.config["interactive_max_age_s"],
                                newer_than=observe.accepted_after(ledger_records))
        observe.refresh(store, ledger_records, snap)
    records = [r for r in store.records() if _selected(r, args)]
    if fmt == "records":
        return ok(project=str(store.project_dir), count=len(records), records=records)
    if fmt == "summary":
        totals: dict = {"by_state": Counter(), "by_outcome": Counter(), "requested": Counter(), "used": Counter()}
        for record in records:
            state = record.get("state") or {}
            totals["by_state"][state.get("execution_state") or record.get("status")] += 1
            if state.get("execution_state") == "ended":
                totals["by_outcome"][state.get("outcome")] += 1
            submit = record.get("submit") or {}
            if submit.get("unit") and submit.get("requested_unit_hours") is not None:
                totals["requested"][submit["unit"]] += submit["requested_unit_hours"]
            usage = record.get("usage") or {}
            if usage.get("unit"):
                totals["used"][usage["unit"]] += usage["quantity"]
        return ok(project=str(store.project_dir), count=len(records),
                  summary={key: {k: round(v, 6) if isinstance(v, float) else v for k, v in counter.items()}
                           for key, counter in totals.items()})
    if fmt not in {"events", "smooth-research"}:
        raise MiyabiError("invalid_arguments", "format must be records, summary, events or smooth-research")
    wanted = {r["job_id"] for r in records if r.get("job_id")}
    events, cursor = store.events(args.get("cursor"), args.get("limit"))
    events = [e for e in events if e["job_id"] in wanted]
    if fmt == "events":
        return ok(project=str(store.project_dir), events=events, next_cursor=cursor)
    observations = [o for o in (export.observation(e) for e in events) if o]
    from .notify import export_command

    result = ok(project=str(store.project_dir), observations=observations, next_cursor=cursor,
                export_command=export_command(settings, store.project_dir))
    if args.get("include_token"):
        result["token_usages"] = [u for u in (export.token_usage(e) for e in events) if u]
    return result
