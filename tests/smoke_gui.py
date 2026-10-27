import os, sys, ctypes, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import maint_app
from PIL import Image
app = maint_app.App()
app.nodes = [{"host": "10.0.0.1", "hostname": "cucmpub", "role": "Publisher", "type": "CUCM", "active": "14.0.1.13900-155", "inactive": "12.5.1.16900-48", "cops": ["ciscocm.ucm_ldap.cop.sgn"]},
             {"host": "10.0.0.2", "hostname": "cucmsub1", "role": "Subscriber", "type": "CUCM"}]
app._nodes_changed()
out = sys.argv[1]
def grab(i):
    app.update(); hwnd = ctypes.windll.user32.GetParent(app.winfo_id()) or app.winfo_id()
    w, h = app.winfo_width(), app.winfo_height()
    import ctypes.wintypes as wt
    r = wt.RECT(); ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)); w, h = r.right-r.left, r.bottom-r.top
    hdc = ctypes.windll.user32.GetWindowDC(hwnd); mdc = ctypes.windll.gdi32.CreateCompatibleDC(hdc)
    bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc, w, h); ctypes.windll.gdi32.SelectObject(mdc, bmp)
    ctypes.windll.user32.PrintWindow(hwnd, mdc, 2)
    buf = ctypes.create_string_buffer(w*h*4)
    class BIH(ctypes.Structure): _fields_=[("a",ctypes.c_uint32),("w",ctypes.c_int32),("h",ctypes.c_int32),("p",ctypes.c_uint16),("b",ctypes.c_uint16),("c",ctypes.c_uint32),("s",ctypes.c_uint32),("x",ctypes.c_int32),("y",ctypes.c_int32),("u",ctypes.c_uint32),("i",ctypes.c_uint32)]
    bi = BIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    ctypes.windll.gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(os.path.join(out, f"tab{i}.png"))
for i in range(6):
    app.nb.select(i); app.update(); time.sleep(0.3); grab(i)
app.destroy(); print("GUI OK")
