"""NAKA Presence : Rich Presence Discord pilotée depuis la zone de notification Windows."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import gc
import time
import winreg
from ctypes import wintypes
from pathlib import Path

import pystray
from PIL import Image, ImageOps
from pypresence import Presence

tk = None  # tkinter n'est chargé qu'à l'ouverture du menu (économie de mémoire)


def trim_memory() -> None:
    """Rend au système la mémoire inutilisée du processus."""
    gc.collect()
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        ctypes.windll.psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        ctypes.windll.psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
    except Exception:
        pass

APP_NAME = "NAKA Presence"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NakaPresence"

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "NakaPresence"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "enabled": True,
    "client_id": "1499730037103005716",
    "details": "𝗡𝗔𝗞𝗔 🌸 - Serveur accueillant ✦ Kiss or Kill ✦ 10 grades à débloquer",
    "state": "Stats, Lovecalc & Mariages ✦ Rejoins-nous ! .gg/nakaa",
    "activity_type": 0,  # 0 = Joue à, 2 = Écoute, 3 = Regarde, 5 = Participe à
    "large_image": "",
    "large_text": "",
    "small_image": "",
    "small_text": "",
    "buttons": [{"label": "Rejoindre le serveur", "url": "https://discord.gg/ZnfGhv8eAx"}],
}

# type d'activité Discord -> libellé affiché dans le menu
ACTIVITY_TYPES = [(0, "Joue"), (2, "Écoute"), (3, "Regarde"), (5, "Participe")]

WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

try:  # netteté du menu sur écrans à forte densité
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def config_mtime() -> int:
    try:
        return CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return 0


# --- démarrage avec Windows (clé HKCU, supprimée à la désinstallation) ---------

def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


# --- icône ---------------------------------------------------------------------

def _resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def make_icon(active: bool) -> Image.Image:
    # tête seule : le personnage entier paraît minuscule dans la zone de notification
    img = Image.open(_resource("tray.png")).convert("RGBA")
    if not active:
        # version grisée et atténuée quand la présence est désactivée
        alpha = img.getchannel("A").point(lambda a: int(a * 0.55))
        img = ImageOps.grayscale(img).convert("RGBA")
        img.putalpha(alpha)
    return img


def uninstaller_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    path = Path(sys.executable).parent / "unins000.exe"
    return path if path.exists() else None


# --- présence ------------------------------------------------------------------

class PresenceWorker(threading.Thread):
    def __init__(self, app: "App") -> None:
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.refresh = threading.Event()
        self.rpc: Presence | None = None
        self.rpc_id = ""
        self.started_at = int(time.time())
        self.status = "Démarrage…"

    def _close(self) -> None:
        if self.rpc is not None:
            try:
                self.rpc.clear()
            except Exception:
                pass
            try:
                self.rpc.close()
            except Exception:
                pass
        self.rpc = None
        self.rpc_id = ""

    def _payload(self, cfg: dict) -> dict:
        data: dict = {}
        for key in ("details", "state", "large_image", "large_text", "small_image", "small_text"):
            value = str(cfg.get(key) or "").strip()
            if value:
                data[key] = value[:128]  # limite Discord
        data["type"] = cfg.get("activity_type", 0) if cfg.get("activity_type") in (0, 2, 3, 5) else 0
        # minuteur toujours affiché (depuis le lancement) : Discord ne permet pas de le masquer
        data["start"] = self.started_at
        buttons = [
            {"label": str(b.get("label", "")).strip()[:32], "url": str(b.get("url", "")).strip()}
            for b in cfg.get("buttons", [])[:2]
            if str(b.get("label", "")).strip() and str(b.get("url", "")).strip()
        ]
        if buttons:
            data["buttons"] = buttons
        return data

    @staticmethod
    def _raw_payload(data: dict) -> dict:
        """Activité brute : sans clé `timestamps` quand le minuteur est coupé.

        pypresence envoie toujours `timestamps: {start: null}` ; Discord y répond alors par
        son propre minuteur, qui repart de zéro.
        """
        activity: dict = {"type": data.get("type", 0), "instance": True}
        for key in ("details", "state"):
            if key in data:
                activity[key] = data[key]
        if "start" in data:
            activity["timestamps"] = {"start": int(data["start"])}
        assets = {k: data[k] for k in ("large_image", "large_text", "small_image", "small_text")
                  if k in data}
        if assets:
            activity["assets"] = assets
        if "buttons" in data:
            activity["buttons"] = data["buttons"]
        return {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(time.time_ns()),
        }

    def _apply(self, cfg: dict) -> None:
        client_id = str(cfg.get("client_id") or "").strip()
        if not cfg.get("enabled"):
            # on garde la connexion ouverte : seule l'activité est effacée
            if self.rpc is not None:
                try:
                    self.rpc.clear()
                except Exception:
                    self._close()
            self._set_status("Désactivée")
        elif not client_id.isdigit():
            self._close()
            self._set_status("Application ID manquant")
        else:
            try:
                if self.rpc is None or self.rpc_id != client_id:
                    self._close()
                    self.rpc = Presence(client_id)
                    self.rpc.connect()
                    self.rpc_id = client_id
                payload = self._payload(cfg)
                if payload:
                    self.rpc.update(payload_override=self._raw_payload(payload))
                else:
                    self.rpc.clear()
                self._set_status("Active")
            except Exception:
                self._close()
                self._set_status("En attente de Discord…")

    def _alive(self) -> bool:
        """PING sur la connexion Discord : ne compte pas dans la limite des mises à jour."""
        try:
            self.rpc.send_data(3, {"nonce": str(time.time_ns())})
            self.rpc.loop.run_until_complete(self.rpc.read_output())
        except KeyError:  # PONG : réponse sans champ « evt »
            pass
        except Exception:
            return False
        return True

    def run(self) -> None:
        # Discord n'affiche qu'une mise à jour d'activité toutes les ~15 s : on n'envoie
        # l'activité que lorsqu'elle change, et on surveille la connexion par PING.
        last_sig = None
        last_check = time.time()
        last_trim = time.time() - 50  # premier nettoyage ~10 s après le lancement
        while not self.stop_event.is_set():
            self.app.reload_if_changed()
            cfg = self.app.cfg
            sig = json.dumps(cfg, sort_keys=True)
            if self.refresh.is_set() or sig != last_sig:
                self.refresh.clear()
                self._apply(cfg)
                last_sig, last_check = sig, time.time()
            elif time.time() - last_check >= 15:
                last_check = time.time()
                if self.rpc is None:
                    if cfg.get("enabled"):
                        self._apply(cfg)  # Discord fermé : nouvelle tentative
                elif not self._alive():
                    self._close()  # Discord fermé ou redémarré : reconnexion + renvoi
                    self._apply(cfg)
            if time.time() - last_trim >= 60:
                trim_memory()
                last_trim = time.time()
            self.refresh.wait(2)
        self._close()

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.app.update_tooltip()


# --- icône de la zone de notification -------------------------------------------

class TrayIcon(pystray.Icon):
    """Double clic = activer/désactiver ; clic droit = menu personnalisé."""

    def __init__(self, *args, on_double, on_menu, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_double = on_double
        self._on_menu = on_menu

    def _on_notify(self, wparam, lparam):  # noqa: D401 (API pystray)
        if lparam == WM_LBUTTONDBLCLK:
            self._on_double()
        elif lparam == WM_RBUTTONUP:
            self._on_menu()


# --- menu stylisé (tkinter, thread dédié) ----------------------------------------

# Noir pur = « verre » : le fond du menu laisse voir l'effet acrylique de Windows.
BG = "#000000"
HOVER = "#2a2a31"
TINT = 0xB0211C1C  # teinte du fond acrylique, format AABBGGRR (≈ #1c1c21, 69 % opaque)
FG = "#f2f2f5"
MUTED = "#9a9aa6"
SEP = "#34343d"
ACCENT = "#5865F2"
OFF = "#585863"
GREEN = "#43b581"

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
dwmapi.DwmSetWindowAttribute.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmExtendFrameIntoClientArea.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetWindowCompositionAttribute.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


class _ACCENT(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                ("color", ctypes.c_uint), ("anim", ctypes.c_int)]


class _WCAD(ctypes.Structure):
    _fields_ = [("attr", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


class _MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


def _acrylic(hwnd: int) -> None:
    """Fond acrylique (flou + transparence) ; suit le réglage « Effets de transparence »."""
    margins = _MARGINS(-1, -1, -1, -1)
    dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    accent = _ACCENT(4, 2, TINT, 0)  # ACCENT_ENABLE_ACRYLICBLURBEHIND
    data = _WCAD(19, ctypes.cast(ctypes.byref(accent), ctypes.c_void_p), ctypes.sizeof(accent))
    user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _cursor() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _work_area(x: int, y: int) -> tuple[int, int, int, int]:
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
    user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


class TrayMenu:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.q: queue.Queue = queue.Queue()
        self.win: tk.Toplevel | None = None
        self.switches: list = []
        self.status_label: tk.Label | None = None
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    # appelé depuis les autres threads
    def show(self) -> None:
        # Tk n'existe que pendant que le menu est ouvert : démarré ici, détruit à la fermeture
        with self.lock:
            self.q.put(("show", _cursor()))
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()

    def refresh(self) -> None:
        with self.lock:
            if self.thread is not None:
                self.q.put(("refresh", None))

    # --- thread Tk ---
    def _loop(self) -> None:
        while True:
            try:
                self._run_tk()
            except Exception:
                pass
            with self.lock:
                if not any(cmd == "show" for cmd, _ in list(self.q.queue)):
                    self.q = queue.Queue()
                    self.thread = None
                    break
        trim_memory()

    def _run_tk(self) -> None:
        global tk
        import tkinter as tk_module
        from tkinter import font as tkfont
        from PIL import ImageTk
        tk = tk_module
        self.root = tk.Tk()
        self.root.withdraw()
        self.s = self.root.winfo_fpixels("1i") / 96
        families = set(tkfont.families(self.root))
        self.icon_font = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        size = int(40 * self.s)
        self.logo = ImageTk.PhotoImage(
            Image.open(_resource("icon.png")).convert("RGBA").resize((size, size), Image.LANCZOS))
        self.root.after(0, self._poll)
        self.root.mainloop()
        self._close()
        self.root.destroy()
        self.root = None
        self.logo = None

    def _poll(self) -> None:
        try:
            while True:
                cmd, arg = self.q.get_nowait()
                if cmd == "show":
                    self._open(arg)
                elif cmd == "refresh":
                    self._update_state()
        except queue.Empty:
            pass
        if self.win is not None:
            self._close_if_click_outside()
        if self.win is None:  # menu fermé : on quitte Tk pour libérer la mémoire
            self.root.quit()
            return
        self.root.after(50, self._poll)

    def _close(self) -> None:
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.win = None
        self.switches = []
        self.type_buttons = []
        self.status_label = None

    def _close_if_click_outside(self) -> None:
        pressed = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))
        if not pressed or self.win is None:
            return
        x, y = _cursor()
        w = self.win
        inside = (w.winfo_rootx() <= x <= w.winfo_rootx() + w.winfo_width()
                  and w.winfo_rooty() <= y <= w.winfo_rooty() + w.winfo_height())
        if not inside:
            self._close()

    def _open(self, pos: tuple[int, int]) -> None:
        self._close()
        s = self.s
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=BG)
        self.win = w

        body = tk.Frame(w, bg=BG)
        body.pack(fill="both", expand=True, padx=int(6 * s), pady=int(6 * s))
        tk.Frame(body, bg=BG, width=int(280 * s), height=1).pack()

        header = tk.Frame(body, bg=BG)
        header.pack(fill="x", pady=(int(4 * s), int(6 * s)), padx=int(6 * s))
        tk.Label(header, image=self.logo, bg=BG).pack(side="left")
        texts = tk.Frame(header, bg=BG)
        texts.pack(side="left", padx=int(12 * s))
        tk.Label(texts, text=APP_NAME, bg=BG, fg=FG, font=("Segoe UI Semibold", 11),
                 anchor="w").pack(fill="x")
        self.status_label = tk.Label(texts, bg=BG, font=("Segoe UI", 9), anchor="w")
        self.status_label.pack(fill="x")

        self._separator(body)
        app = self.app
        self._row(body, "", "Présence Discord", app.toggle_presence,
                  lambda: app.cfg["enabled"], keep_open=True)
        self._row(body, "", "Démarrer avec Windows", app.toggle_autostart,
                  autostart_enabled, keep_open=True)
        self._separator(body)
        self._type_selector(body)
        self._separator(body)
        if uninstaller_path() is not None:
            self._row(body, "", "Désinstaller…", app.uninstall)
        self._row(body, "", "Quitter", app.quit)

        self._update_state()
        self._place(w, pos)

    def _place(self, w: tk.Toplevel, pos: tuple[int, int]) -> None:
        w.update_idletasks()
        width, height = w.winfo_reqwidth(), w.winfo_reqheight()
        left, top, right, bottom = _work_area(*pos)
        margin = int(8 * self.s)
        x = min(max(pos[0] - width // 2, left + margin), right - width - margin)
        if pos[1] >= bottom - 1:          # barre des tâches en bas
            y = bottom - height - margin
        elif pos[1] <= top:               # barre des tâches en haut
            y = top + margin
        else:
            y = min(max(pos[1] - height // 2, top + margin), bottom - height - margin)
        w.geometry(f"+{x}+{y}")
        w.deiconify()
        w.lift()
        try:  # coins arrondis + liseré discret (Windows 11) + fond acrylique
            hwnd = user32.GetAncestor(w.winfo_id(), 2)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_int(0x00443A3A)), 4)
            _acrylic(hwnd)
        except Exception:
            pass

    def _separator(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=SEP, height=1).pack(fill="x", pady=int(4 * self.s), padx=int(6 * self.s))

    def _row(self, parent, glyph, text, action, state=None, keep_open=False,
             enabled=lambda: True) -> None:
        s = self.s
        row = tk.Frame(parent, bg=BG, height=int(38 * s))
        row.pack(fill="x")
        row.pack_propagate(False)
        icon = tk.Label(row, text=glyph, font=(self.icon_font, 12), bg=BG, fg=FG)
        icon.pack(side="left", padx=int(12 * s))
        label = tk.Label(row, text=text, font=("Segoe UI", 10), bg=BG, fg=FG, anchor="w")
        label.pack(side="left", fill="x", expand=True)
        widgets = [row, icon, label]
        switch = None
        if state is not None:
            switch = tk.Canvas(row, width=int(34 * s), height=int(18 * s), bg=BG,
                               highlightthickness=0)
            switch.pack(side="right", padx=int(12 * s))
            widgets.append(switch)
            self.switches.append((switch, state, (icon, label), enabled))

        def paint(color: str) -> None:
            for widget in widgets:
                widget.configure(bg=color)
            if switch is not None:
                self._draw_switch(switch, state(), color, enabled())

        def on_click(_event=None) -> None:
            if not enabled():
                return
            if not keep_open:
                self._close()
            action()
            if keep_open:
                self._update_state()

        for widget in widgets:
            widget.bind("<Enter>", lambda _e: paint(HOVER))
            widget.bind("<Leave>", lambda _e: paint(BG))
            widget.bind("<Button-1>", on_click)

    def _type_selector(self, parent: tk.Frame) -> None:
        """Choix « Joue / Écoute / Regarde / Participe » en boutons arrondis."""
        s = self.s
        tk.Label(parent, text="Affichage sur le profil", bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=int(12 * s), pady=(int(2 * s), int(4 * s)))
        bar = tk.Frame(parent, bg=BG)
        bar.pack(padx=int(10 * s), pady=(0, int(4 * s)))
        self.type_buttons = []
        width, height = int(62 * s), int(28 * s)
        for value, text in ACTIVITY_TYPES:
            cv = tk.Canvas(bar, width=width, height=height, bg=BG, highlightthickness=0,
                           cursor="hand2")
            cv.pack(side="left", padx=int(1 * s))
            self.type_buttons.append((cv, value, text))

            def choose(_e=None, v=value) -> None:
                self.app.set_activity_type(v)
                self._update_state()

            cv.bind("<Button-1>", choose)
            cv.bind("<Enter>", lambda _e, c=cv, v=value, t=text: self._draw_pill(c, t, v, True))
            cv.bind("<Leave>", lambda _e, c=cv, v=value, t=text: self._draw_pill(c, t, v, False))

    def _draw_pill(self, cv: tk.Canvas, text: str, value: int, hover: bool) -> None:
        s = self.s
        W, H = int(62 * s), int(28 * s)
        selected = value == self._current_type()
        cv.delete("all")
        fill = ACCENT if selected else (HOVER if hover else None)
        if fill:
            r = int(10 * s)  # rayon des coins
            x1, y1, x2, y2 = 1, 1, W - 1, H - 1
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            cv.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        cv.create_text(W / 2, H / 2, text=text, font=("Segoe UI", 9),
                       fill="white" if selected else FG)

    def _current_type(self) -> int:
        value = self.app.cfg.get("activity_type", 0)
        return value if value in dict(ACTIVITY_TYPES) else 0

    def _draw_switch(self, cv: tk.Canvas, on: bool, bg: str, enabled: bool = True) -> None:
        s = self.s
        W, H = int(34 * s), int(18 * s)
        color = (ACCENT if on else OFF) if enabled else SEP
        cv.configure(bg=bg)
        cv.delete("all")
        cv.create_oval(0, 0, H, H, fill=color, outline=color)
        cv.create_oval(W - H, 0, W, H, fill=color, outline=color)
        cv.create_rectangle(H / 2, 0, W - H / 2, H, fill=color, outline=color)
        pad = int(3 * s)
        x = W - H + pad if on else pad
        cv.create_oval(x, pad, x + H - 2 * pad, H - pad, fill="white", outline="white")

    def _update_state(self) -> None:
        if self.win is None:
            return
        for switch, state, labels, enabled in self.switches:
            try:
                on = enabled()
                self._draw_switch(switch, state(), switch.cget("bg"), on)
                for widget in labels:
                    widget.configure(fg=FG if on else MUTED)
            except tk.TclError:
                pass
        for cv, value, text in getattr(self, "type_buttons", []):
            try:
                self._draw_pill(cv, text, value, False)
            except tk.TclError:
                pass
        if self.status_label is not None:
            status = self.app.worker.status
            self.status_label.configure(
                text=("● " if status == "Active" else "○ ") + status,
                fg=GREEN if status == "Active" else MUTED)


# --- application ---------------------------------------------------------------

class App:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.cfg_mtime = config_mtime()
        self.worker = PresenceWorker(self)
        self.menu = TrayMenu(self)
        self.icon = TrayIcon(
            APP_NAME, make_icon(self.cfg["enabled"]), APP_NAME,
            on_double=self.toggle_presence, on_menu=self.menu.show,
        )

    def reload_if_changed(self) -> None:
        mtime = config_mtime()
        if mtime != self.cfg_mtime:
            self.cfg_mtime = mtime
            self.cfg = load_config()
            self.icon.icon = make_icon(self.cfg["enabled"])

    def _save(self) -> None:
        save_config(self.cfg)
        self.cfg_mtime = config_mtime()
        self.worker.refresh.set()
        self.menu.refresh()

    def update_tooltip(self) -> None:
        self.icon.title = f"{APP_NAME} — {self.worker.status}"
        self.menu.refresh()

    def toggle_presence(self, *_: object) -> None:
        self.cfg["enabled"] = not self.cfg["enabled"]
        self.icon.icon = make_icon(self.cfg["enabled"])
        self._save()

    def set_activity_type(self, value: int) -> None:
        if self.cfg.get("activity_type") != value:
            self.cfg["activity_type"] = value
            self._save()

    def toggle_autostart(self, *_: object) -> None:
        set_autostart(not autostart_enabled())

    def open_config(self, *_: object) -> None:
        load_config()
        os.startfile(CONFIG_FILE)  # type: ignore[attr-defined]

    def uninstall(self, *_: object) -> None:
        path = uninstaller_path()
        if path is not None:
            # le désinstalleur demande confirmation, ferme l'application et supprime tout
            subprocess.Popen([str(path)], close_fds=True)

    def quit(self, *_: object) -> None:
        self.worker.stop_event.set()
        self.worker.refresh.set()
        self.worker.join(timeout=3)
        self.icon.stop()

    def run(self) -> None:
        self.worker.start()
        if not str(self.cfg.get("client_id") or "").strip():
            threading.Timer(1.0, self.open_config).start()
        self.icon.run()


def _already_running() -> bool:
    """Une seule instance à la fois (mutex nommé, aussi utilisé par l'installateur)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _already_running.handle = kernel32.CreateMutexW(None, False, "NakaPresenceMutex")
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if not _already_running():
        App().run()
