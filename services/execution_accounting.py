from __future__ import annotations


BLOCKING_EXECUTION_STATUSES = {
    "blocked",
    "rejected",
    "failed",
    "skipped",
    "pending",
    "protective_order_missing",
}

SUCCESS_EXECUTION_STATUSES = {
    "executed",
    "executed_paper",
    "filled",
    "closed",
    "protective_order_missing_closed",
}


def execution_record_status(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    candidates = [
        item.get("status"),
        item.get("decision_status"),
        _nested(item, "receipt", "status"),
        _nested(item, "paper_order", "status"),
    ]
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    return ""


def execution_record_id(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    candidates = [
        item.get("ticket_id"),
        _nested(item, "ticket", "ticket_id"),
        item.get("order_id"),
        item.get("request_id"),
        _nested(item, "paper_order", "order_id"),
    ]
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def counts_as_executed_record(item: dict, *, legacy_count_missing_status: bool = False) -> bool:
    status = execution_record_status(item)
    if status in BLOCKING_EXECUTION_STATUSES:
        return False
    if status in SUCCESS_EXECUTION_STATUSES:
        return True
    return bool(legacy_count_missing_status and execution_record_id(item))


def executed_record_ids(rows: list, *, legacy_count_missing_status: bool = False) -> set[str]:
    ids: set[str] = set()
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        if not counts_as_executed_record(item, legacy_count_missing_status=legacy_count_missing_status):
            continue
        record_id = execution_record_id(item)
        if record_id:
            ids.add(record_id)
    return ids


def _nested(item: dict, parent: str, key: str):
    value = item.get(parent)
    if isinstance(value, dict):
        return value.get(key)
    return None
