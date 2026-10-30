"""Parsers for Miyabi's qstat dialect and site commands. Unknown output fails instead of becoming empty."""

from __future__ import annotations

import math
import re
from datetime import datetime

from .util import MiyabiError, hms_to_seconds

BANNER = "Miyabi scheduled stop time:"
STOP = re.compile(r"Miyabi scheduled stop time:\s*(\d{4})/(\d{2})/(\d{2})\([^)]*\)\s+(\d{2}):(\d{2}):(\d{2})")
EMPTY_MESSAGES = {"No unfinished job found.", "No finished job found.", "No matching job found."}
TABLE_HEADER = ("JOB_ID", "JOB_NAME", "STATUS", "PROJECT", "QUEUE", "START_DATE", "ELAPSE", "TOKEN", "NODE", "MIG")
JOB_ID = re.compile(r"[0-9]+(?:\[(?:[0-9]+(?:-[0-9]+)?)?\])?(?:\.[A-Za-z0-9_.-]+)?")


def parse_error(message: str, text: str = "") -> MiyabiError:
    return MiyabiError("parse_error", message, output=text[-4096:])


def notices(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith(BANNER)]


def scheduled_stop(text: str) -> str | None:
    match = STOP.search(text)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    return datetime(year, month, day, hour, minute, second).astimezone().isoformat()


# -- job tables: qstat [-H] -ll ------------------------------------------------------------------------------

def _integer(value: str) -> int | None:
    if value == "-":
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("invalid node or MIG count")
    return int(value)


def _token(value: str) -> tuple[float | None, float | None]:
    """(usage, estimate): parentheses mark a not-yet-used amount, as on queued jobs."""
    if value == "-":
        return None, None
    estimate = value.startswith("(") and value.endswith(")")
    number = value[1:-1] if estimate else value
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", number):
        raise ValueError("invalid token value")
    result = float(number.replace(",", ""))
    if not math.isfinite(result):
        raise ValueError("non-finite token value")
    return (None, result) if estimate else (result, None)


def _elapsed(value: str) -> int | None:
    if value in {"-", "--:--:--"}:
        return None
    seconds = hms_to_seconds(value)
    if seconds is None:
        raise ValueError("invalid elapsed time")
    return seconds


def _start_date(value: str) -> str | None:
    if value in {"-", "--:--:--", "--/-- --:--:--"}:
        return None
    date = value[1:-1] if value.startswith("(") and value.endswith(")") else value
    if not re.fullmatch(r"[0-9]{2}/[0-9]{2} [0-9]{2}:[0-5][0-9]:[0-5][0-9]", date):
        raise ValueError("invalid start date")
    return value  # no year or zone is printed; parentheses mark a scheduler estimate


def _row(line: str, name_start: int, status_start: int) -> dict:
    identifier = line[:name_start].strip()
    name = line[name_start:status_start].strip()
    if not JOB_ID.fullmatch(identifier) or not name:
        raise ValueError("invalid job ID or missing job name")
    cells = line[status_start:].split()
    if len(cells) not in (8, 9) or not re.fullmatch(r"[A-Z_]+", cells[0]):
        raise ValueError("unexpected job columns")
    status, project, queue = cells[:3]
    elapsed, token, nodes, mig = cells[-4:]
    usage, estimate = _token(token)
    return {
        "job_id": identifier, "job_name": name, "status": status, "project": project, "queue": queue,
        "start_date": _start_date(" ".join(cells[3:-4])), "elapsed_seconds": _elapsed(elapsed),
        "token": usage, "token_estimate": estimate, "nodes": _integer(nodes), "mig": _integer(mig),
    }


def parse_table(text: str) -> list[dict]:
    """Rows of `qstat [-H] -ll`; an explicit empty-job message gives []."""
    jobs: list[dict] = []
    layout = None
    empty = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(BANNER):
            continue
        if stripped in EMPTY_MESSAGES:
            if layout is not None or empty:
                raise parse_error(f"line {number}: contradictory empty-job message", text)
            empty = True
            continue
        if tuple(stripped.split()) == TABLE_HEADER:
            if empty:
                raise parse_error(f"line {number}: job table after empty-job message", text)
            layout = (line.index("JOB_NAME"), line.index("STATUS"))
            continue
        if layout is None:
            raise parse_error(f"line {number}: expected a job table or an empty-job message", text)
        try:
            jobs.append(_row(line, *layout))
        except ValueError as exc:
            raise parse_error(f"line {number}: {exc}", text) from None
    if layout is None and not empty:
        raise parse_error("no job table or empty-job message found", text)
    return jobs


# -- job records: qstat [-H] -f ------------------------------------------------------------------------------

def parse_detail(text: str) -> dict[str, dict[str, str]]:
    """`qstat -f` blocks keyed by full job ID; values wrapped onto tab-indented lines are joined back."""
    joined = text.replace("\n\t", "")
    records: dict[str, dict[str, str]] = {}
    for block in re.split(r"^Job Id:\s*", joined, flags=re.MULTILINE)[1:]:
        job_id = block.split(None, 1)[0].strip()
        fields = dict(re.findall(r"^    ([A-Za-z_][\w.\-]*) = (.*)$", block, re.MULTILINE))
        records[job_id] = fields
    return records


def variable_list(value: str | None) -> dict[str, str]:
    """`Variable_List` as a dict. Values containing commas are not split reliably; only exact names matter here."""
    result = {}
    for item in (value or "").split(","):
        if "=" in item:
            key, _, val = item.partition("=")
            result[key.strip()] = val
    return result


# -- site tables ----------------------------------------------------------------------------------------------

RSC_ROW = re.compile(r"^(\s*(?:\|--|`--)\s*)?(\S+)\s+\[([^\]]*)\]\s+(.*)$")


def parse_rsc(text: str) -> list[dict]:
    """`qstat --rsc -x`: one entry per queue; routing parents list their destinations in `routes`."""
    queues: list[dict] = []
    system = None
    has_mig = False
    parent: dict | None = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(BANNER):
            continue
        if stripped.startswith("SYSTEM:"):
            system, parent = stripped.split(":", 1)[1].strip(), None
            continue
        if stripped.split()[0] == "QUEUE":
            has_mig, parent = "AVAIL_MIG" in stripped.split(), None
            continue
        match = RSC_ROW.match(line)
        if match:
            child, name, status, rest = match.groups()
            cells = rest.split()
            try:
                min_nodes, max_nodes = int(cells[0]), int(cells[1])
                index = 2
                mig_sizes = None
                if has_mig:
                    mig_sizes = [int(size) for size in cells[2].split(",")]
                    index = 3
                max_elapse, remain_elapse = hms_to_seconds(cells[index]), hms_to_seconds(cells[index + 1])
                mem_gib = float(cells[index + 2])
                projects = [p for p in ",".join(cells[index + 3:]).split(",") if p]
            except (IndexError, ValueError):
                raise parse_error(f"line {number}: unexpected queue columns", text) from None
            words = [word.strip() for word in status.split(",")]
            entry = {
                "system": system, "name": name, "parent": parent["name"] if child and parent else None,
                "status": status, "enabled": "ENABLE" in words, "started": "START" in words,
                "min_nodes": min_nodes, "max_nodes": max_nodes, "mig_sizes": mig_sizes, "mig": has_mig,
                "max_elapse_s": max_elapse, "remain_elapse_s": remain_elapse, "mem_gib": mem_gib,
                "projects": projects,
            }
            if child:
                if parent is None:
                    raise parse_error(f"line {number}: routing destination without a parent queue", text)
                parent["routes"].append(name)
            else:
                parent = None
            queues.append(entry)
        elif re.fullmatch(r"\S+", stripped) and not line.startswith(" "):
            parent = {"system": system, "name": stripped, "routing": True, "routes": [], "mig": has_mig}
            queues.append(parent)
        else:
            raise parse_error(f"line {number}: unexpected queue line", text)
    if not queues:
        raise parse_error("no queues found", text)
    return queues


def parse_limit(text: str) -> list[dict]:
    """`qstat --limit`: used/limit pairs per system and project."""
    rows: list[dict] = []
    system = None
    columns: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(BANNER):
            continue
        if stripped.startswith("SYSTEM:"):
            system = stripped.split(":", 1)[1].strip()
            continue
        cells = stripped.split()
        if cells[0] == "PROJECT":
            columns = [cell.lower() for cell in cells[1:]]
            continue
        if not columns or len(cells) != len(columns) + 1:
            raise parse_error(f"line {number}: unexpected limit row", text)
        row = {"system": system, "project": cells[0]}
        for column, cell in zip(columns, cells[1:]):
            match = re.fullmatch(r"(\d+)/(\d+)", cell)
            if not match:
                raise parse_error(f"line {number}: unexpected limit value {cell!r}", text)
            row[column] = {"used": int(match.group(1)), "limit": int(match.group(2))}
        rows.append(row)
    if not rows:
        raise parse_error("no limits found", text)
    return rows


RSCUSE_ROW = re.compile(r"^(\S+)\s+[*\-]+\s+(\d+)%\s+(\d+)/\s*(\d+)\s*$")


def parse_rscuse(text: str) -> list[dict]:
    """`qstat --rscuse`: occupancy per resource pool (context only, not a start-time prediction)."""
    rows: list[dict] = []
    system = None
    unit = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(BANNER):
            continue
        if stripped.startswith("SYSTEM:"):
            system = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("QUEUE"):
            match = re.search(r"\((\w+)\)", stripped)
            unit = match.group(1).lower() if match else None
            continue
        match = RSCUSE_ROW.match(stripped)
        if not match:
            raise parse_error(f"line {number}: unexpected occupancy row", text)
        pool, ratio, used, total = match.groups()
        rows.append({"system": system, "pool": pool, "unit": unit, "ratio_percent": int(ratio),
                     "used": int(used), "total": int(total)})
    if not rows:
        raise parse_error("no occupancy rows found", text)
    return rows


def _number(value: str) -> float | None:
    value = value.rstrip("%")
    if value in {"-", ""}:
        return None
    return float(value.replace(",", ""))


def parse_show_token(text: str) -> list[dict]:
    """`show_token`: the group's budget row and its members' usage rows, in hours."""
    rows: list[dict] = []
    project = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or set(line.strip()) == {"-"} or line.startswith("Project"):
            continue
        cells = line.split()
        try:
            if line[0].isspace():
                user, limit, used, percent, max_c, max_g, max_mig, expiry = cells
                rows.append({"project": project, "user": user, "used_hours": _number(used),
                             "used_percent": _number(percent), "expiry": expiry})
            else:
                project, _user, limit, used, percent, max_c, max_g, max_mig, expiry = cells
                rows.append({"project": project, "user": None, "limit_hours": _number(limit),
                             "used_hours": _number(used), "used_percent": _number(percent),
                             "max_nodes_c": _number(max_c), "max_nodes_g": _number(max_g),
                             "max_mig": _number(max_mig), "expiry": expiry})
        except ValueError:
            raise parse_error(f"line {number}: unexpected token row", text) from None
    if not rows:
        raise parse_error("no token rows found", text)
    return rows
