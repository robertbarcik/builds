"""Customers, sources and alert templates for the simulator.

Three fictional European customers and twenty monitoring / security / ITSM sources, each
speaking its own severity vocabulary and emitting a vendor-shaped `raw` block next to the
normalised fields. Vendor names are used for illustration only; everything is synthetic.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- customers

# host roles -> how many hosts each customer has in that role
HOST_ROLES: dict[str, int] = {
    "web": 3, "app": 4, "db": 2, "sql": 3, "k8s": 4, "batch": 3, "win": 8, "dc": 2,
    "nas": 2, "backup": 1, "core": 2, "access": 6, "branch": 5, "fw": 2, "esx": 4, "sap": 2,
}


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    short: str
    industry: str
    country: str
    prefix: str
    domain: str
    branches: tuple[str, ...]
    services: dict[str, str]          # host role -> business service name
    sources: tuple[str, ...]
    patch_weekday: int                # 0 = Monday; patch window starts 22:00 local that day
    patch_week_parity: int            # patch on ISO weeks where week % 2 == parity

    def hosts(self, role: str) -> list[str]:
        n = HOST_ROLES[role]
        p = self.prefix
        if role == "core":
            return [f"{p}-core-sw-{i:02d}" for i in range(1, n + 1)]
        if role == "access":
            return [f"{p}-acc-sw-{i:02d}" for i in range(1, n + 1)]
        if role == "branch":
            return [f"{p}-br-{city}-rt01" for city in self.branches[:n]]
        if role == "fw":
            return [f"{p}-fw-{i:02d}" for i in range(1, n + 1)]
        if role == "nas":
            return [f"{p}-nas-{i:02d}" for i in range(1, n + 1)]
        if role == "backup":
            return [f"{p}-vbr-01"]
        return [f"{p}-{role}-{i:02d}" for i in range(1, n + 1)]

    def service_for(self, role: str) -> str:
        return self.services.get(role, _DEFAULT_SERVICE.get(role, role))


_DEFAULT_SERVICE = {
    "core": "network", "access": "network", "branch": "branch-wan", "fw": "perimeter",
    "nas": "storage", "backup": "backup", "esx": "virtualisation", "win": "windows-servers",
    "dc": "active-directory", "batch": "batch", "k8s": "kubernetes",
}

CUSTOMERS: dict[str, Customer] = {
    c.id: c for c in [
        Customer(
            id="nordhafen", name="Nordhafen Logistik GmbH", short="Nordhafen",
            industry="Logistics", country="DE", prefix="nhl", domain="nordhafen-logistik.example",
            branches=("hamburg", "bremen", "rostock", "kiel", "luebeck"),
            services={"web": "tracking-portal", "app": "wms-api", "db": "wms-db", "sql": "sap-erp-db",
                      "k8s": "routing-engine", "sap": "sap-s4", "batch": "edi-batch"},
            sources=("dynatrace", "zabbix", "checkmk", "prtg", "scom", "veeam", "netapp", "controlm",
                     "sap_alm", "fortigate", "servicenow", "azure_monitor", "redgate", "vcf_ops",
                     "solarwinds", "defender_xdr", "sentinel", "alertmanager"),
            patch_weekday=2, patch_week_parity=0,
        ),
        Customer(
            id="valdorina", name="Banca Valdorina S.p.A.", short="Valdorina",
            industry="Regional banking", country="IT", prefix="bva", domain="bancavaldorina.example",
            branches=("verona", "padova", "trento", "vicenza", "bolzano"),
            services={"web": "online-banking", "app": "payments-api", "db": "core-ledger-db",
                      "sql": "core-banking-db", "k8s": "card-services", "sap": "sap-fi", "batch": "eod-batch"},
            sources=("dynatrace", "scom", "solarwinds", "redgate", "veeam", "netapp", "vcf_ops", "controlm",
                     "splunk_es", "sentinel", "crowdstrike", "fortigate", "servicenow", "azure_monitor",
                     "checkmk", "prtg", "zabbix"),
            patch_weekday=3, patch_week_parity=1,
        ),
        Customer(
            id="verdalia", name="Verdalia Foods S.L.", short="Verdalia",
            industry="Food retail", country="ES", prefix="vfd", domain="verdalia-foods.example",
            branches=("valencia", "zaragoza", "murcia", "alicante", "castellon"),
            services={"web": "webshop", "app": "checkout-service", "db": "orders-db", "sql": "pos-db",
                      "k8s": "checkout-service", "sap": "sap-retail", "batch": "replenishment-batch"},
            sources=("alertmanager", "dynatrace", "zabbix", "checkmk", "azure_monitor", "defender_xdr",
                     "crowdstrike", "fortigate", "veeam", "netapp", "servicenow", "sap_alm", "controlm",
                     "redgate", "prtg", "solarwinds", "scom"),
            patch_weekday=1, patch_week_parity=1,
        ),
    ]
}

# ---------------------------------------------------------------- render context


@dataclass
class Ctx:
    """Everything a raw-block builder needs for one event."""

    customer: Customer
    source_id: str
    check: str
    host: str
    service: str
    title: str
    native: str
    status: str
    ts: float                         # epoch seconds (simulated)
    nid: int                          # native id seed, shared by a firing and its resolve
    value: Optional[float] = None
    threshold: Optional[float] = None
    unit: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def iso(self) -> str:
        return iso(self.ts)

    @property
    def ms(self) -> int:
        return int(self.ts * 1000)

    @property
    def firing(self) -> bool:
        return self.status == "firing"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex(n: int, width: int = 16) -> str:
    return f"{(n * 2654435761) & ((1 << (4 * width)) - 1):0{width}X}"


# ---------------------------------------------------------------- vendor raw blocks

def _raw_dynatrace(c: Ctx) -> dict:
    impact = c.extra.get("impact", "SERVICES")
    etype = {"SERVICES": "SERVICE", "APPLICATION": "APPLICATION", "INFRASTRUCTURE": "HOST",
             "ENVIRONMENT": "ENVIRONMENT"}[impact]
    name = c.service if etype in ("SERVICE", "APPLICATION") else c.host
    raw = {
        "problemId": f"-{c.nid % 10**18}_{c.nid % 10**13}V2",
        "displayId": f"P-{c.nid % 900000 + 100000}",
        "title": c.extra.get("dt_title", c.title),
        "impactLevel": impact,
        "severityLevel": c.native,
        "status": "OPEN" if c.firing else "CLOSED",
        "affectedEntities": [{"entityId": {"id": f"{etype}-{_hex(c.nid)}", "type": etype}, "name": name}],
        "managementZones": [{"name": c.customer.short + " production"}],
        "startTime": c.ms,
    }
    if not c.firing:
        raw["endTime"] = c.ms
    if c.value is not None:
        raw["evidenceDetails"] = {"metric": c.check, "value": c.value, "threshold": c.threshold, "unit": c.unit}
    return raw


def _raw_zabbix(c: Ctx) -> dict:
    raw = {
        "eventid": str(c.nid % 10**8),
        "name": c.title,
        "severity": c.native if c.firing else c.extra.get("orig_native", c.native),
        "host": {"host": c.host, "name": c.host},
        "value": "1" if c.firing else "0",
        "status": "PROBLEM" if c.firing else "RESOLVED",
        "clock": int(c.ts),
        "trigger": {"expression": c.extra.get("expr", f"last(/{c.host}/{c.check})>0")},
        "tags": [{"tag": "customer", "value": c.customer.id}, {"tag": "service", "value": c.service}],
    }
    if c.value is not None:
        raw["opdata"] = f"Current: {c.value:g}{c.unit}"
    return raw


def _raw_checkmk(c: Ctx) -> dict:
    if c.extra.get("host_check"):
        return {
            "WHAT": "HOST", "HOSTNAME": c.host, "HOSTSTATE": c.native, "HOSTOUTPUT": c.extra.get("output", ""),
            "NOTIFICATIONTYPE": c.extra.get("ntype", "PROBLEM" if c.firing else "RECOVERY"),
            "SHORTDATETIME": c.iso, "OMD_SITE": c.customer.prefix + "_prod",
        }
    raw = {
        "WHAT": "SERVICE", "HOSTNAME": c.host, "SERVICEDESC": c.extra.get("svcdesc", c.check),
        "SERVICESTATE": c.native, "SERVICEOUTPUT": c.extra.get("output", ""),
        "NOTIFICATIONTYPE": c.extra.get("ntype", "PROBLEM" if c.firing else "RECOVERY"),
        "SHORTDATETIME": c.iso, "OMD_SITE": c.customer.prefix + "_prod",
    }
    if c.value is not None:
        raw["SERVICEPERFDATA"] = f"{c.check}={c.value:g}{c.unit};{c.threshold:g};{(c.threshold or 0) + 10:g}"
    return raw


def _raw_azure(c: Ctx) -> dict:
    sub = f"{c.nid % 10**8:08x}-0000-4000-8000-{c.customer.prefix}00000001"
    target = (f"/subscriptions/{sub}/resourceGroups/rg-{c.customer.prefix}-prod/providers/"
              f"{c.extra.get('provider', 'Microsoft.Compute/virtualMachines')}/{c.host}")
    ctx: dict[str, Any] = {}
    if c.value is not None:
        ctx = {"conditionType": "SingleResourceMultipleMetricCriteria", "condition": {"allOf": [{
            "metricName": c.extra.get("metric", c.check), "operator": "GreaterThan",
            "threshold": str(c.threshold), "timeAggregation": "Average", "metricValue": c.value}],
            "windowSize": "PT5M"}}
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertId": f"/subscriptions/{sub}/providers/Microsoft.AlertsManagement/alerts/{_hex(c.nid, 8)}",
                "alertRule": c.extra.get("rule", c.title),
                "severity": c.native,
                "signalType": c.extra.get("signal", "Metric"),
                "monitorCondition": "Fired" if c.firing else "Resolved",
                "monitoringService": c.extra.get("msvc", "Platform"),
                "alertTargetIDs": [target.lower()],
                "firedDateTime": c.iso,
            },
            "alertContext": ctx,
        },
    }


def _raw_alertmanager(c: Ctx) -> dict:
    labels = {"alertname": c.check, "severity": c.native, "instance": c.extra.get("instance", f"{c.host}:9100"),
              "namespace": c.extra.get("ns", c.service), "cluster": f"{c.customer.prefix}-aks-prod"}
    if "pod" in c.extra:
        labels["pod"] = c.extra["pod"]
    return {
        "status": "firing" if c.firing else "resolved",
        "labels": labels,
        "annotations": {"summary": c.title, "description": c.extra.get("output", c.title)},
        "startsAt": c.iso,
        "endsAt": "0001-01-01T00:00:00Z" if c.firing else c.iso,
        "fingerprint": _hex(c.nid, 16).lower(),
        "generatorURL": f"http://prometheus.{c.customer.prefix}.internal/graph?g0.expr={c.check}",
    }


def _raw_splunk(c: Ctx) -> dict:
    return {
        "finding_id": f"{_hex(c.nid, 8).lower()}@@notable@@{_hex(c.nid + 7, 12).lower()}",
        "rule_title": c.title, "urgency": c.native, "security_domain": c.extra.get("domain", "access"),
        "risk_score": {"informational": 10, "low": 20, "medium": 40, "high": 60, "critical": 80}.get(c.native, 20),
        "src": c.extra.get("src", f"10.{c.nid % 200}.{c.nid % 97}.{c.nid % 251}"), "dest": c.host,
        "status_label": "New" if c.firing else "Closed", "_time": c.iso,
    }


def _raw_sentinel(c: Ctx) -> dict:
    return {
        "SystemAlertId": _hex(c.nid, 8).lower() + "-sent", "AlertName": c.title, "AlertSeverity": c.native,
        "ProductName": "Azure Sentinel", "ProviderName": "ASI Scheduled Alerts",
        "Tactics": c.extra.get("tactics", "CredentialAccess"),
        "CompromisedEntity": c.host, "Status": "New" if c.firing else "Resolved", "TimeGenerated": c.iso,
    }


def _raw_defender(c: Ctx) -> dict:
    return {
        "id": "da" + _hex(c.nid, 16).lower(), "title": c.title, "severity": c.native.lower(),
        "category": c.extra.get("category", "Execution"), "serviceSource": "microsoftDefenderForEndpoint",
        "detectionSource": c.extra.get("dsource", "antivirus"),
        "status": "new" if c.firing else "resolved",
        "evidence": [{"@odata.type": "#microsoft.graph.security.deviceEvidence", "deviceDnsName": f"{c.host}.{c.customer.domain}"}],
        "createdDateTime": c.iso,
    }


SN_EM_SEVERITY = {"Clear": "0", "Critical": "1", "Major": "2", "Minor": "3", "Warning": "4", "Info": "5"}


def _raw_servicenow(c: Ctx) -> dict:
    if c.check == "incident":
        return {
            "number": f"INC{c.nid % 10**7:07d}", "priority": c.native,
            "short_description": c.title, "assignment_group": c.extra.get("group", "Service Desk L2"),
            "cmdb_ci": c.host, "state": "New" if c.firing else "Resolved", "opened_at": c.iso,
            "company": c.customer.name,
        }
    return {
        "source": c.extra.get("em_source", "SCOM"), "node": c.host, "type": c.extra.get("em_type", c.check),
        "resource": c.extra.get("resource", c.service), "severity": SN_EM_SEVERITY.get(c.native, "5"),
        "message_key": f"{c.host}_{c.check}", "description": c.title, "time_of_event": c.iso,
    }


def _raw_veeam(c: Ctx) -> dict:
    return {
        "JobName": c.extra.get("job", "Daily-Backup"), "JobType": "Backup", "Result": c.native,
        "SessionId": f"{_hex(c.nid, 8).lower()}-{_hex(c.nid + 1, 4).lower()}",
        "BackupServer": c.customer.hosts("backup")[0], "Details": c.extra.get("output", ""),
        "EndTime": c.iso,
    }


def _raw_controlm(c: Ctx) -> dict:
    return {
        "jobName": c.extra.get("job", c.host), "folder": c.extra.get("folder", c.customer.prefix.upper() + "_NIGHTLY"),
        "application": c.service, "subApplication": c.extra.get("subapp", "DAILY"),
        "status": c.native, "orderId": f"{c.nid % 36**5:05x}", "runNo": 1,
        "host": c.host, "ctm": f"ctm-{c.customer.prefix}-prod", "time": c.iso,
    }


def _raw_redgate(c: Ctx) -> dict:
    return {
        "AlertId": c.nid % 10**7, "AlertType": c.extra.get("atype", c.title), "Severity": c.native,
        "Target": f"{c.host}\\MSSQLSERVER", "Database": c.extra.get("db", ""),
        "Status": "Active" if c.firing else "Ended", "Raised": c.iso,
        "Details": c.extra.get("output", ""),
    }


def _raw_scom(c: Ctx) -> dict:
    return {
        "Id": f"{_hex(c.nid, 8).lower()}-scom", "Name": c.extra.get("alert", c.title), "Severity": c.native,
        "Priority": c.extra.get("priority", "Normal"), "MonitoringObjectDisplayName": f"{c.host}.{c.customer.domain}",
        "ResolutionState": "New" if c.firing else "Closed", "TimeRaised": c.iso,
        "ManagementGroup": f"{c.customer.prefix.upper()}-MG01",
    }


def _raw_prtg(c: Ctx) -> dict:
    return {
        "device": c.host, "sensor": c.extra.get("sensor", c.check), "status": c.native,
        "message": c.extra.get("output", ""), "lastvalue": "" if c.value is None else f"{c.value:g} {c.unit}".strip(),
        "probe": f"{c.customer.short} probe", "group": c.service, "datetime": c.iso, "sensorid": 2000 + c.nid % 8000,
    }


def _raw_solarwinds(c: Ctx) -> dict:
    return {
        "AlertName": c.extra.get("alert", c.title), "Severity": c.native, "NodeName": c.host,
        "ObjectType": c.extra.get("otype", "Node"), "EntityCaption": c.extra.get("caption", c.host),
        "AlertMessage": c.title, "AlertActiveID": c.nid % 10**6,
        "TriggeredDateTime": c.iso, "Acknowledged": False, "State": "Active" if c.firing else "Reset",
    }


def _raw_vcf(c: Ctx) -> dict:
    return {
        "alertId": f"{_hex(c.nid, 8).lower()}-vcfops", "alertDefinitionName": c.title,
        "alertLevel": c.native, "resourceKind": c.extra.get("kind", "VirtualMachine"), "resourceName": c.host,
        "status": "ACTIVE" if c.firing else "CANCELED", "startTimeUTC": c.ms, "controlState": "OPEN",
    }


def _raw_netapp(c: Ctx) -> dict:
    return {
        "eventName": c.extra.get("ename", c.title), "severity": c.native, "state": "New" if c.firing else "Obsolete",
        "sourceName": c.extra.get("vol", "vol01"), "sourceType": "Volume", "cluster": c.host,
        "svm": f"svm_{c.customer.prefix}_backup", "triggeredTime": c.iso,
        "condition": "" if c.value is None else f"Used {c.value:g}% (threshold {c.threshold:g}%)",
    }


def _raw_fortigate(c: Ctx) -> dict:
    return {
        "date": c.iso[:10], "time": c.iso[11:19], "devname": c.host, "devid": f"FG200F{c.nid % 10**10:010d}",
        "logid": c.extra.get("logid", "0100032001"), "type": "event", "subtype": c.extra.get("subtype", "system"),
        "level": c.native, "msg": c.title, "logdesc": c.extra.get("logdesc", c.title),
    }


def _raw_crowdstrike(c: Ctx) -> dict:
    return {
        "composite_id": f"{_hex(c.nid, 8).lower()}:ind:{_hex(c.nid + 3, 8).lower()}",
        "severity_name": c.native, "display_name": c.title, "tactic": c.extra.get("tactic", "Execution"),
        "technique": c.extra.get("technique", "Command and Scripting Interpreter"),
        "device": {"hostname": c.host}, "status": "new" if c.firing else "closed",
        "pattern_disposition_description": c.extra.get("disp", "Detection, standard detection."),
        "created_timestamp": c.iso,
    }


def _raw_sap_alm(c: Ctx) -> dict:
    return {
        "alertName": c.title, "severity": c.native, "category": c.extra.get("category", "Integration & Exception Monitoring"),
        "serviceName": f"{c.customer.prefix.upper()}-PRD", "managedObject": c.host,
        "status": "Open" if c.firing else "Closed", "timestamp": c.iso,
    }


# ---------------------------------------------------------------- sources


@dataclass(frozen=True)
class Source:
    id: str
    vendor: str
    category: str
    severity_map: dict[str, str]      # native -> normalised
    resolved_native: Optional[str]    # native word used on a resolve event (None = keep the firing one)
    raw: Callable[[Ctx], dict]


def _src(id_, vendor, category, smap, resolved, raw) -> Source:
    return Source(id_, vendor, category, smap, resolved, raw)


SOURCES: dict[str, Source] = {s.id: s for s in [
    _src("dynatrace", "Dynatrace", "apm",
         {"AVAILABILITY": "critical", "ERROR": "major", "PERFORMANCE": "minor",
          "RESOURCE_CONTENTION": "warning", "CUSTOM_ALERT": "warning"}, None, _raw_dynatrace),
    _src("zabbix", "Zabbix", "infrastructure",
         {"Disaster": "critical", "High": "major", "Average": "minor", "Warning": "warning",
          "Information": "info", "Not classified": "info", "Resolved": "info"}, "Resolved", _raw_zabbix),
    _src("checkmk", "Checkmk", "infrastructure",
         {"DOWN": "critical", "UNREACH": "major", "CRIT": "major", "UNKNOWN": "minor", "WARN": "warning",
          "OK": "info", "UP": "info"}, "OK", _raw_checkmk),
    _src("azure_monitor", "Azure Monitor", "cloud",
         {"Sev0": "critical", "Sev1": "major", "Sev2": "minor", "Sev3": "warning", "Sev4": "info"}, None, _raw_azure),
    _src("alertmanager", "Prometheus Alertmanager", "kubernetes",
         {"critical": "critical", "warning": "warning", "info": "info"}, None, _raw_alertmanager),
    _src("splunk_es", "Splunk Enterprise Security", "security",
         {"critical": "critical", "high": "major", "medium": "minor", "low": "warning", "informational": "info"},
         None, _raw_splunk),
    _src("sentinel", "Microsoft Sentinel", "security",
         {"High": "major", "Medium": "minor", "Low": "warning", "Informational": "info"}, None, _raw_sentinel),
    _src("defender_xdr", "Microsoft Defender XDR", "security",
         {"High": "major", "Medium": "minor", "Low": "warning", "Informational": "info"}, None, _raw_defender),
    _src("servicenow", "ServiceNow", "itsm",
         {"1 - Critical": "critical", "2 - High": "major", "3 - Moderate": "minor", "4 - Low": "warning",
          "5 - Planning": "info", "Critical": "critical", "Major": "major", "Minor": "minor",
          "Warning": "warning", "Info": "info", "Clear": "info"}, None, _raw_servicenow),
    _src("veeam", "Veeam Backup & Replication", "backup",
         {"Failed": "major", "Warning": "warning", "Success": "info"}, None, _raw_veeam),
    _src("controlm", "BMC Control-M", "jobs",
         {"Ended Not OK": "major", "Late": "warning", "Wait Condition": "info", "Ended OK": "info"}, None, _raw_controlm),
    _src("redgate", "Redgate Monitor", "database",
         {"High": "major", "Medium": "minor", "Low": "warning"}, None, _raw_redgate),
    _src("scom", "Microsoft SCOM", "infrastructure",
         {"Critical": "critical", "Warning": "warning", "Information": "info"}, None, _raw_scom),
    _src("prtg", "Paessler PRTG", "network",
         {"Down": "critical", "Warning": "warning", "Unusual": "minor", "Up": "info"}, "Up", _raw_prtg),
    _src("solarwinds", "SolarWinds", "network",
         {"Critical": "critical", "Serious": "major", "Warning": "warning", "Notice": "info",
          "Informational": "info"}, None, _raw_solarwinds),
    _src("vcf_ops", "VCF Operations", "virtualisation",
         {"CRITICAL": "critical", "IMMEDIATE": "major", "WARNING": "warning", "INFORMATION": "info"}, None, _raw_vcf),
    _src("netapp", "NetApp", "storage",
         {"Critical": "critical", "Error": "major", "Warning": "warning", "Information": "info"}, None, _raw_netapp),
    _src("fortigate", "Fortinet FortiGate", "network",
         {"emergency": "critical", "alert": "critical", "critical": "critical", "error": "major",
          "warning": "warning", "notice": "info", "information": "info"}, None, _raw_fortigate),
    _src("crowdstrike", "CrowdStrike Falcon", "security",
         {"Critical": "critical", "High": "major", "Medium": "minor", "Low": "warning", "Informational": "info"},
         None, _raw_crowdstrike),
    _src("sap_alm", "SAP Cloud ALM", "erp",
         {"Very High": "critical", "High": "major", "Medium": "minor", "Low": "warning"}, None, _raw_sap_alm),
]}

# ---------------------------------------------------------------- templates


@dataclass(frozen=True)
class Template:
    source: str
    check: str                        # stable check name (retune key)
    title: str                        # str.format with host, service, value, threshold, unit + extras
    native: str                       # native severity when firing
    unit: str = ""
    threshold: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)
    # values drawn per event for these extra keys (one rng.choice each)
    choices: dict[str, tuple] = field(default_factory=dict)


def _t(source, check, title, native, unit="", threshold=None, choices=None, **extra) -> Template:
    return Template(source, check, title, native, unit, threshold, extra, choices or {})


TEMPLATES: dict[tuple[str, str], Template] = {(t.source, t.check): t for t in [
    # Dynatrace (problem titles are the platform's own wording)
    _t("dynatrace", "response_time", "Response time degradation on {service}", "PERFORMANCE",
       impact="SERVICES", dt_title="Response time degradation"),
    _t("dynatrace", "failure_rate", "Failure rate increase on {service}", "ERROR",
       impact="SERVICES", dt_title="Failure rate increase"),
    _t("dynatrace", "memory_saturation", "Memory saturation on {host}", "RESOURCE_CONTENTION", "%", 80,
       impact="INFRASTRUCTURE", dt_title="Memory saturation"),
    _t("dynatrace", "low_disk", "Low disk space on {host}", "RESOURCE_CONTENTION", "%", 90,
       impact="INFRASTRUCTURE", dt_title="Low disk space"),
    _t("dynatrace", "synthetic_unavailable", "Synthetic monitor unavailable: {service} login journey", "AVAILABILITY",
       impact="APPLICATION", dt_title="Browser monitor global outage"),
    _t("dynatrace", "process_unavailable", "Process unavailable on {host}", "AVAILABILITY",
       impact="INFRASTRUCTURE", dt_title="Process unavailable"),
    _t("dynatrace", "deployment_change", "Deployment change detected on {service}", "CUSTOM_ALERT",
       impact="SERVICES", dt_title="Custom alert: deployment change"),
    # Zabbix
    _t("zabbix", "cpu_util", "High CPU utilization (over {threshold:g}% for 1m)", "Average", "%", 80,
       expr="min(/{host}/system.cpu.util,1m)>80"),
    _t("zabbix", "icmp_unavailable", "Unavailable by ICMP ping", "High", expr="max(/{host}/icmpping,#3)=0"),
    _t("zabbix", "agent_unavailable", "Zabbix agent is not available (for 3m)", "Average",
       expr="nodata(/{host}/agent.ping,3m)=1"),
    _t("zabbix", "restarted", "{host} has been restarted (uptime < 10m)", "Warning",
       expr="last(/{host}/system.uptime)<10m"),
    _t("zabbix", "pg_connections", "PostgreSQL: Too many connections (used > {threshold:g}%)", "Warning", "%", 80,
       expr="last(/{host}/pgsql.connections.sum.total_pct)>80"),
    _t("zabbix", "swap_low", "High swap space usage (less than 50% free)", "Warning"),
    # Checkmk
    _t("checkmk", "ntp_time", "NTP Time WARN on {host}: offset {value:g} ms", "WARN", " ms", 150,
       svcdesc="NTP Time"),
    _t("checkmk", "fs_var", "Filesystem /var WARN on {host}: {value:g}% used", "WARN", "%", 80,
       svcdesc="Filesystem /var"),
    _t("checkmk", "fs_backup", "Filesystem /backup on {host}: {value:g}% used", "WARN", "%", 85,
       svcdesc="Filesystem /backup"),
    _t("checkmk", "interface", "Interface {iface} CRIT on {host}: operational state down", "CRIT",
       choices={"iface": ("Gi1/0/48", "Gi1/0/24", "Te1/1/1")}, svcdesc="Interface"),
    _t("checkmk", "interface_flap", "Interface {iface} on {host} is flapping", "WARN",
       choices={"iface": ("Gi1/0/48", "Gi1/0/24")}, svcdesc="Interface", ntype="FLAPPINGSTART"),
    _t("checkmk", "host_down", "Host {host} DOWN", "DOWN", host_check=True, output="CRIT - 10.0.0.1: rta nan, lost 100%"),
    _t("checkmk", "host_unreach", "Host {host} UNREACH", "UNREACH", host_check=True,
       output="Parent host down, host not reachable"),
    _t("checkmk", "https_cert", "HTTPS certificate for {service} expires in {days} days", "WARN",
       choices={"days": (12, 9, 6, 3)}, svcdesc="HTTPS Cert"),
    _t("checkmk", "https_cert_expired", "HTTPS certificate for {service} has expired", "CRIT", svcdesc="HTTPS Cert"),
    # Azure Monitor
    _t("azure_monitor", "cpu_pct", "Percentage CPU greater than {threshold:g} on {host}", "Sev3", "%", 70,
       metric="Percentage CPU"),
    _t("azure_monitor", "http5xx", "Http 5xx above threshold on {service}", "Sev1", "", None,
       provider="Microsoft.Web/sites", metric="Http5xx"),
    _t("azure_monitor", "advisor", "Advisor: right-size or shut down underutilized virtual machines", "Sev4",
       signal="Activity Log", msvc="Activity Log - Recommendation"),
    _t("azure_monitor", "appgw_unhealthy", "Application Gateway unhealthy host count above 0", "Sev1",
       provider="Microsoft.Network/applicationGateways", metric="UnhealthyHostCount"),
    _t("azure_monitor", "appgw_cert", "Application Gateway listener certificate expired", "Sev0",
       provider="Microsoft.Network/applicationGateways", signal="Log", msvc="Log Analytics"),
    # Prometheus Alertmanager (kube-prometheus style alert names)
    _t("alertmanager", "KubePodCrashLooping", "Pod {service}/{pod} is in waiting state (reason: CrashLoopBackOff)",
       "critical", choices={"pod": ("api-7f9c4", "api-5d2b8", "worker-6c1aa")}),
    _t("alertmanager", "KubeContainerOOMKilled", "Container in pod {service}/{pod} was OOMKilled", "warning",
       choices={"pod": ("api-7f9c4", "api-5d2b8", "worker-6c1aa")}),
    _t("alertmanager", "ContainerMemoryNearLimit", "Container memory above {threshold:g}% of limit in {service}",
       "warning", "%", 85),
    _t("alertmanager", "NodeFilesystemSpaceFillingUp", "Filesystem on {host} is predicted to run out of space within 24 hours",
       "warning"),
    _t("alertmanager", "KubeHpaMaxedOut", "HPA {service} has been running at max replicas for longer than 15 minutes",
       "warning"),
    _t("alertmanager", "CPUThrottlingHigh", "{value:g}% throttling of CPU in namespace {service}", "info", "%", 25),
    _t("alertmanager", "BlackboxProbeFailed", "Probe of https://{service} failed (TLS handshake error)", "critical"),
    _t("alertmanager", "SSLCertExpiringSoon", "TLS certificate for {service} expires in less than 7 days", "warning"),
    # Splunk ES
    _t("splunk_es", "failed_logins", "Excessive Failed Logins", "low", domain="access"),
    _t("splunk_es", "brute_force", "Brute Force Access Behavior Detected", "medium", domain="access"),
    _t("splunk_es", "unusual_admin", "Unusual Volume of Admin Logons", "medium", domain="identity"),
    # Microsoft Sentinel
    _t("sentinel", "failed_signins", "Multiple failed sign-in attempts from a single IP", "Low"),
    _t("sentinel", "rare_operation", "Rare subscription-level operations in Azure", "Low", tactics="Impact"),
    _t("sentinel", "unfamiliar_signin", "Sign-in from an unfamiliar location", "Medium", tactics="InitialAccess"),
    # Microsoft Defender XDR
    _t("defender_xdr", "suspicious_ps", "Suspicious PowerShell command line", "Medium", category="Execution"),
    _t("defender_xdr", "pua", "Potentially unwanted application detected", "Low", category="Malware"),
    _t("defender_xdr", "sensor_stale", "Device stopped reporting sensor data", "Informational",
       category="SensorHealth", dsource="microsoftDefenderForEndpoint"),
    # ServiceNow
    _t("servicenow", "incident", "{summary}", "2 - High", choices={"summary": ("Service degraded",)}),
    _t("servicenow", "em_event", "{summary}", "Major", choices={"summary": ("Event received",)}),
    # Veeam
    _t("veeam", "job_failed", "Backup job '{job}' finished with Failed", "Failed",
       choices={"job": ("Daily-SQL", "Daily-FileServers", "Daily-VMs-Tier1")},
       output="Error: There is not enough space on the disk. Failed to write data to the file."),
    _t("veeam", "job_warning", "Backup job '{job}' finished with Warning", "Warning",
       choices={"job": ("Daily-SQL", "Daily-FileServers", "Daily-VMs-Tier1")},
       output="Warning: VSS snapshot creation timed out, retrying with crash-consistent backup"),
    _t("veeam", "job_success", "Backup job '{job}' finished with Success", "Success",
       choices={"job": ("Daily-SQL", "Daily-FileServers", "Daily-VMs-Tier1", "Daily-VMs-Tier2")}),
    # Control-M
    _t("controlm", "ended_not_ok", "Job {job} Ended Not OK", "Ended Not OK",
       choices={"job": ("SAP_EXTRACT_FI", "EDI_INBOUND", "EOD_SETTLEMENT")}),
    _t("controlm", "late", "Job {job} is Late (expected end passed)", "Late",
       choices={"job": ("SAP_EXTRACT_FI", "EDI_INBOUND", "EOD_SETTLEMENT", "DWH_LOAD_SALES")}),
    _t("controlm", "wait_condition", "Job {job} in Wait Condition (input condition not met)", "Wait Condition",
       choices={"job": ("DWH_LOAD_FI", "REPORT_DAILY_PL", "INVOICE_RUN", "DWH_AGGREGATE")}),
    _t("controlm", "ended_ok", "Job {job} Ended OK", "Ended OK",
       choices={"job": ("SAP_EXTRACT_FI", "EDI_INBOUND", "DWH_LOAD_SALES", "ARCHIVE_LOGS", "REPORT_DAILY_PL")}),
    # Redgate Monitor
    _t("redgate", "long_running_query", "Long-running query on {host}", "Medium", db="APPDB",
       output="Query running for 312 s (threshold 120 s)"),
    _t("redgate", "blocking", "Blocking process on {host}", "High", output="Blocking chain of 47 sessions"),
    _t("redgate", "ple_low", "Page life expectancy low on {host}", "Low"),
    _t("redgate", "instance_unreachable", "SQL Server instance unreachable: {host}", "High",
       atype="Monitoring error (SQL Server data collection)"),
    _t("redgate", "deadlock", "Deadlock detected on {host}", "Medium"),
    # SCOM
    _t("scom", "heartbeat_failure", "Health Service Heartbeat Failure on {host}", "Critical", alert="Health Service Heartbeat Failure"),
    _t("scom", "failed_connect", "Failed to Connect to Computer {host}", "Critical", alert="Failed to Connect to Computer"),
    _t("scom", "sql_service_stopped", "SQL Server service stopped on {host}", "Critical", priority="High",
       alert="MSSQL on Windows: SQL Server service is stopped"),
    _t("scom", "health_service_unavailable", "Health Service unavailable on {host} (agent not reporting)", "Warning",
       alert="Health Service Unavailable"),
    _t("scom", "logical_disk", "Logical Disk Free Space is low on {host}", "Warning"),
    # PRTG
    _t("prtg", "ping", "Ping Down on {host}", "Down", sensor="Ping", output="Destination host unreachable (ICMP error # 11003)"),
    _t("prtg", "traffic_unusual", "SNMP Traffic Unusual on {host}", "Unusual", sensor="SNMP Traffic (WAN)",
       output="Unusual: traffic is higher than usual for this weekday and hour"),
    _t("prtg", "ping_jitter", "Ping Jitter Warning on {host}", "Warning", sensor="Ping Jitter", output="Jitter above 30 ms"),
    # SolarWinds
    _t("solarwinds", "node_down", "Node {host} is down", "Critical", alert="Node is down"),
    _t("solarwinds", "iface_down", "Interface {iface} on {host} is down", "Critical",
       choices={"iface": ("Te1/1/1", "Te1/1/2")}, alert="Interface is down", otype="Interface"),
    _t("solarwinds", "high_response", "High response time on {host}", "Warning", alert="High Response Time"),
    # VCF Operations
    _t("vcf_ops", "mem_contention", "Virtual machine has memory contention", "WARNING"),
    _t("vcf_ops", "host_disconnected", "Host has lost connection to vCenter Server", "IMMEDIATE", kind="HostSystem"),
    _t("vcf_ops", "datastore_space", "Datastore is running out of disk space", "WARNING", kind="Datastore"),
    # NetApp
    _t("netapp", "vol_nearly_full", "Volume Space Nearly Full on {vol}", "Warning", "%", 85,
       choices={"vol": ("vol_veeam_repo01",)}, ename="Volume Space Nearly Full"),
    _t("netapp", "vol_full", "Volume Space Full on {vol}", "Error", "%", 98,
       choices={"vol": ("vol_veeam_repo01",)}, ename="Volume Space Full"),
    _t("netapp", "snap_reserve", "Volume Snapshot Reserve Space Nearly Full on {vol}", "Warning",
       choices={"vol": ("vol_vmware_ds01", "vol_cifs_home")}, ename="Volume Snapshot Reserve Space Nearly Full"),
    # FortiGate
    _t("fortigate", "admin_login", "Admin login successful", "information", logid="0100032001", subtype="system"),
    _t("fortigate", "vpn_down", "IPsec VPN tunnel {tunnel} down", "warning",
       choices={"tunnel": ("to-branch-01", "to-branch-02", "to-azure")}, logid="0101037141", subtype="vpn"),
    _t("fortigate", "link_down", "Link monitor: interface {iface} status down", "critical",
       choices={"iface": ("port1", "wan1")}, logid="0100022932", subtype="system"),
    # CrowdStrike
    _t("crowdstrike", "pup", "Adware/PUP detected", "Low", tactic="Malware", technique="Adware/PUP"),
    _t("crowdstrike", "suspicious_activity", "Suspicious process activity", "Medium"),
    # SAP Cloud ALM
    _t("sap_alm", "integration_failed", "Integration message failed (IDoc ORDERS05)", "Medium"),
    _t("sap_alm", "job_failed", "Job failed: {job}", "High", category="Job & Automation Monitoring",
       choices={"job": ("Z_FI_EXTRACT_DAILY", "Z_SD_BILLING_RUN")}),
]}


# ---------------------------------------------------------------- event construction


def build_event(customer: Customer, source_id: str, check: str, host: str, host_role: str, ts: float,
                rng: random.Random, *, status: str = "firing", value: Optional[float] = None,
                threshold: Optional[float] = None, native: Optional[str] = None, nid: Optional[int] = None,
                picks: Optional[dict[str, Any]] = None, extra: Optional[dict[str, Any]] = None,
                truth: Optional[dict[str, Any]] = None) -> dict:
    """Return one event as a plain dict (no id yet). `picks` fixes the template choices so a
    resolve event repeats its firing's job/interface/volume names."""
    tpl = TEMPLATES[(source_id, check)]
    src = SOURCES[source_id]
    service = customer.service_for(host_role)
    if threshold is None:
        threshold = tpl.threshold
    chosen = dict(picks or {})
    for k, opts in tpl.choices.items():
        if k not in chosen:
            chosen[k] = rng.choice(opts)
    fmt = {"host": host, "service": service, "value": value if value is not None else 0,
           "threshold": threshold if threshold is not None else 0, "unit": tpl.unit, **chosen}
    if extra:
        fmt.update(extra)
    title = tpl.title.format(**fmt)
    firing_native = native or tpl.native
    if status == "resolved" and src.resolved_native:
        native_word = src.resolved_native
    else:
        native_word = firing_native
    if source_id == "servicenow" and status == "resolved" and check == "em_event":
        native_word = "Clear"
    if status == "resolved" and tpl.extra.get("host_check"):
        native_word = "UP"
    ex = {**tpl.extra, **chosen, **(extra or {})}
    if "expr" in ex:
        ex["expr"] = ex["expr"].replace("{host}", host)
    if status == "resolved":
        ex["orig_native"] = firing_native
        ex.pop("ntype", None)
        if tpl.extra.get("ntype") == "FLAPPINGSTART":
            ex["ntype"] = "FLAPPINGSTOP"
    nid = nid if nid is not None else rng.getrandbits(48)
    ctx = Ctx(customer=customer, source_id=source_id, check=check, host=host, service=service, title=title,
              native=native_word, status=status, ts=ts, nid=nid, value=value, threshold=threshold,
              unit=tpl.unit.strip(), extra=ex)
    severity = src.severity_map[firing_native]   # a resolve keeps the severity of what it resolves
    ev = {
        "ts": iso(ts), "customer": customer.id, "source": source_id, "vendor": src.vendor, "host": host,
        "service": service, "check": check, "title": title if status == "firing" else f"Resolved: {title}",
        "severity": severity, "native_severity": native_word, "status": status,
        "value": None if value is None else round(float(value), 1),
        "threshold": None if threshold is None else float(threshold),
        "raw": src.raw(ctx),
        "truth": truth or {"scenario_id": None, "role": "noise", "incident_id": None, "pattern": None},
        "_nid": nid, "_picks": chosen,
    }
    return ev


def customers_with(sources: set[str]) -> list[str]:
    return [cid for cid, c in CUSTOMERS.items() if sources.issubset(set(c.sources))]


def source_ids() -> list[str]:
    return list(SOURCES)
