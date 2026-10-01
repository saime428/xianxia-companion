"""Local operational command: request/check/release drain; never restarts a service."""
import argparse
import json
import os
import sqlite3
import time


def set_drain(conn, value):
    conn.execute("INSERT INTO app_runtime_state(key,value,updated_at) VALUES('deployment_drain',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (value, time.time()))
    conn.commit()


def active_flows(conn):
    result = []
    for key, value in conn.execute("SELECT key,value FROM app_runtime_state WHERE key LIKE 'runtime_inflight:%' AND value<>''"):
        try:
            flow = json.loads(value)
        except (ValueError, TypeError):
            result.append({"flow": key, "state": "invalid"})
            continue
        # Never infer quiescence from a timeout: only an absent OS process is stale.
        pid = int(flow.get("pid") or 0)
        if os.name != "nt" and pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
        result.append(flow)
    return result


def unsupported_legacy_work(conn):
    """Legacy direct-send schedulers have no drain admission/flow registration yet."""
    busy = {}
    for table in ("fanren_sessions", "sect_sessions"):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        cursor = conn.execute(f"SELECT * FROM {table}")  # table is a fixed internal name
        columns = [column[0] for column in cursor.description]
        for values in cursor:
            row = dict(zip(columns, values))
            enabled = any(value not in (None, 0, "0", "", False) for key, value in row.items()
                          if key == "enabled" or (key.startswith("auto_") and key.endswith("_enabled")))
            pending = any(value not in (None, 0, "0", "", "[]", "{}") for key, value in row.items()
                          if key.endswith(("_pending_commands", "_pending_msg_id", "_pending_reply_msg_id")))
            rift = str(row.get("rift_state") or "")
            pending = pending or "准备发送" in rift or "等待回包" in rift or row.get("yuanying_state") in {
                "状态检查中", "结算指令已发送", "出窍指令已发送"}
            if enabled or pending:
                busy[table] = busy.get(table, 0) + 1
    return busy


def reject_unsupported_work(busy):
    if busy:
        raise SystemExit("legacy direct-send work is not drain-aware; automatic deployment refused; "
                         "use an explicitly coordinated maintenance window: " + json.dumps(busy))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("action", choices=["request", "status", "release", "wait"])
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    conn = sqlite3.connect(args.database, timeout=30)
    if args.action in {"request", "wait"}:
        row = conn.execute("SELECT value FROM app_runtime_state WHERE key='telegram_runtime_status'").fetchone()
        status = json.loads(row[0]) if row else {}
        if "deployment_drain_v1" not in status.get("capabilities", []):
            raise SystemExit("running Telegram worker does not advertise drain support; automatic deployment refused")
        reject_unsupported_work(unsupported_legacy_work(conn))
        set_drain(conn, str(time.time()))
    elif args.action == "release":
        set_drain(conn, "0")
        return
    deadline, quiet_count = time.monotonic() + args.timeout, 0
    while True:
        active = active_flows(conn)
        busy = conn.execute("SELECT COUNT(*) FROM outgoing_commands WHERE status IN ('pending','sending','awaiting_confirm')").fetchone()[0]
        heart = conn.execute("SELECT COUNT(*) FROM companion_heart_tribulation_tasks WHERE enabled=1 AND workflow_state NOT IN ('','idle','failed_stopped')").fetchone()[0]
        unsupported = unsupported_legacy_work(conn)
        print(json.dumps({"flows": active, "commands": busy, "heart_tribulations": heart,
                          "unsupported_legacy": unsupported}, ensure_ascii=False), flush=True)
        if args.action != "wait":
            return
        reject_unsupported_work(unsupported)
        quiet_count = quiet_count + 1 if not active and not busy and not heart else 0
        if quiet_count >= 2:
            return
        if time.monotonic() >= deadline:
            raise SystemExit("drain timed out; no restart performed; drain remains set (release explicitly)")
        time.sleep(2)


if __name__ == "__main__":
    main()
