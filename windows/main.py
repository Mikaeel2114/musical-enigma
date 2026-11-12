"""ScreenBridge - share your Windows screen to your phone over Wi-Fi/LAN."""
import ctypes, io, json, os, random, socket, sys, threading, time
import tkinter as tk
from tkinter import messagebox
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import mss
from PIL import Image, ImageTk
import urllib.request, urllib.error

APP, PORT = "ScreenBridge", 8765
CFG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP, "config.json")
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
user32 = ctypes.windll.user32


def res(name):
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def load_cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(c):
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w") as f:
        json.dump(c, f)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------- remote input (Win32) ----------
ULP = ctypes.POINTER(ctypes.c_ulong)
class KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULP)]
class MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULP)]
class _U(ctypes.Union):
    _fields_ = [("ki", KI), ("mi", MI)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

VK = {"enter": 0x0D, "esc": 0x1B, "win": 0x5B, "tab": 0x09, "backspace": 0x08,
      "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


def press(vk):
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def type_text(s):
    for ch in s:
        if ch == "\n":
            press(0x0D)
            continue
        if ord(ch) > 0xFFFF:
            continue
        for flag in (0x0004, 0x0006):
            i = INPUT(1, _U(ki=KI(0, ord(ch), flag, 0, None)))
            user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def handle_input(d):
    t = d.get("t")
    if "x" in d:
        user32.SetCursorPos(int(d["x"] * user32.GetSystemMetrics(0)), int(d["y"] * user32.GetSystemMetrics(1)))
    if t == "click":
        user32.mouse_event(2, 0, 0, 0, 0); user32.mouse_event(4, 0, 0, 0, 0)
    elif t == "dbl":
        for _ in range(2):
            user32.mouse_event(2, 0, 0, 0, 0); user32.mouse_event(4, 0, 0, 0, 0)
    elif t == "rclick":
        user32.mouse_event(8, 0, 0, 0, 0); user32.mouse_event(16, 0, 0, 0, 0)
    elif t == "scroll":
        user32.mouse_event(0x0800, 0, 0, int(d.get("dy", 0)) * 120, 0)
    elif t == "text":
        type_text(str(d.get("s", "")))
    elif t == "key" and d.get("k") in VK:
        press(VK[d["k"]])


# ---------- server ----------
class State:
    pin = "0000"
    allow_control = True
    running = False
    approved = set()
    lock = threading.Lock()
    ask = None  # set by GUI

S = State()


def frames(q, fps, width):
    with mss.mss() as sct:
        mon = sct.monitors[1]
        while S.running:
            t0 = time.time()
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if width and img.width > width:
                img = img.resize((width, int(img.height * width / img.width)), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            yield buf.getvalue()
            time.sleep(max(0, 1 / fps - (time.time() - t0)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def auth(self, qs):
        if qs.get("pin", [""])[0] != S.pin:
            self.reply(401, b"bad pin")
            return False
        ip = self.client_address[0]
        with S.lock:
            if ip not in S.approved:
                if not S.ask(ip):
                    self.reply(403, b"denied")
                    return False
                S.approved.add(ip)
        return True

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        if u.path not in ("/ping", "/stream") or not self.auth(qs):
            if u.path not in ("/ping", "/stream"):
                self.reply(404)
            return
        if u.path == "/ping":
            info = {"name": socket.gethostname(), "w": user32.GetSystemMetrics(0),
                    "h": user32.GetSystemMetrics(1), "control": S.allow_control}
            return self.reply(200, json.dumps(info).encode(), "application/json")
        q = max(10, min(95, int(qs.get("q", ["60"])[0])))
        fps = max(1, min(60, int(qs.get("fps", ["24"])[0])))
        w = int(qs.get("w", ["1280"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for jpg in frames(q, fps, w):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                self.wfile.write(jpg + b"\r\n")
        except (ConnectionError, OSError):
            pass

    def do_POST(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b"{}"
        if u.path != "/input" or not self.auth(qs):
            if u.path != "/input":
                self.reply(404)
            return
        if not S.allow_control:
            return self.reply(403, b"control disabled")
        try:
            handle_input(json.loads(body))
        except Exception:
            return self.reply(400)
        self.reply(200, b"ok")


# ---------- phone viewer (phone -> Windows) ----------
class PhoneViewer:
    PPORT = 8766

    def __init__(self, root, cfg):
        self.cfg, self.gen, self.latest, self.pending, self.tkimg = cfg, 0, None, False, None
        w = self.win = tk.Toplevel(root)
        w.title("Phone screen - ScreenBridge"); w.configure(bg=BG); w.geometry("460x840")
        bar = tk.Frame(w, bg=CARD, padx=8, pady=8); bar.pack(fill="x")
        self.ip = tk.StringVar(value=cfg.get("phone_ip", "")); self.pin = tk.StringVar(value=cfg.get("phone_pin", ""))
        tk.Label(bar, text="Phone IP", fg=MUTED, bg=CARD).grid(row=0, column=0)
        tk.Entry(bar, textvariable=self.ip, width=15).grid(row=0, column=1, padx=4)
        tk.Label(bar, text="PIN", fg=MUTED, bg=CARD).grid(row=0, column=2)
        tk.Entry(bar, textvariable=self.pin, width=6).grid(row=0, column=3, padx=4)
        for i, (t, c) in enumerate((("Connect", self.connect), ("Screenshot", self.shot))):
            tk.Button(bar, text=t, command=c, bg=ACC, fg="white", relief="flat", bd=0, padx=8, cursor="hand2").grid(row=1, column=i * 2, columnspan=2, pady=(8, 0), sticky="ew", padx=2)
        self.status = tk.Label(w, text="On the phone: tap 'Share this phone to Windows'", fg=MUTED, bg=BG)
        self.status.pack(pady=4)
        self.canvas = tk.Canvas(w, bg="black", highlightthickness=0); self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.show())
        w.protocol("WM_DELETE_WINDOW", self.close)

    def say(self, text, color=MUTED):
        try:
            self.win.after(0, lambda: self.status.config(text=text, fg=color))
        except Exception:
            pass

    def connect(self):
        self.gen += 1
        self.cfg["phone_ip"], self.cfg["phone_pin"] = self.ip.get().strip(), self.pin.get().strip()
        save_cfg(self.cfg)
        self.say("Connecting...")
        threading.Thread(target=self.read, args=(self.gen, self.cfg["phone_ip"], self.cfg["phone_pin"]), daemon=True).start()

    def read(self, gen, ip, pin):
        try:
            r = urllib.request.urlopen(f"http://{ip}:{self.PPORT}/stream?pin={pin}", timeout=10)
        except urllib.error.HTTPError as e:
            return self.say("Wrong PIN" if e.code == 401 else f"Phone error {e.code}", "#f87171")
        except Exception:
            return self.say("Can't reach the phone. Same Wi-Fi? Sharing started?", "#f87171")
        self.say("Connected", "#4ade80")
        try:
            while gen == self.gen:
                n = -1
                while True:
                    line = r.readline()
                    if not line:
                        return self.say("Phone disconnected", "#f87171")
                    if line.lower().startswith(b"content-length:"):
                        n = int(line.split(b":")[1])
                    elif line.strip() == b"" and n > 0:
                        break
                self.latest = Image.open(io.BytesIO(r.read(n))); self.latest.load()
                if not self.pending:
                    self.pending = True
                    self.win.after(0, self.tick)
        except Exception:
            self.say("Connection lost", "#f87171")
        finally:
            r.close()

    def tick(self):
        self.pending = False
        self.show()

    def show(self):
        img = self.latest
        if img is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        k = min(cw / img.width, ch / img.height)
        self.tkimg = ImageTk.PhotoImage(img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.BILINEAR))
        self.canvas.delete("all"); self.canvas.create_image(cw // 2, ch // 2, image=self.tkimg)

    def shot(self):
        if self.latest is None:
            return self.say("No frame yet")
        d = os.path.join(os.path.expanduser("~"), "Pictures", APP); os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"phone_{int(time.time())}.png"); self.latest.save(p)
        self.say(f"Saved: {p}", "#4ade80")

    def close(self):
        self.gen += 1
        self.win.destroy()


# ---------- GUI ----------
BG, CARD, ACC, FG, MUTED = "#0f172a", "#1e293b", "#6366f1", "#e2e8f0", "#94a3b8"


class App:
    def __init__(self):
        self.cfg = load_cfg()
        S.pin = self.cfg.get("pin") or "%04d" % random.randint(0, 9999)
        self.cfg["pin"] = S.pin
        self.server = None
        r = self.root = tk.Tk()
        r.title(APP); r.configure(bg=BG); r.geometry("420x640"); r.resizable(False, False)
        try:
            self.logo = tk.PhotoImage(file=res("icon.png")).subsample(8, 8)
            r.iconphoto(True, self.logo)
            tk.Label(r, image=self.logo, bg=BG).pack(pady=(18, 0))
        except Exception:
            pass
        tk.Label(r, text=APP, font=("Segoe UI", 22, "bold"), fg=FG, bg=BG).pack()
        tk.Label(r, text="Mirror this PC to your phone over Wi-Fi", fg=MUTED, bg=BG).pack(pady=(0, 12))

        card = tk.Frame(r, bg=CARD, padx=16, pady=14); card.pack(fill="x", padx=22)
        self.status = tk.Label(card, text="● Stopped", fg="#f87171", bg=CARD, font=("Segoe UI", 11, "bold"))
        self.status.pack(anchor="w")
        tk.Label(card, text="Enter on your phone:", fg=MUTED, bg=CARD).pack(anchor="w", pady=(8, 0))
        self.addr = tk.Label(card, text=f"{local_ip()}", fg=FG, bg=CARD, font=("Consolas", 20, "bold"))
        self.addr.pack(anchor="w")
        tk.Label(card, text=f"Port {PORT}", fg=MUTED, bg=CARD).pack(anchor="w")
        row = tk.Frame(card, bg=CARD); row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text="PIN", fg=MUTED, bg=CARD).pack(side="left")
        self.pin_lbl = tk.Label(row, text=S.pin, fg=FG, bg=CARD, font=("Consolas", 20, "bold")); self.pin_lbl.pack(side="left", padx=10)
        self.btn(row, "New PIN", self.new_pin, CARD).pack(side="right")

        self.ctl = tk.BooleanVar(value=True)
        tk.Checkbutton(r, text="Allow phone to control mouse & keyboard", variable=self.ctl, command=self.toggle_ctl,
                       fg=FG, bg=BG, selectcolor=CARD, activebackground=BG, activeforeground=FG).pack(pady=10)
        self.main_btn = self.btn(r, "Start sharing", self.toggle, ACC, big=True); self.main_btn.pack(fill="x", padx=22)
        self.btn(r, "View phone screen on this PC", self.open_phone, CARD, big=True).pack(fill="x", padx=22, pady=(8, 0))

        tk.Label(r, text="Approved devices", fg=MUTED, bg=BG).pack(anchor="w", padx=22, pady=(14, 2))
        self.lst = tk.Listbox(r, height=4, bg=CARD, fg=FG, bd=0, highlightthickness=0); self.lst.pack(fill="x", padx=22)
        self.btn(r, "Revoke all devices", self.revoke, BG).pack(pady=6)
        S.ask = self.ask
        r.protocol("WM_DELETE_WINDOW", self.quit)
        r.after(300, self.first_run)

    def btn(self, parent, text, cmd, color, big=False):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg="white", relief="flat", bd=0, cursor="hand2",
                         activebackground="#4f46e5", activeforeground="white",
                         font=("Segoe UI", 12 if big else 9, "bold" if big else "normal"), pady=8 if big else 2)

    def first_run(self):
        if self.cfg.get("consented"):
            return
        ok = messagebox.askokcancel(
            "Permissions needed",
            f"{APP} needs your permission to:\n\n"
            "• Capture your screen and send it to approved devices\n"
            f"• Run a local server on port {PORT} (your Wi-Fi/LAN only)\n"
            "• Let your phone move the mouse and type (optional, can be turned off)\n\n"
            "Every new phone must be approved by you and know your PIN.", parent=self.root)
        if not ok:
            self.quit(); return
        if messagebox.askyesno("Windows Firewall", "Allow ScreenBridge through Windows Firewall (private networks)?\n"
                               "Windows will ask for administrator permission.", parent=self.root):
            args = f'advfirewall firewall add rule name="{APP}" dir=in action=allow protocol=TCP localport={PORT} profile=private'
            ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0)
        self.cfg["consented"] = True; save_cfg(self.cfg)

    def ask(self, ip):
        ev, out = threading.Event(), [False]
        def show():
            self.root.deiconify(); self.root.lift()
            out[0] = messagebox.askyesno("Allow device?", f"Device {ip} wants to view your screen"
                                         f"{' and control this PC' if S.allow_control else ''}.\n\nAllow it?", parent=self.root)
            ev.set()
        self.root.after(0, show)
        ev.wait(60)
        self.root.after(0, self.refresh)
        return out[0]

    def refresh(self):
        self.lst.delete(0, "end")
        for ip in sorted(S.approved):
            self.lst.insert("end", ip)

    def open_phone(self):
        if getattr(self, "pv", None) and self.pv.win.winfo_exists():
            return self.pv.win.lift()
        self.pv = PhoneViewer(self.root, self.cfg)

    def revoke(self):
        S.approved.clear(); self.refresh()

    def new_pin(self):
        S.pin = "%04d" % random.randint(0, 9999); self.pin_lbl.config(text=S.pin)
        self.cfg["pin"] = S.pin; save_cfg(self.cfg); self.revoke()

    def toggle_ctl(self):
        S.allow_control = self.ctl.get()

    def toggle(self):
        if S.running:
            S.running = False
            self.server.shutdown(); self.server.server_close()
            self.status.config(text="● Stopped", fg="#f87171"); self.main_btn.config(text="Start sharing", bg=ACC)
        else:
            try:
                self.server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            except OSError as e:
                return messagebox.showerror(APP, f"Cannot start server: {e}")
            self.server.daemon_threads = True
            S.running = True
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.addr.config(text=local_ip())
            self.status.config(text="● Sharing", fg="#4ade80"); self.main_btn.config(text="Stop sharing", bg="#dc2626")

    def quit(self):
        S.running = False
        self.root.destroy()
        os._exit(0)


if __name__ == "__main__":
    App().root.mainloop()
