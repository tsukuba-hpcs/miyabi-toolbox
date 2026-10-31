#!/usr/bin/env python3
"""A fake Miyabi PBS for offline tests: qstat (site dialect), qsub, qdel and show_token.

Invoked through symlinks named after the command. Job state lives in the JSON file named by
FAKE_PBS_STATE; site tables come from the anonymized live fixtures next to this file.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BANNER = "Miyabi scheduled stop time: 2026/11/25(Wed) 09:00:00 (Remain: 25days 15:03:18)\n\n"
HEADER = ("JOB_ID            JOB_NAME                                                         STATUS    PROJECT    "
          "QUEUE           START_DATE       ELAPSE        TOKEN NODE MIG")
WORDS = {"Q": "QUEUED", "H": "HELD", "R": "RUNNING", "E": "EXITING", "F": "FINISH"}


def load():
    return json.loads(Path(os.environ["FAKE_PBS_STATE"]).read_text())


def save(state):
    path = Path(os.environ["FAKE_PBS_STATE"])
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream, indent=1)
    os.replace(tmp, path)


def stamp(ts):
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(ts)) if ts else None


def row(number, job):
    start = time.strftime("%m/%d %H:%M:%S", time.localtime(job["stime"])) if job.get("stime") else "--:--:--"
    elapse = job.get("used") or ("--:--:--" if not job.get("stime") else "00:00:10")
    token = "-" if job["state"] != "F" or not job.get("stime") else "0.2"
    nodes, mig = (str(job["units"]), "-") if not job.get("mig") else ("-", str(job["units"]))
    return (f"{number:<18}{job['name']:<65}{WORDS[job['state']]:<10}{job['project']:<11}{job['route']:<16}"
            f"{start:<17}{elapse:<14}{token:>5}{nodes:>5}{mig:>4}")


def block(number, job, server):
    vnode_kind = "nvnodes-mig" if job.get("mig") else "nvnodes-g"
    lines = [f"Job Id: {number}.{server}",
             f"    Job_Name = {job['name']}",
             f"    Job_Owner = {os.environ.get('USER', 'utest')}@miyabi-g1",
             f"    job_state = {job['state']}",
             f"    queue = {job['route']}",
             f"    server = {server}",
             f"    qtime = {stamp(job['qtime'])}",
             f"    Resource_List.nodect = {job['units']}",
             f"    Resource_List.select = {job['select']}",
             f"    Resource_List.walltime = {job['walltime']}",
             f"    Output_Path = miyabi-g1:{job['output']}"]
    if job.get("stime"):
        lines += [f"    stime = {stamp(job['stime'])}",
                  "    exec_host = mg0001/0*72",
                  f"    exec_vnode = (mg0001:mem=104857600kb:ncpus=72:ngpus=1:{vnode_kind}={job['units']})",
                  f"    resources_used.walltime = {job.get('used') or '00:00:10'}"]
    if job["state"] == "F":
        lines.append(f"    obittime = {stamp(job['obittime'])}")
        if job.get("exit_status") is not None:
            lines.append(f"    Exit_status = {job['exit_status']}")
        lines.append(f"    TOKEN = {'0.2' if job.get('stime') else '-'}")
    variables = ",".join(["PBS_O_HOST=miyabi-g1"] + [f"{k}={v}" for k, v in job["variables"].items()])
    lines.append(f"    Variable_List = {variables}")
    lines.append(f"    Submit_arguments = {job['submit_args']}")
    return "\n".join(lines) + "\n"


def qstat(args):
    state = load()
    server = state.get("server", "opbs")
    if "--rsc" in args:
        print((FIXTURES / "rsc_x.txt").read_text(), end="")
        return 0
    if "--limit" in args:
        text = (FIXTURES / "limit.txt").read_text()
        active = sum(1 for job in state["jobs"].values() if job["state"] != "F")
        print(text.replace("0/128", f"{active}/128", 1), end="")
        return 0
    if "--rscuse" in args:
        print((FIXTURES / "rscuse.txt").read_text(), end="")
        return 0
    history = "-H" in args
    detail = "-f" in args
    values = {index + 1 for index, a in enumerate(args) if a in ("--hday", "--hnum")}
    ids = [a.split(".")[0] for index, a in enumerate(args)
           if index not in values and re.fullmatch(r"\d+(\[\d*\])?(\.\S+)?", a)]
    jobs = state["jobs"]
    selected = [(n, j) for n, j in sorted(jobs.items()) if (j["state"] == "F") == history and (not ids or n in ids)]
    print(BANNER, end="")
    if detail:
        if not selected:
            print("No finished job found. " if history else "No unfinished job found. ")
            return 0
        print("\n".join(block(n, j, server) for n, j in selected))
        return 0
    if not selected:
        print("No finished job found. " if history else "No unfinished job found. ")
        return 0
    print(HEADER)
    for number, job in selected:
        print(row(number, job))
    return 0


def parse_v(text):
    result, current, quote = [], "", None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in "'\"":
            quote = char
        elif char == ",":
            result.append(current)
            current = ""
        else:
            current += char
    result.append(current)
    return dict(item.split("=", 1) for item in result if "=" in item)


def qsub(args):
    state = load()
    mode = state.get("qsub_mode", "ok")
    if mode == "fail":
        print("qsub: Unknown queue", file=sys.stderr)
        return 170
    interactive = "-I" in args
    words = [arg for arg in args if arg != "-I"]
    options = {"-v": "", "-W": [], "-l": []}
    for index in range(0, len(words) - 1, 2):
        flag, value = words[index], words[index + 1]
        if flag in ("-W", "-l"):
            options[flag].append(value)
        else:
            options[flag] = value
    script = "" if interactive else args[-1]
    limits = dict(item.split("=", 1) for item in options.get("-l", []))
    number = str(state["next_id"])
    state["next_id"] += 1
    select = limits.get("select", "1")
    queue = options.get("-q", "debug-g")
    mig = queue.endswith("-mig")
    route = {"regular-g": "small-g"}.get(queue, queue)
    depend = any(w.startswith("depend=") for w in options.get("-W", []))
    state["jobs"][number] = {
        "name": options.get("-N", Path(script).name), "state": "H" if depend else "Q", "queue": queue,
        "route": route, "project": next((w.split("=", 1)[1] for w in options.get("-W", [])
                                         if w.startswith("group_list=")), "gtest"),
        "select": select, "units": int(select.split(":")[0]), "mig": mig, "walltime": limits.get("walltime"),
        "qtime": time.time(), "stime": None, "obittime": None, "exit_status": None, "used": None,
        "output": options.get("-o", f"{os.getcwd()}/{number}.out"), "variables": parse_v(options.get("-v", "")),
        "submit_args": " ".join(shlex.quote(a) for a in args), "cwd": os.getcwd(), "script": script,
    }
    save(state)
    if interactive:
        if mode == "interactive_silent":
            while load()["jobs"][number]["state"] != "F":
                time.sleep(0.05)
            return 1
        print(f"qsub: waiting for job {number}.{state.get('server', 'opbs')} to start", flush=True)
        if mode == "interactive_queue":
            while load()["jobs"][number]["state"] != "F":
                time.sleep(0.05)
            return 1
        state = load()
        state["jobs"][number].update(state="R", stime=time.time())
        save(state)
        print(f"qsub: job {number}.{state.get('server', 'opbs')} ready", flush=True)
        env = dict(os.environ, **parse_v(options["-v"]), PBS_JOBID=f"{number}.{state.get('server', 'opbs')}", PS1="compute> ")
        result = subprocess.call(["/bin/bash", "--noprofile", "--norc", "-i"], env=env)
        state = load()
        state["jobs"][number].update(state="F", exit_status=result, obittime=time.time(), used="00:00:01")
        save(state)
        return result
    if mode == "garbage":
        print("qsub: connection reset")
        return 0
    print(f"{number}.{state.get('server', 'opbs')}")
    return 0


def qdel(args):
    state = load()
    code = 0
    for job_id in args:
        number = job_id.split(".")[0]
        job = state["jobs"].get(number)
        if job is None or job["state"] == "F":
            print(f"qdel: Unknown Job Id {job_id}", file=sys.stderr)
            code = 1
            continue
        if job.get("stime"):
            job.update(state="F", exit_status=271, obittime=time.time(), used=job.get("used") or "00:00:20")
        else:
            job.update(state="F", obittime=time.time())
    save(state)
    return code


def show_token(args):
    print((FIXTURES / "show_token.txt").read_text(), end="")
    return 0


if __name__ == "__main__":
    command = Path(sys.argv[0]).name
    sys.exit({"qstat": qstat, "qsub": qsub, "qdel": qdel, "show_token": show_token}[command](sys.argv[1:]))
