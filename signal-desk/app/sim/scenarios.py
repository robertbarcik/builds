"""The eight incident scripts, as timed step lists shared by the 30-day history and the live
stream. Offsets are minutes relative to the root event (0). Negative offsets are precursors.
The live stream compresses the same script into a 60-90 s window (see generator.compress).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.sim.catalog import customers_with


@dataclass(frozen=True)
class Step:
    role: str                              # root | symptom | precursor
    source: str
    check: str
    host_role: str
    at: tuple[float, float]                # minutes from root, drawn uniformly per occurrence
    hosts: tuple[int, int] = (1, 1)        # number of distinct hosts (shared pick order per role)
    host_skip: int = 0                     # skip the first n hosts of the role's pick order
    spread: float = 0.0                    # hosts fire spread over this many minutes
    repeat: tuple[int, int] = (1, 1)       # firings per host
    every: float = 0.0                     # minutes between repeats
    value: Optional[tuple[float, float]] = None
    value_step: float = 0.0                # value grows by this much per repeat
    threshold: Optional[float] = None
    resolve: Optional[tuple[float, float]] = None   # minutes after firing until a resolve event
    native: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    blurb: str
    steps: tuple[Step, ...]
    occurrences: tuple[int, int] = (4, 8)  # in the 30-day history
    orphans: tuple[int, int] = (0, 0)      # precursor-only runs that lead to nothing
    visitor: bool = False                  # offered as a "Break something" tile
    tile_title: str = ""
    tile_blurb: str = ""
    patch_night: bool = False              # history occurrences land inside patch windows
    hours: Optional[tuple[int, int]] = None  # local hour window for the root event in history

    @property
    def sources(self) -> set[str]:
        return {s.source for s in self.steps}

    def eligible_customers(self) -> list[str]:
        return customers_with(self.sources)


S = Step

SCENARIOS: dict[str, Scenario] = {sc.id: sc for sc in [
    Scenario(
        id="backup_disk_fills",
        title="Backup repository fills up, Veeam jobs fail",
        blurb="The Veeam repository volume creeps up for hours, then fills; nightly jobs fail.",
        visitor=True, tile_title="Fill the backup disk",
        tile_blurb="The backup repository fills up and the nightly jobs start failing.",
        orphans=(3, 4), hours=(0, 4),
        steps=(
            S("precursor", "netapp", "vol_nearly_full", "nas", (-720, -300), value=(85.5, 88.0), threshold=85),
            S("precursor", "checkmk", "fs_backup", "backup", (-600, -240), value=(86.0, 89.0), threshold=85),
            S("precursor", "netapp", "vol_nearly_full", "nas", (-180, -90), value=(92.0, 95.0), threshold=85),
            S("root", "netapp", "vol_full", "nas", (0, 0), value=(98.0, 99.9), threshold=98),
            S("symptom", "checkmk", "fs_backup", "backup", (1, 3), value=(98.5, 99.9), threshold=85, native="CRIT"),
            S("symptom", "veeam", "job_failed", "backup", (8, 25), repeat=(2, 3), every=12),
            S("symptom", "servicenow", "em_event", "backup", (10, 26), native="Major",
              extra={"summary": "Veeam backup jobs failed on repository", "em_source": "Veeam ONE"}),
            S("symptom", "servicenow", "incident", "backup", (28, 40), native="2 - High",
              extra={"summary": "Nightly backups failed: repository volume full", "group": "Backup & Storage"}),
        ),
    ),
    Scenario(
        id="cert_expiry",
        title="TLS certificate expires on the customer portal",
        blurb="Expiry warnings are ignored for two days, then the portal certificate expires.",
        visitor=True, tile_title="Let a certificate expire",
        tile_blurb="Nobody renews the portal certificate. Browsers start refusing the site.",
        occurrences=(4, 5), hours=(6, 10),
        steps=(
            S("precursor", "checkmk", "https_cert", "web", (-2880, -2820), repeat=(6, 8), every=360),
            S("precursor", "alertmanager", "SSLCertExpiringSoon", "web", (-1440, -1380), repeat=(2, 3), every=480),
            S("root", "checkmk", "https_cert_expired", "web", (0, 0)),
            S("symptom", "alertmanager", "BlackboxProbeFailed", "web", (1, 3)),
            S("symptom", "dynatrace", "synthetic_unavailable", "web", (2, 5)),
            S("symptom", "dynatrace", "failure_rate", "web", (4, 8)),
            S("symptom", "azure_monitor", "appgw_cert", "web", (3, 6)),
            S("symptom", "servicenow", "incident", "web", (15, 25), native="1 - Critical",
              extra={"summary": "Customer portal unreachable: TLS certificate expired", "group": "Web Operations"}),
        ),
    ),
    Scenario(
        id="core_uplink_down",
        title="Core uplink down, alert storm",
        blurb="One core uplink fails and everything behind it reports unreachable at once.",
        visitor=True, tile_title="Pull the core uplink",
        tile_blurb="One cable at the core switch. Watch how many tools notice.",
        occurrences=(4, 6),
        steps=(
            S("root", "solarwinds", "iface_down", "core", (0, 0), extra={"iface": "Te1/1/1"}),
            S("symptom", "checkmk", "interface", "core", (0, 1), extra={"iface": "Te1/1/1"}, resolve=(35, 55)),
            S("symptom", "fortigate", "link_down", "fw", (0, 1), resolve=(35, 55)),
            S("symptom", "prtg", "ping", "access", (1, 3), hosts=(5, 6), spread=3, resolve=(35, 55)),
            S("symptom", "prtg", "ping", "branch", (1, 3), hosts=(3, 5), spread=3, resolve=(35, 55)),
            S("symptom", "checkmk", "host_unreach", "win", (1, 4), hosts=(6, 8), spread=4, resolve=(36, 56)),
            S("symptom", "checkmk", "host_unreach", "app", (1, 4), hosts=(3, 4), spread=3, resolve=(36, 56)),
            S("symptom", "solarwinds", "node_down", "access", (1, 4), hosts=(4, 6), spread=3, resolve=(35, 55)),
            S("symptom", "scom", "heartbeat_failure", "win", (3, 6), hosts=(5, 8), spread=3, resolve=(38, 58)),
            S("symptom", "zabbix", "icmp_unavailable", "app", (2, 5), hosts=(3, 4), spread=3, resolve=(36, 56)),
            S("symptom", "dynatrace", "synthetic_unavailable", "web", (4, 7)),
            S("symptom", "servicenow", "incident", "core", (12, 20), native="1 - Critical",
              extra={"summary": "Multiple sites unreachable: core network outage", "group": "Network Operations"}),
        ),
    ),
    Scenario(
        id="memory_leak_after_deploy",
        title="Memory leak after a deployment",
        blurb="A new release leaks memory; containers climb towards their limit, then crash-loop.",
        orphans=(2, 3), hours=(9, 18),
        steps=(
            S("precursor", "dynatrace", "deployment_change", "k8s", (-200, -150)),
            S("precursor", "alertmanager", "ContainerMemoryNearLimit", "k8s", (-120, -60), repeat=(2, 3), every=20,
              value=(86.0, 88.0), value_step=3.0, threshold=85, resolve=(5, 10)),
            S("precursor", "dynatrace", "memory_saturation", "k8s", (-50, -25), value=(91.0, 94.0), threshold=80),
            S("root", "alertmanager", "KubeContainerOOMKilled", "k8s", (0, 0), repeat=(2, 3), every=4),
            S("symptom", "alertmanager", "KubePodCrashLooping", "k8s", (5, 9), resolve=(40, 60)),
            S("symptom", "dynatrace", "failure_rate", "k8s", (6, 12)),
            S("symptom", "dynatrace", "response_time", "k8s", (6, 12)),
            S("symptom", "servicenow", "incident", "k8s", (20, 30), native="2 - High",
              extra={"summary": "Checkout errors after release: pods crash-looping", "group": "Platform Team"}),
        ),
    ),
    Scenario(
        id="flapping_interface",
        title="Flapping access-switch interface",
        blurb="A worn patch cable makes one interface go up and down dozens of times.",
        occurrences=(5, 8),
        steps=(
            S("root", "checkmk", "interface_flap", "access", (0, 0), resolve=(40, 60)),
            S("symptom", "checkmk", "interface", "access", (1, 2), repeat=(10, 16), every=3, resolve=(1, 2)),
            S("symptom", "solarwinds", "iface_down", "access", (1, 2), repeat=(8, 12), every=4, resolve=(1, 2)),
            S("symptom", "prtg", "ping_jitter", "access", (2, 6), repeat=(2, 4), every=10),
        ),
    ),
    Scenario(
        id="controlm_job_chain",
        title="Control-M job chain breaks overnight",
        blurb="The upstream SAP extract runs late, then fails; every downstream job waits.",
        orphans=(3, 4), hours=(1, 4),
        steps=(
            S("precursor", "controlm", "late", "batch", (-75, -35), extra={"job": "SAP_EXTRACT_FI"}),
            S("root", "controlm", "ended_not_ok", "batch", (0, 0), extra={"job": "SAP_EXTRACT_FI"}),
            S("symptom", "sap_alm", "job_failed", "sap", (1, 3), extra={"job": "Z_FI_EXTRACT_DAILY"}),
            S("symptom", "controlm", "wait_condition", "batch", (10, 40), repeat=(3, 4), every=8),
            S("symptom", "servicenow", "incident", "batch", (30, 45), native="3 - Moderate",
              extra={"summary": "Nightly finance extract failed; downstream jobs waiting", "group": "Batch Operations"}),
        ),
    ),
    Scenario(
        id="db_pool_exhaustion",
        title="Database connection pool exhausted",
        blurb="Slow queries hold connections until the pool is empty and the app returns errors.",
        orphans=(2, 3), hours=(9, 18),
        steps=(
            S("precursor", "redgate", "long_running_query", "sql", (-45, -25), repeat=(2, 3), every=6),
            S("precursor", "zabbix", "pg_connections", "db", (-30, -15), value=(82.0, 86.0), value_step=4.0,
              repeat=(2, 3), every=5, threshold=80, resolve=(20, 40)),
            S("root", "redgate", "blocking", "sql", (0, 0)),
            S("symptom", "zabbix", "pg_connections", "db", (0, 2), value=(99.0, 100.0), threshold=80, native="High",
              resolve=(25, 40)),
            S("symptom", "zabbix", "cpu_util", "db", (1, 3), value=(95.0, 99.0), threshold=80, resolve=(22, 35)),
            S("symptom", "dynatrace", "failure_rate", "app", (2, 5)),
            S("symptom", "azure_monitor", "http5xx", "app", (3, 6)),
            S("symptom", "servicenow", "incident", "app", (15, 25), native="2 - High",
              extra={"summary": "Payment API errors: database connection pool exhausted", "group": "Database Team"}),
        ),
    ),
    Scenario(
        id="patch_night_failure",
        title="Patch night hides a real failure",
        blurb="Among hundreds of expected reboot alerts, one SQL Server does not come back.",
        occurrences=(4, 5), patch_night=True,
        steps=(
            S("root", "scom", "sql_service_stopped", "sql", (0, 0)),
            S("symptom", "redgate", "instance_unreachable", "sql", (1, 3)),
            S("symptom", "controlm", "ended_not_ok", "batch", (20, 40), extra={"job": "EOD_SETTLEMENT"}),
            S("symptom", "dynatrace", "process_unavailable", "sql", (2, 4)),
            S("symptom", "servicenow", "incident", "sql", (45, 60), native="2 - High",
              extra={"summary": "SQL Server did not start after patching", "group": "Database Team"}),
        ),
    ),
]}

VISITOR_SCENARIOS: tuple[str, ...] = tuple(s.id for s in SCENARIOS.values() if s.visitor)
