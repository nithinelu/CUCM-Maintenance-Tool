"""Cisco Unity Connection (CUC) specifics.

CUC runs on the same platform as CUCM, so SSH CLI, DRS, 'utils system upgrade initiate'
and 'file get' are shared (cucm_ops). This module adds what differs:

  * CUC health-check commands ('show cuc ...')
  * DRS feature name and trace presets
  * CUPI (Cisco Unity Provisioning Interface, REST /vmrest) inventory -> Excel
"""

import json

import requests
import urllib3

from cucm_cli import CLIError

DEFAULT_FEATURES = "CUC"

HEALTH_COMMANDS = [
    "show version active",
    "show version inactive",
    "show network cluster",
    "show cuc version",
    "show cuc cluster status",
    "show cuc dbserver disk",
    "show status",
    "utils ntp status",
    "utils service list",
    "utils core active list",
    "show cuc sysinfo",
]

FILE_PRESETS = {
    "CUC: all Unity Connection traces (last 2 h)": ("activelog", "cuc/*", "reltime hours 2"),
    "CUC: Conversation Manager traces (last 2 h)": ("activelog", "cuc/diag_CuCsMgr*", "reltime hours 2"),
    "CUC: Mixer traces (last 2 h)": ("activelog", "cuc/diag_CuMixer*", "reltime hours 2"),
    "CUC: Notifier traces (last 2 h)": ("activelog", "cuc/diag_CuNotifier*", "reltime hours 2"),
    "CUC: Tomcat / CUPI logs": ("activelog", "tomcat/logs/*", "reltime hours 24"),
}

CONSOLE_COMMANDS = [
    "show cuc version",
    "show cuc cluster status",
    "show cuc sysinfo",
    "show cuc dbserver disk",
]

# Sheet name -> (CUPI path, columns to put first). Other fields follow; *URI links dropped.
CUPI_PRESETS = {
    "Users": ("/vmrest/users", ["Alias", "FirstName", "LastName", "DisplayName", "DtmfAccessId",
                                "SmtpAddress", "EmailAddress", "IsVmEnrolled", "ObjectId"]),
    "Call handlers": ("/vmrest/handlers/callhandlers", ["DisplayName", "DtmfAccessId", "IsPrimary",
                                                         "ObjectId"]),
    "Directory handlers": ("/vmrest/handlers/directoryhandlers", ["DisplayName", "DtmfAccessId"]),
    "Interview handlers": ("/vmrest/handlers/interviewhandlers", ["DisplayName", "DtmfAccessId"]),
    "Distribution lists": ("/vmrest/distributionlists", ["DisplayName", "Alias", "DtmfAccessId"]),
    "Classes of service": ("/vmrest/coses", ["DisplayName"]),
    "User templates": ("/vmrest/usertemplates", ["Alias", "DisplayName"]),
    "Partitions": ("/vmrest/partitions", ["Name", "Description"]),
    "Search spaces": ("/vmrest/searchspaces", ["Name", "Description"]),
    "Routing rules": ("/vmrest/routingrules", ["DisplayName", "RuleIndex", "State"]),
    "Schedules": ("/vmrest/schedules", ["DisplayName"]),
    "Phone systems": ("/vmrest/phonesystems", ["DisplayName"]),
    "Port groups": ("/vmrest/portgroups", ["DisplayName", "TelephonyIntegrationMethodEnum"]),
    "Ports": ("/vmrest/ports", ["DisplayName", "MediaPortGroupObjectId"]),
    "Mailbox stores": ("/vmrest/mailboxstores", ["DisplayName", "ServerName", "DatabaseName"]),
}


def is_cuc(node):
    return (node or {}).get("cluster") == "cuc" or (node or {}).get("type") == "Unity"


class CUPI:
    """Read-only CUPI client (GET only)."""

    def __init__(self, host, username, password, timeout=60, base_url=None):
        self.base = (base_url or f"https://{host.strip()}").rstrip("/")
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.verify = False
        self.s.headers["Accept"] = "application/json"
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.timeout = timeout

    def _get(self, path, **params):
        try:
            r = self.s.get(self.base + path, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise CLIError(f"CUPI connection failed: {e}") from e
        if r.status_code == 401:
            raise CLIError("CUPI: 401 Unauthorized - use a Unity Connection application "
                           "administrator (e.g. System Administrator role)")
        if r.status_code == 403:
            raise CLIError(f"CUPI: 403 Forbidden for {path} - the account lacks the role for it")
        if r.status_code == 404:
            raise CLIError(f"CUPI: {path} not found on this Unity Connection version")
        if r.status_code != 200:
            raise CLIError(f"CUPI: HTTP {r.status_code} for {path}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise CLIError(f"CUPI: {path} did not return JSON (is this a Unity Connection server?)") from e

    @staticmethod
    def _items(doc):
        """{'@total': 'N', 'User': [...]} - the list key differs per object; a single item
        comes back as a dict instead of a list."""
        for k, v in (doc or {}).items():
            if k.startswith("@"):
                continue
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                return [v]
        return []

    def count(self, path):
        doc = self._get(path, rowsPerPage=1, pageNumber=1)
        return int((doc or {}).get("@total", len(self._items(doc))))

    def get_all(self, path, page_size=500):
        out, page = [], 1
        while True:
            doc = self._get(path, rowsPerPage=page_size, pageNumber=page)
            items = self._items(doc)
            out.extend(items)
            total = int((doc or {}).get("@total", len(out)) or 0)
            if not items or len(out) >= total or len(items) < page_size:
                return out
            page += 1

    def version(self):
        try:
            doc = self._get("/vmrest/version/product")
            return doc.get("version") or doc.get("Version") or json.dumps(doc)[:60]
        except CLIError:
            return "?"


def flatten(item, first=()):
    """CUPI item -> flat row dict: preferred columns first, *URI link fields dropped,
    nested values as JSON text."""
    row = {}
    for k in first:
        if k in item:
            row[k] = item[k]
    for k in sorted(item):
        if k in row or k.endswith("URI"):
            continue
        row[k] = item[k]
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}


def fetch_preset(cupi, name_or_path):
    """Rows for a CUPI_PRESETS name, or for a raw '/vmrest/...' path (custom)."""
    if name_or_path in CUPI_PRESETS:
        path, first = CUPI_PRESETS[name_or_path]
    else:
        path, first = name_or_path.strip(), ()
        if not path.startswith("/vmrest/"):
            raise CLIError("Custom CUPI path must start with /vmrest/ (e.g. /vmrest/users)")
    return [flatten(i, first) for i in cupi.get_all(path)]
