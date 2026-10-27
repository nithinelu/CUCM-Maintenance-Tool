"""CUCM Maintenance Tool - DRS backup, upgrades / COP installs and data export,
with its own built-in SFTP server (no separate SFTP application needed)."""

import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox, scrolledtext

import cucm_ops as ops
from cucm_cli import CUCMShell, CLIError, run_on_nodes
from secrets_store import protect, unprotect
from sftp_server import SFTPServer, local_ipv4_addresses, source_ip_for

APP = "CUCM Maintenance Tool"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
REPORT_DIR = os.path.join(HERE, "reports")

BG, CARD, NAVY, TEAL, MUTED = "#F3F5F9", "#FFFFFF", "#1F3864", "#0E7C86", "#6B7280"
GREEN, RED, AMBER = "#1E8E3E", "#C62828", "#B26A00"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------
# Node picker: check-list of cluster nodes with a per-node status column
# --------------------------------------------------------------------------
class NodePicker(ttk.Frame):
    def __init__(self, parent, app, height=6, default_all=True):
        super().__init__(parent)
        self.app = app
        self.default_all = default_all
        self.checked = set()
        cols = ("sel", "host", "ip", "type", "role", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=height, selectmode="none")
        for c, t, w in (("sel", "✓", 34), ("host", "Hostname", 150), ("ip", "IP", 120),
                        ("type", "Type", 70), ("role", "Role", 90), ("status", "Status", 360)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=(c == "status"), anchor="center" if c == "sel" else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Button-1>", self._click)
        self.tree.tag_configure("ok", foreground=GREEN)
        self.tree.tag_configure("bad", foreground=RED)
        self.tree.tag_configure("run", foreground=AMBER)
        app.pickers.append(self)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        known = {n["host"] for n in self.app.nodes}
        self.checked &= known
        if not self.checked and self.default_all:
            self.checked = set(known)
        for n in self.app.nodes:
            self.tree.insert("", "end", iid=n["host"], values=(
                "☑" if n["host"] in self.checked else "☐", n.get("hostname", ""), n["host"],
                n.get("type", ""), n.get("role", ""), ""))

    def _click(self, ev):
        iid = self.tree.identify_row(ev.y)
        if not iid:
            if self.tree.identify_region(ev.x, ev.y) == "heading" and \
                    self.tree.identify_column(ev.x) == "#1":
                self.checked = set() if len(self.checked) == len(self.app.nodes) else \
                    {n["host"] for n in self.app.nodes}
                for n in self.app.nodes:
                    self.tree.set(n["host"], "sel", "☑" if n["host"] in self.checked else "☐")
            return
        if iid in self.checked:
            self.checked.discard(iid)
        else:
            self.checked.add(iid)
        self.tree.set(iid, "sel", "☑" if iid in self.checked else "☐")

    def selected(self):
        return [n for n in self.app.nodes if n["host"] in self.checked]

    def set_status(self, host, text, tag=""):
        def _do():
            if self.tree.exists(host):
                self.tree.set(host, "status", text)
                self.tree.item(host, tags=(tag,) if tag else ())
        self.app.ui(_do)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("1180x860")
        self.minsize(980, 700)
        self.cfg = self._load_cfg()
        self.q = queue.Queue()
        self.nodes = []
        self.pickers = []
        self.cancel = threading.Event()
        self.busy = False
        self.server = None
        self.transfers = {}
        self.console_shell = None
        self._partial = {}
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(REPORT_DIR, exist_ok=True)
        self._style()
        self._build()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.cfg.get("sftp_autostart"):
            self.after(300, self.start_sftp)

    # -- config -----------------------------------------------------------
    def _load_cfg(self):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_cfg(self):
        c = self.cfg
        c.update({
            "publisher": self.v_pub.get().strip(), "os_user": self.v_osuser.get().strip(),
            "axl_user": self.v_axluser.get().strip(),
            "nodes": self.nodes,
            "sftp_root": self.v_root.get().strip(), "sftp_ip": self.v_ip.get().strip(),
            "sftp_port": self.v_port.get().strip(), "sftp_user": self.v_suser.get().strip(),
            "sftp_password": protect(self.v_spw.get()),
            "sftp_autostart": bool(self.v_autostart.get()),
            "sftp_restrict": bool(self.v_restrict.get()),
            "drs_device": self.v_dev.get().strip(), "drs_keep": self.v_keep.get().strip(),
            "drs_features": self.v_feat.get().strip(), "drs_dir": self.v_bkdir.get().strip(),
        })
        if self.v_savepw.get():
            c["os_password"] = protect(self.v_ospw.get())
            c["axl_password"] = protect(self.v_axlpw.get())
        else:
            c.pop("os_password", None)
            c.pop("axl_password", None)
        c["save_passwords"] = bool(self.v_savepw.get())
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(c, f, indent=2)
        except OSError as e:
            self.log(f"Could not save config: {e}")

    # -- thread plumbing --------------------------------------------------
    def ui(self, fn, *a):
        self.q.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except Exception as e:
                    print("UI error:", e)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def log(self, msg):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        try:
            with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.ui(self._append, self.logbox, line + "\n")

    def _append(self, box, text):
        box.configure(state="normal")
        box.insert("end", text)
        if int(box.index("end-1c").split(".")[0]) > 6000:
            box.delete("1.0", "1000.0")
        box.see("end")
        box.configure(state="disabled")

    def cli_out(self, host):
        """on_output callback: write CLI output to the CLI tab, line by line, tagged by host."""
        def _cb(data):
            text = self._partial.get(host, "") + data
            lines = text.split("\n")
            self._partial[host] = lines.pop()
            if lines:
                self.ui(self._append, self.clibox, "".join(f"[{host}] {ln}\n" for ln in lines))
        return _cb

    def task(self, title, fn, *args):
        if self.busy:
            messagebox.showinfo(APP, "Another operation is still running - wait for it or press Stop.")
            return
        self._save_cfg()
        self.busy = True
        self.cancel.clear()
        self.status.set(f"Running: {title} ...")
        self.prog.configure(mode="indeterminate")
        self.prog.start(12)
        self.stop_btn.state(["!disabled"])

        def _run():
            try:
                fn(*args)
            except CLIError as e:
                self.log(f"{title}: {e}")
                self.ui(messagebox.showerror, APP, f"{title} failed:\n\n{str(e)[:1500]}")
            except Exception as e:
                self.log(f"{title}: unexpected error {type(e).__name__}: {e}")
                self.ui(messagebox.showerror, APP, f"{title} failed:\n\n{e}")
            finally:
                self.ui(self._done)
        threading.Thread(target=_run, daemon=True).start()

    def _done(self):
        self.busy = False
        self.prog.stop()
        self.prog.configure(mode="determinate", value=0)
        self.status.set("Ready")
        self.stop_btn.state(["disabled"])

    def stop(self):
        self.cancel.set()
        self.log("Stop requested")

    # -- credentials / helpers ----------------------------------------------
    def os_creds(self):
        u, p = self.v_osuser.get().strip(), self.v_ospw.get()
        if not u or not p:
            raise CLIError("Enter the OS (platform) administrator user and password on the Cluster tab.")
        return u, p

    def shell(self, host):
        u, p = self.os_creds()
        return CUCMShell(host, u, p, log=self.log)

    def publisher(self):
        for n in self.nodes:
            if n.get("role") == "Publisher" and n.get("type") == "CUCM":
                return n["host"]
        pub = self.v_pub.get().strip()
        if not pub:
            raise CLIError("Enter the CUCM publisher address on the Cluster tab.")
        return pub

    def sftp_params(self, need_running=True):
        ip = self.v_ip.get().strip()
        if not ip:
            raise CLIError("Set the 'IP address CUCM uses to reach this PC' on the SFTP Server tab.")
        route_ip = source_ip_for(self.v_pub.get().strip()) if self.v_pub.get().strip() else ""
        if ip not in local_ipv4_addresses() and ip != route_ip:
            # Typical after moving between networks (Wi-Fi / VPN / office LAN).
            if not route_ip:
                raise CLIError(f"SFTP IP {ip} is not an address of this PC - fix it on the SFTP Server tab.")
            self.log(f"SFTP IP {ip} is not an address of this PC any more - using {route_ip}, "
                     "the address this PC uses to reach the publisher")
            ip = route_ip
            self.ui(self.v_ip.set, ip)
        elif route_ip and ip != route_ip:
            self.log(f"Note: this PC reaches the publisher from {route_ip}, but the SFTP IP is {ip}. "
                     "If CUCM cannot connect, use 'Detect' on the SFTP Server tab.")
        if need_running and not (self.server and self.server.running):
            self.log("Built-in SFTP server is not running - starting it")
            self.ui(self.start_sftp)
            for _ in range(50):
                if self.server and self.server.running:
                    break
                threading.Event().wait(0.1)
            if not (self.server and self.server.running):
                raise CLIError("The built-in SFTP server is not running (SFTP Server tab).")
        return ip, int(self.v_port.get() or 22), self.v_suser.get().strip(), self.v_spw.get()

    def sftp_hint(self, host, since):
        """Explain a CUCM->SFTP failure when the server never even saw a TCP connection."""
        if not self.server:
            return ""
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            ip = host
        if self.server.seen.get(ip, 0) >= since:
            return ""
        return (f"\n\nCUCM ({ip}) never connected to the built-in SFTP server at "
                f"{self.v_ip.get()}:{self.v_port.get()}. Check that this IP is reachable from CUCM "
                "(no NAT / VPN in between), the Windows Firewall rule, and that nothing else "
                "(e.g. OpenSSH Server) owns the port.")

    def root(self, *sub):
        p = os.path.join(self.v_root.get().strip() or os.path.join(HERE, "sftp_root"), *sub)
        os.makedirs(p, exist_ok=True)
        return p

    # -- look -------------------------------------------------------------
    def _style(self):
        self.configure(bg=BG)
        s = ttk.Style(self)
        s.theme_use("clam")
        fs = 10
        s.configure(".", font=("Segoe UI", fs), background=BG)
        for w in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton"):
            s.configure(w, background=BG)
        s.configure("TLabelframe", background=BG, bordercolor="#DDE3EC", relief="solid")
        s.configure("TLabelframe.Label", background=BG, foreground=NAVY, font=("Segoe UI", fs, "bold"))
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(6, 6, 6, 0))
        s.configure("TNotebook.Tab", padding=(16, 8), font=("Segoe UI", fs, "bold"), background="#E3E8F0")
        s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", NAVY)])
        s.configure("TButton", padding=(12, 5))
        s.configure("Accent.TButton", background=TEAL, foreground="white", bordercolor=TEAL)
        s.map("Accent.TButton", background=[("active", "#0B6B73"), ("disabled", "#9CC9CD")])
        s.configure("Danger.TButton", background=RED, foreground="white", bordercolor=RED)
        s.map("Danger.TButton", background=[("active", "#A31F1F"), ("disabled", "#E3A5A5")])
        s.configure("Treeview", rowheight=26, fieldbackground=CARD, background=CARD)
        s.configure("Treeview.Heading", font=("Segoe UI", fs - 1, "bold"), background="#E7ECF4",
                    foreground=NAVY, relief="flat")
        s.configure("TProgressbar", background=TEAL, troughcolor="#E3E8F0")
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Ok.TLabel", foreground=GREEN)
        s.configure("Bad.TLabel", foreground=RED)
        s.configure("Head.TLabel", foreground=NAVY, font=("Segoe UI", fs + 2, "bold"))

    def _build(self):
        head = tk.Frame(self, bg=NAVY, height=56)
        head.pack(fill="x")
        tk.Label(head, text="☎", font=("Segoe UI Symbol", 18), fg="white", bg=NAVY).pack(side="left", padx=(16, 4))
        tk.Label(head, text=APP, font=("Segoe UI", 15, "bold"), fg="white", bg=NAVY).pack(side="left")
        tk.Label(head, text="  Backup · Upgrade · COP · Export  —  built-in SFTP server",
                 font=("Segoe UI", 10), fg="#C9D3E6", bg=NAVY).pack(side="left", pady=14)
        self.sftp_pill = tk.Label(head, text="  ● SFTP stopped  ", font=("Segoe UI", 9, "bold"),
                                  fg="white", bg="#6B7280")
        self.sftp_pill.pack(side="right", padx=16)
        self.conn_pill = tk.Label(head, text="  ● No cluster  ", font=("Segoe UI", 9, "bold"),
                                  fg="white", bg="#6B7280")
        self.conn_pill.pack(side="right")

        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", style="Danger.TButton", command=self.stop)
        self.stop_btn.pack(side="right")
        self.stop_btn.state(["disabled"])
        self.prog = ttk.Progressbar(bar, length=260)
        self.prog.pack(side="right", padx=10)

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self.nb = ttk.Notebook(pw)
        pw.add(self.nb, weight=3)

        lognb = ttk.Notebook(pw)
        pw.add(lognb, weight=1)
        self.logbox = self._console(lognb)
        lognb.add(self.logbox.holder, text="Activity log")
        self.clibox = self._console(lognb)
        lognb.add(self.clibox.holder, text="CLI output")

        for title, builder in (("Cluster", self._tab_cluster), ("SFTP Server", self._tab_sftp),
                               ("Backup (DRS)", self._tab_backup), ("Upgrade / COP", self._tab_upgrade),
                               ("Data Export", self._tab_export), ("CLI Console", self._tab_console)):
            f = ttk.Frame(self.nb, padding=14)
            self.nb.add(f, text=title)
            builder(f)
        self.nodes = self.cfg.get("nodes", [])
        self._nodes_changed()

    def _console(self, parent):
        fr = ttk.Frame(parent)
        box = scrolledtext.ScrolledText(fr, height=9, bg="#111827", fg="#D1D5DB", insertbackground="white",
                                        font=("Consolas", 9), state="disabled", relief="flat")
        box.pack(fill="both", expand=True)
        box.holder = fr
        return box

    def _row(self, parent, r, label, var, show=None, width=34, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        e = ttk.Entry(parent, textvariable=var, width=width, show=show)
        e.grid(row=r, column=1, sticky="w", pady=3)
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=8)
        return e

    # ======================================================================
    # Cluster tab
    # ======================================================================
    def _tab_cluster(self, f):
        c = self.cfg
        self.v_pub = tk.StringVar(value=c.get("publisher", ""))
        self.v_osuser = tk.StringVar(value=c.get("os_user", "administrator"))
        self.v_ospw = tk.StringVar(value=unprotect(c.get("os_password", "")))
        self.v_axluser = tk.StringVar(value=c.get("axl_user", ""))
        self.v_axlpw = tk.StringVar(value=unprotect(c.get("axl_password", "")))
        self.v_savepw = tk.BooleanVar(value=c.get("save_passwords", False))

        top = ttk.Frame(f)
        top.pack(fill="x")
        lf = ttk.LabelFrame(top, text="Platform (OS) administrator - SSH CLI", padding=10)
        lf.pack(side="left", fill="y")
        self._row(lf, 0, "Publisher IP / FQDN", self.v_pub, width=28)
        self._row(lf, 1, "OS admin user", self.v_osuser, width=28)
        self._row(lf, 2, "OS admin password", self.v_ospw, show="•", width=28)
        ttk.Checkbutton(lf, text="Remember passwords (encrypted for this Windows user)",
                        variable=self.v_savepw).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        af = ttk.LabelFrame(top, text="Application user - AXL (only for Excel inventory export)", padding=10)
        af.pack(side="left", fill="y", padx=10)
        self._row(af, 0, "AXL user", self.v_axluser, width=24)
        self._row(af, 1, "AXL password", self.v_axlpw, show="•", width=24)
        ttk.Label(af, text="Needs the 'Standard AXL API Access' role.", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w")

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=10)
        ttk.Button(btns, text="Connect && discover nodes", style="Accent.TButton",
                   command=lambda: self.task("Discover cluster", self._discover)).pack(side="left")
        ttk.Button(btns, text="Refresh versions",
                   command=lambda: self.task("Versions", self._versions)).pack(side="left", padx=6)
        ttk.Button(btns, text="Health check report",
                   command=lambda: self.task("Health check", self._health, False)).pack(side="left")
        ttk.Button(btns, text="Health check + diagnose test",
                   command=lambda: self.task("Health check", self._health, True)).pack(side="left", padx=6)
        ttk.Button(btns, text="Dry run (no changes)", style="Accent.TButton",
                   command=lambda: self.task("Dry run", self._dry_run)).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Open reports folder",
                   command=lambda: os.startfile(REPORT_DIR)).pack(side="left")

        cols = ("host", "ip", "type", "role", "active", "inactive", "cops")
        self.ctree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for col, t, w in (("host", "Hostname", 150), ("ip", "IP", 120), ("type", "Type", 70),
                          ("role", "Role", 90), ("active", "Active version", 170),
                          ("inactive", "Inactive version", 170), ("cops", "Installed COP files", 300)):
            self.ctree.heading(col, text=t)
            self.ctree.column(col, width=w, stretch=(col == "cops"))
        self.ctree.pack(fill="both", expand=True)
        ttk.Label(f, style="Muted.TLabel", justify="left", text=(
            "Nodes come from 'show network cluster' on the publisher (CUCM + IM&P). The same OS admin "
            "credentials are used for every node.\nHealth check (pre/post upgrade) runs version, cluster, "
            "status, NTP, DB replication, services and core-dump checks on the nodes checked on the "
            "Upgrade / COP tab and saves a text report.")).pack(anchor="w", pady=(8, 0))

    def _nodes_changed(self):
        self.ctree.delete(*self.ctree.get_children())
        for n in self.nodes:
            self.ctree.insert("", "end", iid=n["host"], values=(
                n.get("hostname", ""), n["host"], n.get("type", ""), n.get("role", ""),
                n.get("active", ""), n.get("inactive", ""), ", ".join(n.get("cops", []))))
        for p in self.pickers:
            p.refresh()
        if self.nodes:
            pub = next((n for n in self.nodes if n.get("role") == "Publisher"), self.nodes[0])
            self.conn_pill.configure(text=f"  ● {pub.get('hostname') or pub['host']}  |  "
                                          f"{len(self.nodes)} node(s)  ", bg=GREEN)
        self._console_nodes()

    def _discover(self):
        pub = self.v_pub.get().strip()
        if not pub:
            raise CLIError("Enter the publisher address.")
        with self.shell(pub) as sh:
            nodes, raw = ops.discover_nodes(sh)
            if not nodes:
                self.log("Could not parse 'show network cluster' - using the publisher only:\n" + raw)
                nodes = [{"host": pub, "hostname": pub, "role": "Publisher", "type": "CUCM"}]
            v = ops.versions(sh)
            for n in nodes:
                if n["host"] == pub or n["fqdn"].lower() == pub.lower() or n["hostname"].lower() == pub.lower():
                    n.update(active=v["active"], inactive=v["inactive"], cops=v["cops"])
        self.nodes = nodes
        self.log(f"Discovered {len(nodes)} node(s): " + ", ".join(f"{n['hostname']} ({n['host']})" for n in nodes))
        self.ui(self._nodes_changed)
        self.ui(self._save_cfg)

    def _versions(self):
        if not self.nodes:
            raise CLIError("Discover the cluster first.")

        def one(n):
            with self.shell(n["host"]) as sh:
                v = ops.versions(sh)
            n.update(active=v["active"], inactive=v["inactive"], cops=v["cops"])
            self.log(f"[{n['host']}] active {v['active']}, inactive {v['inactive']}")
        run_on_nodes(self.nodes, one, parallel=True, log=self.log)
        self.ui(self._nodes_changed)

    def _health(self, diagnose):
        nodes = self.up_picker.selected() or self.nodes
        if not nodes:
            raise CLIError("Discover the cluster first.")
        path = os.path.join(REPORT_DIR, f"health-{stamp()}.txt")
        parts = {}

        def one(n):
            with self.shell(n["host"]) as sh:
                parts[n["host"]] = ops.health_check(sh, diagnose, on_output=self.cli_out(n["host"]),
                                                    cancel=self.cancel)
        run_on_nodes(nodes, one, parallel=True, log=self.log)
        with open(path, "w", encoding="utf-8") as fh:
            for n in nodes:
                fh.write(f"#################### {n.get('hostname')} ({n['host']}) ####################\n")
                fh.write(parts.get(n["host"], "(failed - see activity log)") + "\n\n")
        self.log(f"Health report saved: {path}")
        os.startfile(path)

    def _dry_run(self):
        """Read-only end-to-end check: SSH/CLI, versions, DRS read, CUCM->SFTP both ways, AXL.
        Writes reports\\dryrun-<time>.txt with a summary and the full CLI transcript per node."""
        if not self.nodes:
            self._discover()
        nodes = self.up_picker.selected() or self.nodes
        ip, port, user, pw = self.sftp_params()
        ts = stamp()
        pub = self.publisher()
        results, transcripts = {}, {}

        def one(n):
            host = n["host"]
            transcripts[host] = []
            rel = f"export/dryrun-{ts}/{n.get('hostname') or host}"
            self.root(*rel.split("/"))
            self.up_picker.set_status(host, "dry run…", "run")
            try:
                sh = self.shell(host)
                sh.tap = transcripts[host].append
                since = time.time()
                with sh:
                    results[host] = ops.dry_run_node(sh, ip, user, pw, port, "/" + rel,
                                                     publisher=(host == pub),
                                                     on_output=self.cli_out(host), cancel=self.cancel)
                hint = self.sftp_hint(host, since).strip()
                if hint and any(not ok for name, ok, _ in results[host] if "SFTP" in name):
                    results[host].append(("CUCM -> SFTP connection seen by this PC", False, hint))
            except Exception as e:
                results[host] = [("SSH login + CLI prompt", False, str(e))]
            bad = sum(1 for _, ok, _ in results[host] if not ok)
            self.up_picker.set_status(host, "dry run: all checks OK" if not bad else
                                      f"dry run: {bad} check(s) failed", "ok" if not bad else "bad")

        run_on_nodes(nodes, one, parallel=True, log=self.log)

        axl_line = "AXL: skipped (no AXL user entered)"
        if self.v_axluser.get().strip() and self.v_axlpw.get():
            try:
                axl = ops.AXLSQL(pub, self.v_axluser.get().strip(), self.v_axlpw.get())
                rows = axl.query("SELECT COUNT(*) AS devices FROM device")
                axl_line = f"AXL: OK (schema {axl.version}, {rows[0].get('devices', '?')} devices)"
            except Exception as e:
                axl_line = f"AXL: FAILED - {e}"

        path = os.path.join(REPORT_DIR, f"dryrun-{ts}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{APP} - dry run {datetime.now():%Y-%m-%d %H:%M}\n"
                     f"Publisher {pub} | SFTP {ip}:{port} user {user} | root {self.root()}\n{axl_line}\n\n")
            for n in nodes:
                fh.write(f"=== {n.get('hostname')} ({n['host']}) ===\n")
                for name, ok, detail in results.get(n["host"], []):
                    fh.write(f"  [{'OK ' if ok else 'FAIL'}] {name}\n")
                    for ln in str(detail).splitlines()[:25]:
                        fh.write(f"         {ln}\n")
                fh.write("\n")
            for n in nodes:
                fh.write(f"\n#################### CLI transcript {n['host']} ####################\n")
                fh.write("".join(transcripts.get(n["host"], [])).replace("\r", ""))
        total = sum(len(r) for r in results.values())
        failed = sum(1 for r in results.values() for _, ok, _ in r if not ok)
        self.log(f"Dry run: {total - failed}/{total} checks OK. {axl_line}\nReport: {path}")
        os.startfile(path)

    # ======================================================================
    # SFTP tab
    # ======================================================================
    def _tab_sftp(self, f):
        c = self.cfg
        ips = local_ipv4_addresses()
        self.v_root = tk.StringVar(value=c.get("sftp_root", os.path.join(HERE, "sftp_root")))
        self.v_ip = tk.StringVar(value=c.get("sftp_ip") or (ips[0] if ips else ""))
        self.v_port = tk.StringVar(value=c.get("sftp_port", "22"))
        self.v_suser = tk.StringVar(value=c.get("sftp_user", "cucmsftp"))
        self.v_spw = tk.StringVar(value=unprotect(c.get("sftp_password", "")) or
                                  "Cu" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y"))
        self.v_autostart = tk.BooleanVar(value=c.get("sftp_autostart", False))
        self.v_restrict = tk.BooleanVar(value=c.get("sftp_restrict", False))

        lf = ttk.LabelFrame(f, text="Built-in SFTP server", padding=10)
        lf.pack(fill="x")
        ttk.Label(lf, text="Root folder").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(lf, textvariable=self.v_root, width=60).grid(row=0, column=1, sticky="we", pady=3)
        ttk.Button(lf, text="Browse…", command=self._pick_root).grid(row=0, column=2, padx=6)
        ttk.Button(lf, text="Open", command=lambda: os.startfile(self.root())).grid(row=0, column=3)
        ttk.Label(lf, text="IP address CUCM uses to reach this PC").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 8))
        ipf = ttk.Frame(lf)
        ipf.grid(row=1, column=1, sticky="w")
        ttk.Combobox(ipf, textvariable=self.v_ip, values=ips, width=20).pack(side="left")
        ttk.Button(ipf, text="Detect", command=self._detect_ip).pack(side="left", padx=6)
        ttk.Label(ipf, text="(the address this PC uses to reach the publisher)",
                  style="Muted.TLabel").pack(side="left")
        self._row(lf, 2, "Port", self.v_port, width=8, hint="22 is what CUCM expects (DRS on older releases has no port option)")
        self._row(lf, 3, "SFTP user", self.v_suser, width=20)
        self._row(lf, 4, "SFTP password", self.v_spw, width=20, hint="CUCM stores this in the DRS backup device")
        ttk.Checkbutton(lf, text="Start the SFTP server when the tool opens (needed for scheduled DRS backups)",
                        variable=self.v_autostart).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(lf, text="Only accept connections from discovered cluster nodes",
                        variable=self.v_restrict).grid(row=6, column=0, columnspan=3, sticky="w")
        lf.columnconfigure(1, weight=1)

        b = ttk.Frame(f)
        b.pack(fill="x", pady=10)
        self.sftp_start_btn = ttk.Button(b, text="Start SFTP server", style="Accent.TButton", command=self.start_sftp)
        self.sftp_start_btn.pack(side="left")
        self.sftp_stop_btn = ttk.Button(b, text="Stop", command=self.stop_sftp)
        self.sftp_stop_btn.pack(side="left", padx=6)
        self.sftp_stop_btn.state(["disabled"])
        ttk.Button(b, text="Allow port in Windows Firewall (admin)", command=self._firewall).pack(side="left", padx=12)
        self.sftp_info = ttk.Label(b, text="", style="Muted.TLabel")
        self.sftp_info.pack(side="left", padx=10)

        ttk.Label(f, text="Transfers", style="Head.TLabel").pack(anchor="w")
        cols = ("time", "peer", "file", "dir", "size")
        self.xtree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for col, t, w in (("time", "Time", 80), ("peer", "CUCM node", 120), ("file", "File", 420),
                          ("dir", "Direction", 120), ("size", "Bytes", 110)):
            self.xtree.heading(col, text=t)
            self.xtree.column(col, width=w, stretch=(col == "file"))
        self.xtree.pack(fill="both", expand=True)
        ttk.Label(f, style="Muted.TLabel", justify="left", text=(
            "Folders under the root: backup\\ (DRS backups land here), upgrade\\ (put ISO / COP files here), "
            "export\\ (file get exports).\nCUCM sees the root as '/', e.g. backup directory '/backup', "
            "upgrade directory '/upgrade'.")).pack(anchor="w", pady=(6, 0))

    def _detect_ip(self):
        pub = self.v_pub.get().strip()
        ip = source_ip_for(pub) if pub else ""
        if not ip:
            messagebox.showwarning(APP, "Enter the publisher on the Cluster tab first (and check it resolves).")
            return
        self.v_ip.set(ip)
        self.log(f"SFTP IP set to {ip} (route to {pub})")

    def _pick_root(self):
        d = filedialog.askdirectory(initialdir=self.v_root.get() or HERE)
        if d:
            self.v_root.set(os.path.normpath(d))

    def start_sftp(self):
        if self.server and self.server.running:
            return
        user, pw = self.v_suser.get().strip(), self.v_spw.get()
        if not user or len(pw) < 6:
            messagebox.showerror(APP, "Set an SFTP user and a password of at least 6 characters.")
            return
        for sub in ("backup", "upgrade", "export"):
            self.root(sub)
        allowed = [n["host"] for n in self.nodes] if self.v_restrict.get() else None
        self.server = SFTPServer(self.root(), user, pw, port=int(self.v_port.get() or 22),
                                 host_key_path=os.path.join(HERE, "data", "sftp_host_rsa.key"),
                                 allowed_ips=allowed, log=self.log, progress=self._sftp_progress)
        try:
            self.server.start()
        except OSError as e:
            self.server = None
            messagebox.showerror(APP, str(e))
            return
        self._save_cfg()
        self.sftp_start_btn.state(["disabled"])
        self.sftp_stop_btn.state(["!disabled"])
        self.sftp_pill.configure(text=f"  ● SFTP {self.v_ip.get()}:{self.v_port.get()}  ", bg=GREEN)
        self.sftp_info.configure(text=f"Running - CUCM connects to {self.v_ip.get()} as {user}", style="Ok.TLabel")

    def stop_sftp(self):
        if self.server:
            self.server.stop()
        self.server = None
        self.sftp_start_btn.state(["!disabled"])
        self.sftp_stop_btn.state(["disabled"])
        self.sftp_pill.configure(text="  ● SFTP stopped  ", bg="#6B7280")
        self.sftp_info.configure(text="Stopped", style="Muted.TLabel")

    def _sftp_progress(self, peer, path, kind, nbytes):
        def _do():
            key = (peer, path)
            label = {"upload": "CUCM → PC", "download": "PC → CUCM"}.get(kind)
            if key in self.transfers and self.xtree.exists(self.transfers[key]):
                iid = self.transfers[key]
                self.xtree.set(iid, "size", f"{nbytes:,}")
                if kind == "done":
                    self.xtree.set(iid, "dir", self.xtree.set(iid, "dir").replace(" …", "") + " ✓")
                    self.transfers.pop(key, None)
            elif kind != "done":
                iid = self.xtree.insert("", 0, values=(f"{datetime.now():%H:%M:%S}", peer, path,
                                                       label + " …", f"{nbytes:,}"))
                self.transfers[key] = iid
        self.ui(_do)

    def _firewall(self):
        port = int(self.v_port.get() or 22)
        args = (f'/c netsh advfirewall firewall delete rule name="{APP} SFTP" & '
                f'netsh advfirewall firewall add rule name="{APP} SFTP" dir=in action=allow '
                f'protocol=TCP localport={port}')
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", args, None, 0)
        self.log(f"Firewall rule for TCP {port}: " + ("requested (approve the UAC prompt)" if rc > 32 else "cancelled"))

    # ======================================================================
    # Backup tab
    # ======================================================================
    def _tab_backup(self, f):
        c = self.cfg
        self.v_dev = tk.StringVar(value=c.get("drs_device", "MAINT_TOOL_SFTP"))
        self.v_keep = tk.StringVar(value=c.get("drs_keep", "3"))
        self.v_feat = tk.StringVar(value=c.get("drs_features", ops.DEFAULT_FEATURES))
        self.v_bkdir = tk.StringVar(value=c.get("drs_dir", "/backup"))

        lf = ttk.LabelFrame(f, text="DRS backup to the built-in SFTP server (runs on the publisher)", padding=10)
        lf.pack(fill="x")
        self._row(lf, 0, "Backup device name", self.v_dev, width=24)
        self._row(lf, 1, "Directory on SFTP", self.v_bkdir, width=24, hint="/backup = <root>\\backup")
        self._row(lf, 2, "Backups to keep", self.v_keep, width=6, hint="DRS deletes older backups beyond this")
        self._row(lf, 3, "Features", self.v_feat, width=40,
                  hint="UCM, CDR_CAR, PLM (older), CUP / IM&P are added by DRS on IM&P clusters")

        b = ttk.Frame(f)
        b.pack(fill="x", pady=10)
        ttk.Button(b, text="Run backup now", style="Accent.TButton",
                   command=lambda: self.task("DRS backup", self._backup)).pack(side="left")
        ttk.Button(b, text="Create / update backup device",
                   command=lambda: self.task("DRS device", self._backup_device)).pack(side="left", padx=6)
        for text, fn in (("Status", lambda sh: ops.drs_status(sh)["raw"]),
                         ("History", ops.drs_history), ("Device list", ops.drs_device_list),
                         ("Schedules", ops.drs_schedule_list)):
            ttk.Button(b, text=text, command=lambda t=text, fn=fn: self.task(
                f"DRS {t.lower()}", self._pub_cmd, fn, f"DRS {t}")).pack(side="left", padx=2)
        ttk.Button(b, text="Cancel running backup", style="Danger.TButton",
                   command=self._cancel_backup).pack(side="right")

        sf = ttk.Frame(f)
        sf.pack(fill="x")
        self.bk_status = ttk.Label(sf, text="", style="Head.TLabel")
        self.bk_status.pack(side="left")
        self.bk_prog = ttk.Progressbar(sf, length=300, maximum=100)
        self.bk_prog.pack(side="right")

        cols = ("comp", "server", "feature", "state")
        self.bk_tree = ttk.Treeview(f, columns=cols, show="headings", height=5)
        for col, t in zip(cols, ("Feature", "Server", "Component", "Result")):
            self.bk_tree.heading(col, text=t)
            self.bk_tree.column(col, width=160)
        self.bk_tree.pack(fill="x", pady=6)

        hdr = ttk.Frame(f)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Backups on this PC", style="Head.TLabel").pack(side="left")
        ttk.Button(hdr, text="Refresh", command=self._list_backups).pack(side="right")
        ttk.Button(hdr, text="Open folder", command=lambda: os.startfile(self._bk_local())).pack(side="right", padx=6)
        self.bl_tree = ttk.Treeview(f, columns=("name", "size", "time"), show="headings", height=6)
        for col, t, w in (("name", "File", 420), ("size", "Size", 110), ("time", "Modified", 160)):
            self.bl_tree.heading(col, text=t)
            self.bl_tree.column(col, width=w, stretch=(col == "name"))
        self.bl_tree.pack(fill="both", expand=True)
        self.after(500, self._list_backups)

    def _bk_local(self):
        rel = self.v_bkdir.get().strip().strip("/").replace("/", os.sep)
        return self.root(*([rel] if rel else []))

    def _list_backups(self):
        self.bl_tree.delete(*self.bl_tree.get_children())
        base = self._bk_local()
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                p = os.path.join(dirpath, name)
                st = os.stat(p)
                self.bl_tree.insert("", "end", values=(os.path.relpath(p, base), fmt_size(st.st_size),
                                                       datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")))

    def _pub_cmd(self, fn, title):
        with self.shell(self.publisher()) as sh:
            out = fn(sh)
        self.log(f"{title}:\n{out}")

    def _backup_device(self, sh=None):
        ip, port, user, pw = self.sftp_params()
        path = self.v_bkdir.get().strip() or "/backup"
        self._bk_local()
        own = sh is None
        sh = sh or self.shell(self.publisher()).connect()
        try:
            since = time.time()
            try:
                out = ops.drs_add_device(sh, self.v_dev.get().strip(), path, ip, user, pw,
                                         int(self.v_keep.get() or 3))
            except CLIError as e:
                raise CLIError(str(e) + self.sftp_hint(sh.host, since)) from e
            self.log(f"DRS device {self.v_dev.get()} -> sftp://{user}@{ip}{path}\n{out}")
        finally:
            if own:
                sh.close()

    def _backup(self):
        pub = self.publisher()
        with self.shell(pub) as sh:
            self._backup_device(sh)
            out = ops.drs_start_backup(sh, self.v_feat.get(), self.v_dev.get().strip())
            self.log(f"Backup started on {pub}: {out.strip()}")

            def show(st):
                def _do():
                    self.bk_status.configure(text=f"{st['status'] or 'Running'}  {st['tar']}")
                    if st["percent"] is not None:
                        self.bk_prog.configure(value=st["percent"])
                    self.bk_tree.delete(*self.bk_tree.get_children())
                    for comp in st["components"]:
                        self.bk_tree.insert("", "end", values=comp)
                self.ui(_do)
            st = ops.drs_wait(sh, poll=15, cancel=self.cancel, on_status=show)
        ok = "SUCCESS" in st["status"].upper()
        self.log(f"Backup finished: {st['status']}\n{st['raw']}")
        self.ui(self._list_backups)
        self.ui(messagebox.showinfo if ok else messagebox.showwarning, APP,
                f"DRS backup finished: {st['status']}\nTar file: {st['tar']}")

    def _cancel_backup(self):
        if messagebox.askyesno(APP, "Cancel the DRS backup running on the publisher?"):
            threading.Thread(target=lambda: self._safe(self._pub_cmd, ops.drs_cancel, "Cancel backup"),
                             daemon=True).start()

    def _safe(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            self.log(f"Error: {e}")

    # ======================================================================
    # Upgrade / COP tab
    # ======================================================================
    def _tab_upgrade(self, f):
        self.v_switch = tk.BooleanVar(value=False)
        self.v_mode = tk.StringVar(value="sequential")
        top = ttk.Frame(f)
        top.pack(fill="both", expand=True)

        left = ttk.LabelFrame(top, text="Files in <root>\\upgrade (CUCM directory /upgrade)", padding=8)
        left.pack(side="left", fill="both", expand=True)
        self.uf_tree = ttk.Treeview(left, columns=("name", "size"), show="headings", height=7, selectmode="browse")
        self.uf_tree.heading("name", text="ISO / COP file")
        self.uf_tree.heading("size", text="Size")
        self.uf_tree.column("name", width=380)
        self.uf_tree.column("size", width=90)
        self.uf_tree.pack(fill="both", expand=True)
        ub = ttk.Frame(left)
        ub.pack(fill="x", pady=(6, 0))
        ttk.Button(ub, text="Add files…", command=self._add_upgrade_files).pack(side="left")
        ttk.Button(ub, text="Refresh", command=self._list_upgrade_files).pack(side="left", padx=4)
        ttk.Button(ub, text="Checksum (SHA-512 / MD5)",
                   command=lambda: self.task("Checksum", self._checksum)).pack(side="left")
        ttk.Button(ub, text="Open folder", command=lambda: os.startfile(self.root("upgrade"))).pack(side="left", padx=4)

        right = ttk.LabelFrame(top, text="Options", padding=8)
        right.pack(side="left", fill="y", padx=(10, 0))
        ttk.Checkbutton(right, text="Switch version automatically after\na successful upgrade (node reboots)",
                        variable=self.v_switch).pack(anchor="w")
        ttk.Label(right, text="Run on the checked nodes:").pack(anchor="w", pady=(10, 0))
        ttk.Radiobutton(right, text="One after another (publisher first)", value="sequential",
                        variable=self.v_mode).pack(anchor="w")
        ttk.Radiobutton(right, text="All at the same time", value="parallel",
                        variable=self.v_mode).pack(anchor="w")
        ttk.Label(right, style="Muted.TLabel", justify="left", wraplength=280, text=(
            "COP files: usually all nodes, parallel is fine.\nUpgrades: publisher first, then subscribers. "
            "Leave auto-switch off and use Switch version in the maintenance window "
            "(publisher first, wait for it to come up, then subscribers).")).pack(anchor="w", pady=(10, 0))

        ttk.Label(f, text="Target nodes (click to check / uncheck, click ✓ header for all)",
                  style="Head.TLabel").pack(anchor="w", pady=(10, 2))
        self.up_picker = NodePicker(f, self, height=5)
        self.up_picker.pack(fill="both", expand=True)

        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="Install selected file on checked nodes", style="Accent.TButton",
                   command=self._install).pack(side="left")
        ttk.Button(b, text="Upgrade status",
                   command=lambda: self.task("Upgrade status", self._on_nodes, ops.upgrade_status, "upgrade status")).pack(side="left", padx=6)
        ttk.Button(b, text="Show versions",
                   command=lambda: self.task("Versions", self._versions)).pack(side="left")
        ttk.Button(b, text="Switch version on checked nodes…", style="Danger.TButton",
                   command=self._switch).pack(side="right")
        self.after(400, self._list_upgrade_files)

    def _list_upgrade_files(self):
        self.uf_tree.delete(*self.uf_tree.get_children())
        for name, size in ops.upgrade_files(self.root("upgrade")):
            self.uf_tree.insert("", "end", iid=name, values=(name, fmt_size(size)))

    def _add_upgrade_files(self):
        paths = filedialog.askopenfilenames(title="Select CUCM upgrade ISO / COP files", filetypes=[
            ("CUCM upgrade / COP", "*.iso *.cop *.sgn *.sha512"), ("All files", "*.*")])
        if not paths:
            return
        dest = self.root("upgrade")

        def copy():
            for p in paths:
                self.log(f"Copying {os.path.basename(p)} ({fmt_size(os.path.getsize(p))}) to {dest}")
                shutil.copy2(p, dest)
            self.ui(self._list_upgrade_files)
        self.task("Copy upgrade files", copy)

    def _sel_file(self):
        sel = self.uf_tree.selection()
        if not sel:
            raise CLIError("Select an ISO / COP file in the list first.")
        return sel[0]

    def _checksum(self):
        name = self._sel_file()
        path = os.path.join(self.root("upgrade"), name)

        def prog(done, total):
            self.ui(self.prog.configure, {"mode": "determinate", "maximum": total, "value": done})
        self.ui(self.prog.stop)
        sha = ops.file_hash(path, "sha512", prog)
        md5 = ops.file_hash(path, "md5", prog)
        self.log(f"{name}\n  SHA-512: {sha}\n  MD5:     {md5}\n  Compare with the checksum on software.cisco.com")

    def _install(self):
        try:
            name = self._sel_file()
            nodes = self.up_picker.selected()
            if not nodes:
                raise CLIError("Check at least one target node.")
        except CLIError as e:
            messagebox.showwarning(APP, str(e))
            return
        is_cop = ".cop" in name.lower()
        kind = "COP file" if is_cop else "UPGRADE"
        extra = "" if is_cop else ("\n\nThe nodes will REBOOT into the new version when done." if self.v_switch.get()
                                   else "\n\nThe new version goes to the inactive partition (no reboot).")
        if not messagebox.askyesno(APP, f"Install {kind}\n\n{name}\n\non {len(nodes)} node(s):\n"
                                        + "\n".join(f"  • {n.get('hostname')} ({n['host']})" for n in nodes) + extra):
            return
        self.task(f"Install {name}", self._install_run, name, nodes)

    def _install_run(self, name, nodes):
        ip, port, user, pw = self.sftp_params()
        auto = self.v_switch.get() and ".cop" not in name.lower()
        picker = self.up_picker

        def one(n):
            host = n["host"]
            picker.set_status(host, "connecting…", "run")
            with self.shell(host) as sh:
                picker.set_status(host, f"installing {name} …", "run")
                ok, out = ops.upgrade_initiate(sh, ip, user, pw, "/upgrade", name, auto_switch=auto,
                                               sftp_port=port, on_output=self.cli_out(host), cancel=self.cancel)
            tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or [""]
            picker.set_status(host, ("✓ " if ok else "? ") + tail[0][:120], "ok" if ok else "bad")
            self.log(f"[{host}] install {name}: {'success' if ok else 'finished - check CLI output'}")
            if not ok:
                raise CLIError("installation result not confirmed - see CLI output tab")

        order = sorted(nodes, key=lambda n: (n.get("role") != "Publisher", n.get("type") != "CUCM"))
        if self.v_mode.get() == "parallel":
            res = run_on_nodes(order, one, parallel=True, log=self.log)
        else:
            res = {}
            for n in order:
                if self.cancel.is_set():
                    break
                res.update(run_on_nodes([n], one, parallel=False, log=self.log))
                if not res[n["host"]][0] and not (".cop" in name.lower()):
                    self.log("Stopping: upgrade failed on a node - remaining nodes not started.")
                    break
        for host, (ok, err) in res.items():
            if not ok:
                picker.set_status(host, f"✗ {str(err).splitlines()[0][:140]}", "bad")
        good = sum(1 for ok, _ in res.values() if ok)
        self.log(f"Install {name}: {good}/{len(nodes)} node(s) OK")
        self._versions()

    def _switch(self):
        nodes = self.up_picker.selected()
        if not nodes:
            messagebox.showwarning(APP, "Check the nodes to switch.")
            return
        if not messagebox.askyesno(APP, "Switch version and REBOOT on:\n" + "\n".join(
                f"  • {n.get('hostname')} ({n['host']})" for n in nodes) +
                "\n\nThis interrupts call processing on these nodes. Continue?", icon="warning"):
            return
        self.task("Switch version", self._on_nodes, lambda sh: ops.switch_version(sh, sh._cb),
                  "switch-version", nodes, False)

    def _on_nodes(self, fn, title, nodes=None, parallel=True):
        nodes = nodes or self.up_picker.selected()
        if not nodes:
            raise CLIError("Check at least one node.")

        def one(n):
            with self.shell(n["host"]) as sh:
                sh._cb = self.cli_out(n["host"])
                out = fn(sh)
            self.log(f"[{n['host']}] {title}:\n{out}")
            self.up_picker.set_status(n["host"], f"{title}: done", "ok")
        run_on_nodes(nodes, one, parallel=parallel, log=self.log)

    # ======================================================================
    # Data export tab
    # ======================================================================
    def _tab_export(self, f):
        self.v_preset = tk.StringVar(value=list(ops.FILE_PRESETS)[0])
        self.v_loc = tk.StringVar()
        self.v_spec = tk.StringVar()
        self.v_opt = tk.StringVar()

        lf = ttk.LabelFrame(f, text="Files from the nodes ('file get' to the built-in SFTP server)", padding=10)
        lf.pack(fill="x")
        ttk.Label(lf, text="What").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(lf, textvariable=self.v_preset, values=list(ops.FILE_PRESETS), state="readonly", width=40)
        cb.grid(row=0, column=1, columnspan=3, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda e: self._preset())
        ttk.Label(lf, text="Location").grid(row=1, column=0, sticky="w")
        ttk.Combobox(lf, textvariable=self.v_loc, values=["activelog", "inactivelog", "install", "tftp",
                                                          "partBtr", "salog"], width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(lf, text="File spec").grid(row=1, column=2, sticky="e", padx=(10, 4))
        ttk.Entry(lf, textvariable=self.v_spec, width=40).grid(row=1, column=3, sticky="w")
        ttk.Label(lf, text="Options").grid(row=2, column=0, sticky="w")
        ttk.Entry(lf, textvariable=self.v_opt, width=40).grid(row=2, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Label(lf, style="Muted.TLabel", text="e.g. reltime hours 2  |  abstime 09:00:10/27/26 11:00:10/27/26  |  "
                  "match <regex>  |  recurs").grid(row=3, column=1, columnspan=3, sticky="w")
        self._preset()

        self.ex_picker = NodePicker(f, self, height=4, default_all=False)
        self.ex_picker.pack(fill="x", pady=6)
        eb = ttk.Frame(f)
        eb.pack(fill="x")
        ttk.Button(eb, text="Export files from checked nodes", style="Accent.TButton",
                   command=lambda: self.task("File export", self._file_export)).pack(side="left")
        ttk.Button(eb, text="Open export folder", command=lambda: os.startfile(self.root("export"))).pack(side="left", padx=6)

        af = ttk.LabelFrame(f, text="Configuration inventory to Excel (AXL SQL, publisher)", padding=10)
        af.pack(fill="both", expand=True, pady=(10, 0))
        self.sql_list = tk.Listbox(af, selectmode="multiple", height=7, exportselection=False,
                                   font=("Segoe UI", 10), relief="solid", bd=1)
        for name in ops.SQL_PRESETS:
            self.sql_list.insert("end", name)
        self.sql_list.select_set(0, "end")
        self.sql_list.pack(side="left", fill="y")
        rf = ttk.Frame(af)
        rf.pack(side="left", fill="both", expand=True, padx=(10, 0))
        ttk.Label(rf, text="Custom SQL (optional, added as sheet 'Custom'):").pack(anchor="w")
        self.sql_custom = tk.Text(rf, height=5, font=("Consolas", 9), relief="solid", bd=1)
        self.sql_custom.pack(fill="both", expand=True)
        ttk.Button(rf, text="Export inventory to Excel…", style="Accent.TButton",
                   command=self._sql_export).pack(anchor="w", pady=(6, 0))

    def _preset(self):
        loc, spec, opt = ops.FILE_PRESETS[self.v_preset.get()]
        self.v_loc.set(loc)
        self.v_spec.set(spec)
        self.v_opt.set(opt)

    def _file_export(self):
        nodes = self.ex_picker.selected()
        if not nodes:
            raise CLIError("Check the node(s) to export from.")
        loc, spec, opt = self.v_loc.get().strip(), self.v_spec.get().strip(), self.v_opt.get().strip()
        if not loc or not spec:
            raise CLIError("Enter a location and a file spec.")
        ip, port, user, pw = self.sftp_params()
        label = re.sub(r"[^A-Za-z0-9]+", "_", self.v_preset.get()).strip("_")[:30]
        ts = stamp()

        def one(n):
            name = n.get("hostname") or n["host"]
            rel = f"export/{name}/{label}-{ts}"
            local = self.root(*rel.split("/"))
            self.ex_picker.set_status(n["host"], "exporting…", "run")
            with self.shell(n["host"]) as sh:
                ops.file_get(sh, loc, spec, opt, ip, user, pw, "/" + rel, sftp_port=port,
                             on_output=self.cli_out(n["host"]), cancel=self.cancel)
            count = sum(len(fs) for _, _, fs in os.walk(local))
            self.ex_picker.set_status(n["host"], f"✓ {count} file(s) → {local}", "ok")
            self.log(f"[{n['host']}] exported {count} file(s) to {local}")

        res = run_on_nodes(nodes, one, parallel=True, log=self.log)
        for host, (ok, err) in res.items():
            if not ok:
                self.ex_picker.set_status(host, f"✗ {str(err).splitlines()[-1][:140]}", "bad")
        if any(ok for ok, _ in res.values()):
            os.startfile(self.root("export"))

    def _sql_export(self):
        items = [(self.sql_list.get(i), ops.SQL_PRESETS[self.sql_list.get(i)]) for i in self.sql_list.curselection()]
        custom = self.sql_custom.get("1.0", "end").strip()
        if custom:
            items.append(("Custom", custom))
        if not items:
            messagebox.showwarning(APP, "Select at least one inventory item.")
            return
        if not self.v_axluser.get().strip() or not self.v_axlpw.get():
            messagebox.showwarning(APP, "Enter the AXL application user and password on the Cluster tab.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialdir=self.root("export"),
                                            initialfile=f"CUCM-inventory-{stamp()}.xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return

        def run():
            axl = ops.AXLSQL(self.publisher(), self.v_axluser.get().strip(), self.v_axlpw.get())
            ops.export_sql_to_excel(axl, items, path, log=self.log, cancel=self.cancel)
            self.log(f"Inventory saved: {path} (AXL {axl.version})")
            os.startfile(path)
        self.task("Inventory export", run)

    # ======================================================================
    # CLI console tab (interactive)
    # ======================================================================
    def _tab_console(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Node").pack(side="left")
        self.v_cnode = tk.StringVar()
        self.cnode_cb = ttk.Combobox(top, textvariable=self.v_cnode, width=36)
        self.cnode_cb.pack(side="left", padx=6)
        self.c_conn_btn = ttk.Button(top, text="Connect", style="Accent.TButton", command=self._console_connect)
        self.c_conn_btn.pack(side="left")
        ttk.Button(top, text="Disconnect", command=self._console_disconnect).pack(side="left", padx=6)
        self.c_state = ttk.Label(top, text="not connected", style="Muted.TLabel")
        self.c_state.pack(side="left", padx=10)

        quick = ttk.Frame(f)
        quick.pack(fill="x", pady=6)
        for cmd in ("show version active", "show network cluster", "show status", "utils service list",
                    "utils dbreplication runtimestate", "utils ntp status", "utils system upgrade status",
                    "utils disaster_recovery status backup"):
            ttk.Button(quick, text=cmd, command=lambda c=cmd: self._console_send(c)).pack(side="left", padx=2)

        self.term = scrolledtext.ScrolledText(f, bg="#0B1220", fg="#E5E7EB", insertbackground="white",
                                              font=("Consolas", 10), state="disabled", relief="flat")
        self.term.pack(fill="both", expand=True)
        bot = ttk.Frame(f)
        bot.pack(fill="x", pady=(6, 0))
        self.v_cmd = tk.StringVar()
        e = ttk.Entry(bot, textvariable=self.v_cmd, font=("Consolas", 10))
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self._console_send(self.v_cmd.get()))
        ttk.Button(bot, text="Send", command=lambda: self._console_send(self.v_cmd.get())).pack(side="left", padx=4)
        ttk.Button(bot, text="Send password (masked)", command=self._console_pw).pack(side="left")
        ttk.Button(bot, text="Ctrl-C", command=lambda: self._console_raw("\x03")).pack(side="left", padx=4)
        ttk.Label(f, style="Muted.TLabel", text="Live CLI session - answer any interactive question here "
                  "(type the answer and press Enter).").pack(anchor="w")

    def _console_nodes(self):
        vals = [f"{n['host']}  ({n.get('hostname', '')} {n.get('role', '')})" for n in self.nodes]
        self.cnode_cb.configure(values=vals)
        if vals and not self.v_cnode.get():
            self.v_cnode.set(vals[0])

    def _console_connect(self):
        host = self.v_cnode.get().split()[0] if self.v_cnode.get().strip() else ""
        if not host:
            messagebox.showwarning(APP, "Pick or type a node address.")
            return
        self._console_disconnect()
        self.c_state.configure(text=f"connecting to {host}…", style="Muted.TLabel")

        def run():
            try:
                sh = self.shell(host)
                sh.connect()
            except Exception as e:
                self.ui(self.c_state.configure, {"text": f"failed: {e}", "style": "Bad.TLabel"})
                return
            self.console_shell = sh
            self.ui(self.c_state.configure, {"text": f"connected to {host}", "style": "Ok.TLabel"})
            self.ui(self._append, self.term, f"--- connected to {host} ---\nadmin:")
            while self.console_shell is sh and sh.alive:
                try:
                    if sh.chan.recv_ready():
                        data = sh._recv()
                        if data:
                            self.ui(self._append, self.term, data)
                        continue
                except Exception:
                    break
                threading.Event().wait(0.1)
            self.ui(self._append, self.term, "\n--- session closed ---\n")
            self.ui(self.c_state.configure, {"text": "not connected", "style": "Muted.TLabel"})
        threading.Thread(target=run, daemon=True).start()

    def _console_disconnect(self):
        sh, self.console_shell = self.console_shell, None
        if sh:
            sh.close()

    def _console_raw(self, text):
        if not (self.console_shell and self.console_shell.alive):
            messagebox.showinfo(APP, "Connect the console first.")
            return
        self.console_shell.send_raw(text)

    def _console_send(self, cmd):
        self.v_cmd.set("")
        self._console_raw(cmd + "\n")

    def _console_pw(self):
        from tkinter import simpledialog
        pw = simpledialog.askstring(APP, "Password to send:", show="•", parent=self)
        if pw is not None:
            self._console_raw(pw + "\n")

    # ----------------------------------------------------------------------
    def _on_close(self):
        if self.busy and not messagebox.askyesno(APP, "An operation is running. Close anyway?"):
            return
        if self.server and self.server.running and self.server._transports and \
                not messagebox.askyesno(APP, "CUCM is transferring files over SFTP. Close anyway?"):
            return
        self._save_cfg()
        self._console_disconnect()
        if self.server:
            self.server.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
