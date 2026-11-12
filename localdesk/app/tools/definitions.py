"""OpenAI-format tool definitions for the service desk agent."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_sla",
            "description": "Look up contractual SLA response and resolution times for a given customer tier and priority level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_tier": {
                        "type": "string",
                        "enum": ["gold", "silver", "bronze"],
                        "description": "The customer's service tier.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "description": "The incident priority level.",
                    },
                },
                "required": ["customer_tier", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_incident",
            "description": "Create a new incident ticket in the system. Use this when a user reports an IT issue that needs tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Brief description of the incident.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "description": "Priority level of the incident.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["network", "hardware", "software", "access", "other"],
                        "description": "Category of the incident.",
                    },
                    "reporter_name": {
                        "type": "string",
                        "description": "Name of the person reporting the issue.",
                    },
                },
                "required": ["summary", "priority", "category", "reporter_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_asset",
            "description": "Look up hardware and software assets assigned to an employee by their employee ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "The employee's ID (e.g., EMP-001).",
                    },
                },
                "required": ["employee_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_ticket",
            "description": "Escalate an existing incident ticket. Updates its status to escalated and logs the reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID to escalate (e.g., INC-00001).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for escalation.",
                    },
                },
                "required": ["ticket_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_incidents",
            "description": "List incident tickets in the system (newest first). Use when the user asks what is open, what tickets exist, the current queue, or recent incidents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "escalated", "resolved", "all"],
                        "description": "Which tickets to list (default open).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tickets to return (default 10, max 25).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incident",
            "description": "Get the details and current status of one incident ticket by its id (e.g. INC-00003).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "The ticket id, e.g. INC-00003."},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_report",
            "description": "Read this assistant's own security audit log: counts of guardrail triggers (injection attempts, PII redactions, unsourced SLA numbers, judge flags/blocks, voice channel) and the most recent flagged interactions. Use when asked what the guardrails blocked or flagged, how many attacks there were, or what the security layers did.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Lookback window in hours (default 24)."},
                    "limit": {"type": "integer", "description": "How many recent flagged interactions to include (default 5)."},
                },
                "required": [],
            },
        },
    },
    # ── Server fleet ────────────────────────────────────────────────
    # list_servers and small provisioning run directly. The rest is held by
    # app/approvals.py until a human confirms on screen; the descriptions tell
    # the model so it can announce that instead of asking "are you sure?".
    {
        "type": "function",
        "function": {
            "name": "list_servers",
            "description": "List the servers in the fleet with role, size, site, status and monthly cost. Read-only, runs directly. Use it to answer what is running and to find a server's exact name before acting on it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "all"],
                        "description": "active (default) hides terminated servers; all includes them.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "provision_server",
            "description": "Order a new server. Size small runs directly (within the agent's own spending limit). Sizes medium and large are HELD for human approval: a confirmation window opens on the operator's screen and the call returns only after they click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["app", "db", "mail", "file", "gateway", "test"],
                        "description": "What the server is for (default app).",
                    },
                    "size": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "small = 2 vCPU / 4 GB, medium = 4 vCPU / 16 GB, large = 8 vCPU / 32 GB (default small).",
                    },
                    "site": {
                        "type": "string",
                        "enum": ["frankfurt", "vienna", "bratislava"],
                        "description": "Data centre (default frankfurt).",
                    },
                },
                "required": ["size"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reboot_server",
            "description": "Reboot a server. HELD for human approval: a confirmation window opens on the operator's screen and the call returns only after they click Approve or Deny. The result says whether it was executed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string", "description": "Exact server name, e.g. srv-db-01."},
                    "reason": {"type": "string", "description": "Why, including the ticket id if there is one."},
                },
                "required": ["server_name", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminate_server",
            "description": "Terminate (kill, shut down for good, decommission) a server. Irreversible. HELD for human approval: a confirmation window opens on the operator's screen and the call returns only after they click Approve or Deny. The result says whether it was executed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string", "description": "Exact server name, e.g. srv-app-02."},
                    "reason": {"type": "string", "description": "Why, including the ticket id if there is one."},
                },
                "required": ["server_name", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "migrate_server",
            "description": "Move (transfer, migrate) a server to another data centre. HELD for human approval: a confirmation window opens on the operator's screen and the call returns only after they click Approve or Deny. The result says whether it was executed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string", "description": "Exact server name, e.g. srv-mail-01."},
                    "target_site": {
                        "type": "string",
                        "enum": ["frankfurt", "vienna", "bratislava"],
                        "description": "Where to move it.",
                    },
                    "reason": {"type": "string", "description": "Why, including the ticket id if there is one."},
                },
                "required": ["server_name", "target_site", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "Search the knowledge base for relevant articles and documentation. Use this to find answers to IT questions, troubleshooting steps, policies, and procedures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query describing what information is needed.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]
