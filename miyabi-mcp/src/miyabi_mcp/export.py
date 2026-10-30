"""Exports derived only from stored events, so the same event always exports to the same bytes (11.2)."""

from __future__ import annotations

from .ledger import EXPORTED_KINDS

NAMESPACE = "miyabi-pbs"


def observation(event: dict) -> dict | None:
    """One ledger event -> a smooth-research Observation, or None for kinds that are not exported."""
    if event.get("kind") not in EXPORTED_KINDS:
        return None
    job_id = event["job_id"]
    result = {
        "record_id": event["event_id"],
        "source": "miyabi-mcp",
        "observed_at": event["observed_at"],
        "source_sequence": event["seq"],
        "execution_state": event["execution_state"],
        "outcome": event["outcome"],
        "raw_state": event["raw_state"],
        "source_ref": f"{event['source']} {job_id} @{event['observer']}",
        "execution_ref": {"kind": "hpc_job", "executor_namespace": NAMESPACE, "opaque_id": job_id,
                          "locator": event["locator"]},
    }
    if event.get("client_ref"):
        result["attempt_id"] = event["client_ref"]
    if event.get("pbs_time"):
        result["occurred_at"] = event["pbs_time"]
    if event.get("correction_of"):
        result["correction_of"] = event["correction_of"]
        result["note"] = event.get("note")
    if event.get("execution_state") == "ended" and event.get("usage"):
        result["usage"] = {"accounting_ref": f"{NAMESPACE}:{job_id}", **event["usage"]}
    return result


def token_usage(event: dict) -> dict | None:
    """The site TOKEN of an ended job as a separate, reported usage record (never mixed with node·h)."""
    if event.get("execution_state") != "ended" or event.get("token") is None:
        return None
    return {"accounting_ref": f"{NAMESPACE}:{event['job_id']}:token", "quantity": event["token"],
            "unit": "miyabi_token", "resource_type": "site_allocation", "basis": "reported",
            "source_ref": f"TOKEN in {event['source']} {event['job_id']} @{event['observer']}"}
