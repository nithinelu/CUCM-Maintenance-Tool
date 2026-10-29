"""Drive the GUI's Dry run against fake_cucm (no window interaction needed)."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_cucm, maint_app
from cucm_cli import CUCMShell

fake_cucm.SFTP_PORT = 2023
fake_cucm.start(2224)
maint_app.os.startfile = lambda p: print("would open:", p)
_tmp = tempfile.mkdtemp()                  # never touch the real config / logs / reports
maint_app.CONFIG = os.path.join(_tmp, "config.json")
maint_app.LOG_DIR = os.path.join(_tmp, "logs")
maint_app.REPORT_DIR = os.path.join(_tmp, "reports")
app = maint_app.App()
root = tempfile.mkdtemp()
os.makedirs(os.path.join(root, "upgrade"))
open(os.path.join(root, "upgrade", "ciscocm.demo.cop.sha512"), "wb").write(b"x" * 1000)
app.v_pub.set("127.0.0.1"); app.v_root.set(root); app.v_port.set("2023"); app.v_ip.set("127.0.0.1")
app.v_osuser.set("admin"); app.v_ospw.set("adminpw")
app.shell = lambda h: CUCMShell(h, "admin", "adminpw", port=2224, log=app.log)
app.nodes = [{"host": "127.0.0.1", "hostname": "fakepub", "role": "Publisher", "type": "CUCM"}]
app._nodes_changed()
app.start_sftp()
done = []
import threading
def _go():
    try:
        app._dry_run()
    except Exception as e:
        import traceback; traceback.print_exc()
    done.append(1)
app.after(200, lambda: threading.Thread(target=_go, daemon=True).start())
def _check():
    if done:
        app.after(1000, app.quit)
    else:
        app.after(200, _check)
app.after(300, _check)
app.mainloop()
rep = sorted(f for f in os.listdir(maint_app.REPORT_DIR) if f.startswith("dryrun"))[-1]
text = open(os.path.join(maint_app.REPORT_DIR, rep), encoding="utf-8").read()
print(text[:2500])
assert "[OK ] Node -> SFTP: upgrade wizard file list" in text and "ciscocm.demo.cop.sha512" in text
assert app.v_spw.get() not in text and "adminpw" not in text, "password leaked into report"
assert "[OK ] Node -> SFTP: file get" in text and "Start installation" not in text
os.remove(os.path.join(maint_app.REPORT_DIR, rep))
app.stop_sftp(); app.destroy(); print("DRYRUN OK")
