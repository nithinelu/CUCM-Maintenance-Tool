"""End-to-end: built-in SFTP server + CLI automation against fake_cucm."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_cucm
import cucm_ops as ops
from cucm_cli import CUCMShell
from sftp_server import SFTPServer

root = tempfile.mkdtemp()
for d in ("backup", "upgrade", "export"):
    os.makedirs(os.path.join(root, d))
with open(os.path.join(root, "upgrade", "ciscocm.test-patch.k4.cop.sha512"), "wb") as f:
    f.write(os.urandom(2_000_000))
srv = SFTPServer(root, "cucmsftp", "secret123", port=2022, bind="127.0.0.1",
                 host_key_path=os.path.join(root, "hk.key"), log=lambda m: print("  SFTP|", m))
srv.start()
fake_cucm.SFTP_PORT = 2022
fake_cucm.start(2222)

sh = CUCMShell("127.0.0.1", "admin", "adminpw", port=2222, log=lambda m: print("  CLI |", m)).connect()
nodes, _ = ops.discover_nodes(sh)
print("nodes:", nodes)
assert len(nodes) == 3 and nodes[0]["role"] == "Publisher" and nodes[2]["type"] == "IM&P"
v = ops.versions(sh)
print("versions:", v["active"], v["inactive"], v["cops"])
assert v["active"] == "14.0.1.13900-155" and v["cops"] == ["ciscocm.ucm_ldap.cop.sgn"]

print(ops.drs_add_device(sh, "BK", "/backup", "127.0.0.1", "cucmsftp", "secret123", 3))
print(ops.drs_start_backup(sh, "UCM,CDR_CAR", "BK"))
st = ops.drs_wait(sh, poll=1)
print("backup:", st["status"], st["tar"], st["components"])
assert ops.drs_finished(st) and os.path.exists(os.path.join(root, "backup", st["tar"]))

ok, out = ops.upgrade_initiate(sh, "127.0.0.1", "cucmsftp", "secret123", "/upgrade",
                               "ciscocm.test-patch.k4.cop.sha512", sftp_port=2022)
print("upgrade ok:", ok, "|", out.splitlines()[-1])
assert ok

try:
    ops.upgrade_initiate(sh, "127.0.0.1", "cucmsftp", "secret123", "/upgrade", "missing.cop.sgn")
    raise AssertionError("missing file should fail")
except Exception as e:
    print("missing file ->", e)

os.makedirs(os.path.join(root, "export", "n1"))
out = ops.file_get(sh, "activelog", "syslog/messages*", "", "127.0.0.1", "cucmsftp", "secret123",
                   "/export/n1", sftp_port=2022)
got = os.listdir(os.path.join(root, "export", "n1"))
print("exported:", got)
assert len(got) == 3
print("still alive:", sh.run("utils system upgrade status"))
sh.close()

# wrong password must be refused; path traversal must stay in root
import paramiko
t = paramiko.Transport(("127.0.0.1", 2022))
try:
    t.connect(username="cucmsftp", password="bad")
    raise AssertionError("bad password accepted")
except paramiko.AuthenticationException:
    print("bad password refused")
t.close()
t = paramiko.Transport(("127.0.0.1", 2022)); t.connect(username="cucmsftp", password="secret123")
s = paramiko.SFTPClient.from_transport(t)
print("ls /../..:", s.listdir("/../.."))
assert set(s.listdir("/../..")) >= {"backup", "upgrade", "export"}
s.mkdir("/tmpdir"); s.rmdir("/tmpdir"); print("normalize:", s.normalize("."))
t.close()
# legacy client algorithms (old CUCM / JSch)
t = paramiko.Transport(("127.0.0.1", 2022))
t.get_security_options().kex = ["diffie-hellman-group14-sha1"]
t.get_security_options().key_types = ["ssh-rsa"]
t.connect(username="cucmsftp", password="secret123"); print("legacy kex ok:", t.kex_engine.__class__.__name__); t.close()
srv.stop()
print("ALL OK")
