"""MCP server exposing LocalDesk IT service desk tools via FastMCP."""

import json
import sqlite3
import sys
from pathlib import Path

# Ensure app package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

from app.config import DATABASE_PATH
from app.tools import assets, incidents, knowledge, sla

mcp = FastMCP(
    "localdesk",
    instructions=(
        "LocalDesk IT Service Desk. Tools for managing incidents, "
        "looking up SLAs and assets, searching the knowledge base, "
        "and viewing a dashboard summary."
    ),
)


@mcp.tool()
def check_sla(customer_tier: str, priority: str) -> str:
    """Look up contractual SLA response and resolution times for a given customer tier (gold/silver/bronze) and priority level (critical/high/medium/low)."""
    return sla.check_sla(customer_tier=customer_tier, priority=priority)


@mcp.tool()
def create_incident(summary: str, priority: str, category: str, reporter_name: str) -> str:
    """Create a new incident ticket. Priority: critical/high/medium/low. Category: network/hardware/software/access/other."""
    return incidents.create_incident(
        summary=summary, priority=priority, category=category, reporter_name=reporter_name
    )


@mcp.tool()
def lookup_asset(employee_id: str) -> str:
    """Look up hardware and software assets assigned to an employee by their employee ID (e.g. EMP-001)."""
    return assets.lookup_asset(employee_id=employee_id)


@mcp.tool()
def escalate_ticket(ticket_id: str, reason: str) -> str:
    """Escalate an existing incident ticket. Updates its status to escalated and logs the reason."""
    return incidents.escalate_ticket(ticket_id=ticket_id, reason=reason)


@mcp.tool()
def list_incidents(status: str = "open", limit: int = 10) -> str:
    """List incident tickets newest first. status: open/escalated/resolved/all."""
    return incidents.list_incidents(status=status, limit=limit)


@mcp.tool()
def get_incident(ticket_id: str) -> str:
    """Details and status of one ticket by id (e.g. INC-00003)."""
    return incidents.get_incident(ticket_id=ticket_id)


@mcp.tool()
def audit_report(hours: int = 24, limit: int = 5) -> str:
    """This assistant's own guardrail audit: trigger counts and recent flagged interactions."""
    from app.tools.audit_report import audit_report as _report
    return _report(hours=hours, limit=limit)


@mcp.tool()
def search_kb(query: str) -> str:
    """Search the knowledge base for relevant articles, troubleshooting steps, policies, and procedures."""
    try:
        return knowledge.search_kb(query=query)
    except Exception as e:
        return json.dumps({"error": f"Knowledge base unavailable: {e}"})


def _gated(name: str, args: dict, handler) -> str:
    """Server actions go through the same human gate as the web app. This
    process has no operator screen, so held actions come back "not executed"
    (the gate fails closed) — only the direct tier works over MCP."""
    from app.approvals import run_tool

    result, _ = run_tool(name, args, handler, session_id="mcp", channel="mcp")
    return result


@mcp.tool()
def list_servers(status: str = "active") -> str:
    """List the server fleet: role, size, site, status, monthly cost. status: active/all."""
    from app.tools import servers
    return servers.list_servers(status=status)


@mcp.tool()
def provision_server(size: str = "small", role: str = "app", site: str = "frankfurt") -> str:
    """Order a new server. small runs directly; medium/large need a human approval in the web UI."""
    from app.tools import servers
    return _gated("provision_server", {"size": size, "role": role, "site": site},
                  lambda a: servers.provision_server(**a))


@mcp.tool()
def reboot_server(server_name: str, reason: str) -> str:
    """Reboot a server. Needs a human approval in the web UI (not available on this channel)."""
    from app.tools import servers
    return _gated("reboot_server", {"server_name": server_name, "reason": reason},
                  lambda a: servers.reboot_server(**a))


@mcp.tool()
def terminate_server(server_name: str, reason: str) -> str:
    """Terminate a server. Needs a human approval in the web UI (not available on this channel)."""
    from app.tools import servers
    return _gated("terminate_server", {"server_name": server_name, "reason": reason},
                  lambda a: servers.terminate_server(**a))


@mcp.tool()
def migrate_server(server_name: str, target_site: str, reason: str) -> str:
    """Move a server to frankfurt/vienna/bratislava. Needs a human approval in the web UI (not available on this channel)."""
    from app.tools import servers
    return _gated("migrate_server", {"server_name": server_name, "target_site": target_site, "reason": reason},
                  lambda a: servers.migrate_server(**a))


@mcp.tool()
def get_dashboard_summary() -> str:
    """Get a summary of all incidents — counts by status, by priority, and the 5 most recent tickets."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

        by_status = {}
        for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM incidents GROUP BY status"):
            by_status[row["status"]] = row["cnt"]

        by_priority = {}
        for row in conn.execute("SELECT priority, COUNT(*) AS cnt FROM incidents GROUP BY priority"):
            by_priority[row["priority"]] = row["cnt"]

        recent = []
        for row in conn.execute(
            "SELECT ticket_id, summary, priority, status, created_at "
            "FROM incidents ORDER BY created_at DESC LIMIT 5"
        ):
            recent.append(dict(row))
    finally:
        conn.close()

    lines = [f"=== Dashboard Summary ({total} total incidents) ===", ""]

    lines.append("By Status:")
    for status, count in by_status.items():
        lines.append(f"  {status}: {count}")

    lines.append("")
    lines.append("By Priority:")
    for priority, count in by_priority.items():
        lines.append(f"  {priority}: {count}")

    if recent:
        lines.append("")
        lines.append("Recent Incidents:")
        for r in recent:
            lines.append(f"  [{r['ticket_id']}] {r['summary']} ({r['priority']}, {r['status']})")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
