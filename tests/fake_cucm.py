"""Fake CUCM platform CLI over SSH, for testing without a lab.

Mimics the prompts of DRS, 'utils system upgrade initiate' and 'file get', and really
transfers files with the SFTP server given in the answers.
"""

import io
import os
import socket
import threading
import time

import paramiko

SFTP_PORT = 22
HOST_KEY = paramiko.RSAKey.generate(2048)


class _Auth(paramiko.ServerInterface):
    def check_auth_password(self, u, p):
        return paramiko.AUTH_SUCCESSFUL if (u, p) == ("admin", "adminpw") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, u):
        return "password"

    def check_channel_request(self, kind, cid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *a):
        return True

    def check_channel_shell_request(self, ch):
        return True


class CLI:
    def __init__(self, chan, ip):
        self.ch, self.ip = chan, ip
        self.drs = {}

    def w(self, s):
        self.ch.send(s.replace("\n", "\r\n"))

    def line(self):
        buf = b""
        while True:
            c = self.ch.recv(1)
            if not c:
                raise EOFError
            if c in (b"\n", b"\r"):
                if c == b"\r":
                    continue
                self.w("\n")
                return buf.decode()
            buf += c
            self.ch.send(c)            # echo

    def ask(self, prompt):
        self.w(prompt)
        return self.line().strip()

    def sftp(self, ip, user, pw, port=None):
        port = port or SFTP_PORT
        t = paramiko.Transport((ip, int(port)))
        t.connect(username=user, password=pw)
        return t, paramiko.SFTPClient.from_transport(t)

    def run(self):
        self.w("Command Line Interface is starting up, please wait ...\n\n   Welcome to the Platform "
               "Command Line Interface\n\n")
        while True:
            self.w("admin:")
            cmd = self.line().strip()
            if cmd == "exit":
                break
            try:
                self.dispatch(cmd)
            except EOFError:
                raise
            except Exception as e:
                self.w(f"Executed command unsuccessfully\n{e}\n")

    def dispatch(self, cmd):
        if cmd == "set cli pagination off":
            self.w("Automatic Pagination : Off.\n")
        elif cmd == "show network cluster":
            self.w("10.0.0.1 cucmpub.lab.local cucmpub Publisher callmanager DBPub authenticated\n"
                   "10.0.0.2 cucmsub1.lab.local cucmsub1 Subscriber callmanager DBSub authenticated\n"
                   "10.0.0.3 imp1.lab.local imp1 Publisher cups DBPub authenticated\n\n"
                   "Server Table (processnode) Entries\n----\n")
        elif cmd == "show version active":
            self.w("Active Master Version: 14.0.1.13900-155\nActive Version Installed Software Options:\n"
                   "ciscocm.ucm_ldap.cop.sgn\n")
        elif cmd == "show version inactive":
            self.w("Inactive Master Version: 12.5.1.16900-48\nInactive Version Installed Software Options:\n"
                   "No Installed Software Options Found.\n")
        elif cmd.startswith("utils disaster_recovery device add network"):
            _, _, _, _, _, name, path, ip, user, keep = cmd.split()
            pw = self.ask(f"Please enter password to connect to network server {ip}:")
            t, s = self.sftp(ip, user, pw)
            s.listdir(path)
            t.close()
            self.drs[name] = (path, ip, user, pw)
            self.w(f"Successfully added network device {name}\n")
        elif cmd.startswith("utils disaster_recovery backup network"):
            feats, name = cmd.split()[4:6]
            path, ip, user, pw = self.drs[name]
            self.w("Backup initiated successfully. Please run 'utils disaster_recovery status backup' "
                   "command to see the status\n")
            t, s = self.sftp(ip, user, pw)
            tar = time.strftime("%Y-%m-%d-%H-%M-%S") + ".tar"
            s.putfo(io.BytesIO(os.urandom(300_000)), f"{path}/{tar}")
            s.putfo(io.BytesIO(b"<xml/>"), f"{path}/{tar[:-4]}_drfComponent.xml")
            t.close()
            self.last_tar = tar
        elif cmd == "utils disaster_recovery status backup":
            self.w(f"Status: SUCCESS :Backup Completed...\nTar Filename: {self.last_tar}\n"
                   "Storage Location: NETWORK\nOperation: backup\nPercentage Complete: 100\n"
                   "UCM  CUCMPUB  CCMDB  SUCCESS  Mon Oct 27 10:00:00 GST 2026  activelog/x.log\n")
        elif cmd == "utils system upgrade initiate":
            self.w("\nWarning: Do not close this window without first canceling the upgrade.\n\nSource:\n\n"
                   "1) Remote Filesystem via SFTP\n2) Remote Filesystem via FTP\n3) Local DVD/CD\nq) quit\n\n")
            if self.ask('Please select an option (1 - 3 or "q" ):') != "1":
                return
            d = self.ask("Directory: ")
            ip = self.ask("Server: ")
            user = self.ask("User Name: ")
            pw = self.ask("Password: ")
            self.ask("SMTP Server (optional): ")
            self.w("\nChecking for valid upgrades. Please wait...\n\n")
            t, s = self.sftp(ip, user, pw)
            files = [f for f in s.listdir(d) if ".cop" in f or f.endswith(".iso")]
            self.w(f'Available options and upgrades in "{ip}:{d}":\n\n')
            for i, f in enumerate(files, 1):
                self.w(f"{i}) {f}\n")
            self.w("q) quit\n\n")
            sel = self.ask(f'Please select an option (1 - {len(files)} or "q" ):')
            if sel == "q":
                t.close()
                return
            name = files[int(sel) - 1]
            self.w("Accessing the file. Please wait...\n")
            data = io.BytesIO()
            s.getfo(f"{d}/{name}", data)
            t.close()
            self.w(f"Downloaded {len(data.getvalue())} bytes.\n\nChecksum value of {name}: abc\n\n")
            if name.endswith(".iso"):
                self.ask("Automatically switch versions if the upgrade is successful (yes/no): ")
            if self.ask("Start installation (yes/no): ") == "yes":
                self.w("The upgrade log is install_log_2026.log\nUpgrading the system.  Please wait...\n")
                time.sleep(1)
                self.w(f"Successfully installed {name}\n")
        elif cmd.startswith("file get"):
            self.w("Please wait while the system is gathering files info ...done.\n"
                   "Number of files affected: 3\nTotal size in Bytes: 3000\n")
            if self.ask("Would you like to proceed [y/n]? ") != "y":
                return
            ip = self.ask("SFTP server IP: ")
            port = self.ask("SFTP server port [22]: ") or "22"
            user = self.ask("User ID: ")
            pw = self.ask("Password: ")
            d = self.ask("Download directory: ")
            t, s = self.sftp(ip, user, pw, port)
            for i in range(3):
                s.putfo(io.BytesIO(b"x" * 1000), f"{d}/file{i}.txt")
            t.close()
            self.w("...\nTransfer completed.\n")
        elif cmd == "utils system upgrade status":
            self.w("Upgrade status: Idle\n")
        else:
            self.w(f"Executed command unsuccessfully\nNo valid command entered: {cmd}\n")


def serve(port, ready):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(5)
    ready.set()
    while True:
        c, addr = sock.accept()

        def handle(c=c):
            t = paramiko.Transport(c)
            t.add_server_key(HOST_KEY)
            t.start_server(server=_Auth())
            ch = t.accept(30)
            try:
                CLI(ch, "10.0.0.1").run()
            except (EOFError, OSError):
                pass
            finally:
                t.close()
        threading.Thread(target=handle, daemon=True).start()


def start(port=2222):
    ready = threading.Event()
    threading.Thread(target=serve, args=(port, ready), daemon=True).start()
    ready.wait()
