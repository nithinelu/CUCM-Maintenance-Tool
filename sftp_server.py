"""Embedded SFTP server (paramiko) for CUCM DRS backups, upgrades and file exports.

CUCM pushes backups / `file get` exports to it and pulls upgrade ISOs / COP files
from it. Everything is confined to one root folder; "/" on the wire is that folder.

    srv = SFTPServer(root=r"D:\\sftp", username="cucm", password="secret", port=22,
                     log=print)
    srv.start()
    ...
    srv.stop()

Legacy SSH algorithms (ssh-rsa host key, diffie-hellman-group1/14-sha1, CBC ciphers)
stay enabled because older CUCM releases (10.x/11.x DRS uses JSch) need them.
"""

import os
import posixpath
import socket
import threading
import time

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServerInterface, SFTP_OK
from paramiko.sftp_server import SFTPServer as _ParamikoSFTPServer


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def load_host_key(path):
    """RSA host key, created on first run (RSA is the one key type every CUCM accepts)."""
    if os.path.exists(path):
        return paramiko.RSAKey(filename=path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(path)
    return key


# --------------------------------------------------------------------------
# SSH auth / channels
# --------------------------------------------------------------------------
class _Auth(paramiko.ServerInterface):
    def __init__(self, owner, peer):
        self.owner = owner
        self.peer = peer

    def check_auth_password(self, username, password):
        if username == self.owner.username and password == self.owner.password:
            self.owner._log(f"{self.peer}: login OK as '{username}'")
            return paramiko.AUTH_SUCCESSFUL
        self.owner._log(f"{self.peer}: login FAILED for '{username}'")
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


# --------------------------------------------------------------------------
# SFTP file system, chrooted to the root folder
# --------------------------------------------------------------------------
class _Handle(SFTPHandle):
    def __init__(self, flags, owner, peer, rel):
        super().__init__(flags)
        self.owner, self.peer, self.rel = owner, peer, rel
        self.read_bytes = 0
        self.written = 0
        self.started = time.time()

    def read(self, offset, length):
        data = super().read(offset, length)
        if isinstance(data, bytes):
            self.read_bytes += len(data)
            self.owner._progress(self.peer, self.rel, "download", self.read_bytes)
        return data

    def write(self, offset, data):
        rc = super().write(offset, data)
        self.written += len(data)
        self.owner._progress(self.peer, self.rel, "upload", self.written)
        return rc

    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return SFTP_OK

    def close(self):
        super().close()
        secs = max(time.time() - self.started, 0.001)
        if self.written:
            self.owner._log(f"{self.peer}: received {self.rel} ({_fmt_size(self.written)}, "
                            f"{_fmt_size(self.written / secs)}/s)")
        elif self.read_bytes:
            self.owner._log(f"{self.peer}: sent {self.rel} ({_fmt_size(self.read_bytes)}, "
                            f"{_fmt_size(self.read_bytes / secs)}/s)")
        self.owner._progress(self.peer, self.rel, "done", self.written or self.read_bytes)


class _FS(SFTPServerInterface):
    def __init__(self, server, *args, owner=None, peer="", **kwargs):
        super().__init__(server, *args, **kwargs)
        self.owner = owner
        self.peer = peer
        self.root = os.path.abspath(owner.root)

    # "/a/b" -> <root>\a\b ; never escapes root
    def _local(self, path):
        clean = posixpath.normpath("/" + (path or "/").replace("\\", "/"))
        parts = [p for p in clean.split("/") if p not in ("", ".", "..")]
        return os.path.join(self.root, *parts)

    def canonicalize(self, path):
        return posixpath.normpath("/" + (path or "/").replace("\\", "/")).replace("//", "/")

    def list_folder(self, path):
        local = self._local(path)
        try:
            out = []
            for name in os.listdir(local):
                attr = SFTPAttributes.from_stat(os.stat(os.path.join(local, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._local(path)))
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        local = self._local(path)
        try:
            flags |= getattr(os, "O_BINARY", 0)
            fd = os.open(local, flags, 0o666)
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        try:
            f = os.fdopen(fd, mode)
        except OSError as e:
            os.close(fd)
            return _ParamikoSFTPServer.convert_errno(e.errno)
        h = _Handle(flags, self.owner, self.peer, self.canonicalize(path))
        h.filename = local
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._local(path))
            self.owner._log(f"{self.peer}: deleted {self.canonicalize(path)}")
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.replace(self._local(oldpath), self._local(newpath))
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)
        return SFTP_OK

    posix_rename = rename

    def mkdir(self, path, attr):
        try:
            os.makedirs(self._local(path), exist_ok=False)
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._local(path))
        except OSError as e:
            return _ParamikoSFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def chattr(self, path, attr):
        # Permissions / owners are meaningless on the Windows side; accept and ignore,
        # but apply timestamps so the file list shows CUCM's dates.
        if attr._flags & attr.FLAG_AMTIME:
            try:
                os.utime(self._local(path), (attr.st_atime, attr.st_mtime))
            except OSError:
                pass
        return SFTP_OK

    def readlink(self, path):
        return paramiko.SFTP_OP_UNSUPPORTED

    def symlink(self, target_path, path):
        return paramiko.SFTP_OP_UNSUPPORTED


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class SFTPServer:
    def __init__(self, root, username, password, port=22, bind="0.0.0.0",
                 host_key_path=None, allowed_ips=None, log=None, progress=None):
        self.root = os.path.abspath(root)
        self.username = username
        self.password = password
        self.port = int(port)
        self.bind = bind
        self.host_key_path = host_key_path or os.path.join(self.root, ".host_rsa.key")
        self.allowed_ips = set(allowed_ips or [])      # empty = anyone with the password
        self.log = log or (lambda msg: None)
        self.progress = progress                        # progress(peer, path, kind, bytes)
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._transports = []
        self._last_progress = {}
        self.seen = {}                                  # peer IP -> time of last TCP connect

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _log(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def _progress(self, peer, path, kind, nbytes):
        if not self.progress:
            return
        now = time.time()
        key = (peer, path)
        if kind != "done" and now - self._last_progress.get(key, 0) < 0.5:
            return
        self._last_progress[key] = now
        try:
            self.progress(peer, path, kind, nbytes)
        except Exception:
            pass

    def start(self):
        if self.running:
            return
        os.makedirs(self.root, exist_ok=True)
        self.host_key = load_host_key(self.host_key_path)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind, self.port))
        except OSError as e:
            sock.close()
            raise OSError(f"Cannot listen on {self.bind}:{self.port} - {e.strerror or e}. "
                          "Another SFTP/SSH server may already be using this port.") from e
        sock.listen(20)
        sock.settimeout(1.0)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="sftp-accept")
        self._thread.start()
        fp = self.host_key.get_fingerprint().hex(":")
        self._log(f"SFTP server listening on {self.bind}:{self.port}, root {self.root} "
                  f"(host key MD5 {fp})")

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        for t in list(self._transports):
            try:
                t.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._sock = None
        self._log("SFTP server stopped")

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            peer = addr[0]
            self.seen[peer] = time.time()
            if self.allowed_ips and peer not in self.allowed_ips:
                self._log(f"{peer}: connection refused (not in allowed IP list)")
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client, peer), daemon=True,
                             name=f"sftp-{peer}").start()

    def _serve(self, client, peer):
        self._log(f"{peer}: connected")
        t = None
        try:
            client.settimeout(None)
            t = paramiko.Transport(client)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", _ParamikoSFTPServer, _FS, owner=self, peer=peer)
            self._transports.append(t)
            t.start_server(server=_Auth(self, peer))
            while t.is_active() and not self._stop.is_set():
                time.sleep(0.5)
        except Exception as e:
            self._log(f"{peer}: session error - {e}")
        finally:
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
                if t in self._transports:
                    self._transports.remove(t)
            self._log(f"{peer}: disconnected")


def source_ip_for(host):
    """The local IP Windows uses to reach `host` - the address CUCM should connect back to.
    (UDP connect sends no packets; it only asks the routing table.)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((socket.gethostbyname(host.strip()), 22))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def local_ipv4_addresses():
    """Best-effort list of this PC's IPv4 addresses (for the 'SFTP server IP' field)."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    # The address used for the default route goes first.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip in ips:
            ips.remove(ip)
        if not ip.startswith("127."):
            ips.insert(0, ip)
    except OSError:
        pass
    return ips
