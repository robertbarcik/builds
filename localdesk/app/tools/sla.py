"""SLA lookup tool implementation."""

import json

SLA_DATA = {
    "gold": {
        "service_hours": "24/7/365",
        "uptime_guarantee": "99.95%",
        "dedicated_account_manager": True,
        "breach_penalty": "10% monthly fee credit per missed target, capped at 30%",
        "priorities": {
            "critical": {"response_time": "15 minutes", "resolution_time": "4 hours", "escalation_threshold": "Immediate to on-call lead"},
            "high": {"response_time": "30 minutes", "resolution_time": "8 hours", "escalation_threshold": "After 2 hours without progress"},
            "medium": {"response_time": "2 hours", "resolution_time": "24 hours", "escalation_threshold": "After 8 hours without progress"},
            "low": {"response_time": "4 hours", "resolution_time": "72 hours", "escalation_threshold": "After 24 hours without progress"},
        },
    },
    "silver": {
        "service_hours": "Monday-Friday, 07:00-20:00 CET",
        "uptime_guarantee": "99.9%",
        "dedicated_account_manager": False,
        "breach_penalty": "5% monthly fee credit per missed target, capped at 15%",
        "priorities": {
            "critical": {"response_time": "1 hour", "resolution_time": "8 hours", "escalation_threshold": "After 2 hours without progress"},
            "high": {"response_time": "2 hours", "resolution_time": "16 hours", "escalation_threshold": "After 4 hours without progress"},
            "medium": {"response_time": "4 hours", "resolution_time": "48 hours", "escalation_threshold": "After 16 hours without progress"},
            "low": {"response_time": "8 hours", "resolution_time": "5 business days", "escalation_threshold": "After 48 hours without progress"},
        },
    },
    "bronze": {
        "service_hours": "Monday-Friday, 08:00-17:00 CET",
        "uptime_guarantee": "99.5%",
        "dedicated_account_manager": False,
        "breach_penalty": "None (best-effort)",
        "priorities": {
            "critical": {"response_time": "4 hours", "resolution_time": "24 hours", "escalation_threshold": "After 8 hours without progress"},
            "high": {"response_time": "8 hours", "resolution_time": "48 hours", "escalation_threshold": "After 16 hours without progress"},
            "medium": {"response_time": "1 business day", "resolution_time": "5 business days", "escalation_threshold": "After 2 business days"},
            "low": {"response_time": "2 business days", "resolution_time": "10 business days", "escalation_threshold": "After 5 business days"},
        },
    },
}


def check_sla(customer_tier: str, priority: str) -> str:
    tier = customer_tier.lower()
    prio = priority.lower()
    if tier not in SLA_DATA:
        return json.dumps({"error": f"Unknown tier: {customer_tier}. Valid tiers: gold, silver, bronze"})
    tier_data = SLA_DATA[tier]
    if prio not in tier_data["priorities"]:
        return json.dumps({"error": f"Unknown priority: {priority}. Valid priorities: critical, high, medium, low"})
    prio_data = tier_data["priorities"][prio]
    result = {
        "customer_tier": tier.title(),
        "priority": prio.title(),
        "response_time": prio_data["response_time"],
        "resolution_time": prio_data["resolution_time"],
        "escalation_threshold": prio_data["escalation_threshold"],
        "service_hours": tier_data["service_hours"],
        "uptime_guarantee": tier_data["uptime_guarantee"],
        "breach_penalty": tier_data["breach_penalty"],
    }
    return json.dumps(result)
