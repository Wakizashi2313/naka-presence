"""NAKA Presence : Rich Presence Discord pilotée depuis la zone de notification (Windows, Linux)."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import gc
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import winreg
    from ctypes import wintypes

# Paquets Linux : pystray et pypresence sont fournis à côté du programme (/usr/lib/naka-presence/vendor)
_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

if not IS_WIN:
    try:  # GTK (icône) et Tk (menu) parlent au serveur X depuis deux threads
        ctypes.CDLL("libX11.so.6").XInitThreads()
    except OSError:
        pass

import pystray
from PIL import Image, ImageOps
from pypresence import Presence

tk = None  # tkinter n'est chargé qu'à l'ouverture du menu (économie de mémoire)


def trim_memory() -> None:
    """Rend au système la mémoire inutilisée du processus."""
    gc.collect()
    try:
        if IS_WIN:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            ctypes.windll.psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
            ctypes.windll.psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
        else:
            ctypes.CDLL("libc.so.6").malloc_trim(0)  # glibc : rend les pages libres au système
    except Exception:
        pass

APP_NAME = "NAKA Presence"
APP_VERSION = "1.3.2"  # reprise par build.ps1 pour l'installateur
MUTEX_NAME = "NakaPresenceMutex"
REPO = "Wakizashi2313/naka-presence"
SETUP_ASSET = "NakaPresence-Setup.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NakaPresence"

if IS_WIN:
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "NakaPresence"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "naka-presence"
# installé par le paquet .deb / .rpm (sinon : lancé depuis les sources)
LINUX_APP_DIR = Path("/usr/lib/naka-presence")
LINUX_PACKAGED = not IS_WIN and Path(__file__).resolve().parent == LINUX_APP_DIR
CONFIG_FILE = CONFIG_DIR / "config.json"
# minuteur mis de côté pendant une mise à jour ou un redémarrage
RESUME_FILE = CONFIG_DIR / "resume.json"
RESUME_MAX_AGE = 600  # au-delà (mise à jour échouée, relance bien plus tard), on repart de zéro


def save_resume(started_at: int) -> None:
    """Retient le temps écoulé pour que le minuteur reprenne au lancement suivant."""
    now = int(time.time())
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RESUME_FILE.write_text(json.dumps({"elapsed": max(0, now - started_at), "saved": now}),
                               encoding="utf-8")
    except OSError:
        pass


def take_resume() -> int:
    """Temps écoulé à reprendre (0 si rien de récent) ; le fichier est consommé."""
    try:
        data = json.loads(RESUME_FILE.read_text(encoding="utf-8"))
        RESUME_FILE.unlink()
    except (OSError, ValueError):
        return 0
    try:
        age = int(time.time()) - int(data["saved"])
        return int(data["elapsed"]) if 0 <= age <= RESUME_MAX_AGE else 0
    except (KeyError, TypeError, ValueError):
        return 0

DEFAULT_CONFIG = {
    "enabled": True,
    "client_id": "1499730037103005716",
    "details": "𝗡𝗔𝗞𝗔 🌸 - Serveur accueillant ✦ Kiss or Kill ✦ 10 grades à débloquer",
    "state": "Stats, Lovecalc & Mariages ✦ Rejoins-nous ! .gg/nakaa",
    "activity_type": 0,  # 0 = Joue à, 2 = Écoute, 3 = Regarde, 5 = Participe à
    # musique jouée par NakaBOT dans le salon vocal (serveur NAKA) : désactivée par défaut
    "music_presence": False,
    "large_image": "",
    "large_text": "",
    "small_image": "",
    "small_text": "",
    "buttons": [
        {"label": "Visiter le site", "url": "https://naka-discord.fr/"},
        {"label": "Rejoindre le serveur", "url": "https://discord.gg/ZnfGhv8eAx"},
    ],
}

# boutons par défaut de la v1.0.0 : remplacés automatiquement par les nouveaux
OLD_DEFAULT_BUTTONS = [{"label": "Rejoindre le serveur", "url": "https://discord.gg/ZnfGhv8eAx"}]

# type d'activité Discord -> libellé affiché dans le menu
ACTIVITY_TYPES = [(0, "Joue"), (2, "Écoute"), (3, "Regarde"), (5, "Participe")]

# icônes du menu : (Segoe Fluent Icons sous Windows, symbole Unicode ailleurs)
GLYPHS = {
    "presence": ("\ue768", "▶"), "autostart": ("\ue895", "◷"), "music": ("\ue8d6", "♫"),
    "quit": ("\ue7e8", "✕"), "site": ("\ue774", "⌂"), "headphones": ("\ue7f6", "♪"),
    "stats": ("\ue9d2", "▤"), "restart": ("\ue72c", "↻"), "update": ("\ue896", "⇩"),
    "uninstall": ("\ue74d", "✕"),
}


def glyph(name: str) -> str:
    return GLYPHS[name][0 if IS_WIN else 1]


AUTOSTART_LABEL = "Démarrer avec Windows" if IS_WIN else "Démarrer avec la session"

# accès rapide aux pages du serveur : (icône, libellé, adresse)
GUILD_ID = "1500775639706370151"
QUICK_LINKS = [
    ("site", "Site", "https://naka-discord.fr/"),
    ("headphones", "Musique", f"https://musique.naka-discord.fr/g/{GUILD_ID}"),
    ("stats", "Stats", f"https://stats.naka-discord.fr/g/{GUILD_ID}"),
]

WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

if IS_WIN:
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
        if cfg.get("buttons") == OLD_DEFAULT_BUTTONS:
            cfg["buttons"] = DEFAULT_CONFIG["buttons"]
            save_config(cfg)
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


# --- démarrage avec la session -----------------------------------------------------
# Windows : clé HKCU\…\Run (supprimée à la désinstallation).
# Linux : fichier .desktop dans ~/.config/autostart (norme XDG : GNOME, KDE, Xfce…).

AUTOSTART_FILE = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
                  / "autostart" / "naka-presence.desktop")


def _command() -> list[str]:
    """Commande qui relance l'application."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    if LINUX_PACKAGED:
        return ["/usr/bin/naka-presence"]
    return [sys.executable, str(Path(__file__).resolve())]


def _exe_command() -> str:
    return " ".join(f'"{part}"' for part in _command())


def autostart_enabled() -> bool:
    if not IS_WIN:
        return AUTOSTART_FILE.exists()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if not IS_WIN:
        if on:
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text(
                "[Desktop Entry]\nType=Application\nName=NAKA Presence\n"
                f"Exec={_exe_command()} --background\nIcon=naka-presence\nTerminal=false\n"
                "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)
        return
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
    if not IS_WIN or not getattr(sys, "frozen", False):
        return None  # Linux : désinstallation par le gestionnaire de paquets
    path = Path(sys.executable).parent / "unins000.exe"
    return path if path.exists() else None


# --- mises à jour (release GitHub la plus récente) -------------------------------

def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for part in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def _http_get(url: str, timeout: float = 15):
    import urllib.request  # chargé seulement au besoin (mémoire)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"NakaPresence/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def linux_package_format() -> str | None:
    """« deb » (Debian, Ubuntu…) ou « rpm » (Fedora…) pour une installation par paquet."""
    if not LINUX_PACKAGED:
        return None
    try:
        info = Path("/etc/os-release").read_text(encoding="utf-8").lower()
    except OSError:
        info = ""
    ids = " ".join(line.split("=", 1)[1].strip('"') for line in info.splitlines()
                   if line.startswith(("id=", "id_like=")))
    if any(name in ids for name in ("debian", "ubuntu")) or Path("/usr/bin/apt-get").exists():
        return "deb"
    if any(name in ids for name in ("fedora", "rhel", "centos")) or Path("/usr/bin/dnf").exists():
        return "rpm"
    return None


def _wanted_asset(name: str) -> bool:
    if IS_WIN:
        return name == SETUP_ASSET
    fmt = linux_package_format()
    return bool(fmt) and name.startswith("naka-presence") and name.endswith(f".{fmt}")


def fetch_latest_release() -> dict | None:
    """Renvoie {version, url, digest, page} si une version plus récente existe, sinon None.

    `url` est vide quand la mise à jour ne peut pas s'installer d'ici (lancé depuis les
    sources sous Linux) : on propose alors la page de la release.
    """
    with _http_get(f"https://api.github.com/repos/{REPO}/releases/latest") as resp:
        data = json.loads(resp.read().decode("utf-8"))
    version = str(data.get("tag_name", "")).lstrip("vV")
    if not version or _version_tuple(version) <= _version_tuple(APP_VERSION):
        return None
    info = {"version": version, "url": "", "digest": "", "name": "",
            "page": str(data.get("html_url") or f"https://github.com/{REPO}/releases/latest")}
    for asset in data.get("assets", []):
        if _wanted_asset(str(asset.get("name", ""))):
            info.update(url=asset["browser_download_url"], digest=str(asset.get("digest") or ""),
                        name=str(asset["name"]))
            return info
    if IS_WIN:
        return None  # release sans installateur Windows : rien à proposer
    return info


def download_setup(info: dict) -> Path:
    """Télécharge l'installateur dans %TEMP% et vérifie son empreinte SHA-256."""
    import hashlib
    import tempfile
    name = info.get("name") or SETUP_ASSET
    stem, dot, ext = name.rpartition(".")
    target = Path(tempfile.gettempdir()) / f"{stem}-{info['version']}{dot}{ext}"
    sha = hashlib.sha256()
    tmp = target.with_suffix(".part")
    with _http_get(info["url"], timeout=60) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 16):
            sha.update(chunk)
            out.write(chunk)
    expected = info.get("digest", "")
    if expected.startswith("sha256:") and expected[7:].lower() != sha.hexdigest():
        tmp.unlink(missing_ok=True)
        raise ValueError("empreinte SHA-256 incorrecte")
    target.unlink(missing_ok=True)
    tmp.rename(target)
    if not IS_WIN:
        target.chmod(0o644)  # lisible par apt / dnf (root)
    return target


def open_target(target: str) -> None:
    """Ouvre une adresse ou un fichier avec l'application par défaut."""
    if IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(target)], close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- musique du salon vocal (API de NakaBOT sur le site du lecteur) ----------------

MUSIC_SITE = "https://musique.naka-discord.fr"
MUSIC_POLL = 10  # secondes entre deux questions à NakaBOT (seulement si l'option est activée)
MUSIC_TOKEN_FILE = CONFIG_DIR / "music_link.json"


def _http_json(url: str, data: dict | None = None, token: str = "") -> dict:
    """GET (ou POST JSON) ; PermissionError si le jeton est refusé (401)."""
    import urllib.error
    import urllib.request
    headers = {"User-Agent": f"NakaPresence/{APP_VERSION}", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("jeton refusé") from exc
        raise


def load_music_token() -> str:
    try:
        return str(json.loads(MUSIC_TOKEN_FILE.read_text(encoding="utf-8")).get("token") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def save_music_token(token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_TOKEN_FILE.write_text(json.dumps({"token": token}), encoding="utf-8")


def clear_music_token() -> None:
    MUSIC_TOKEN_FILE.unlink(missing_ok=True)


# --- présence ------------------------------------------------------------------

class PresenceWorker(threading.Thread):
    def __init__(self, app: "App") -> None:
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.refresh = threading.Event()
        self.rpc: Presence | None = None
        self.rpc_id = ""
        # après une mise à jour ou un redémarrage, le minuteur reprend où il s'était arrêté
        self.started_at = int(time.time()) - take_resume()
        self.status = "Démarrage…"
        self.music: dict | None = None   # morceau joué par NakaBOT dans le salon vocal
        self.music_note = ""             # raison d'un arrêt de l'option musique

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
                if self.music is not None:
                    self.rpc.update(payload_override=self._music_payload(cfg))
                elif payload:
                    self.rpc.update(payload_override=self._raw_payload(payload))
                else:
                    self.rpc.clear()
                self._set_status("Active · musique" if self.music is not None else "Active")
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

    # --- musique du salon vocal (option, désactivée par défaut) ---------------------
    def _music_wanted(self, cfg: dict) -> bool:
        return bool(cfg.get("enabled") and cfg.get("music_presence"))

    def _music_off(self, note: str) -> None:
        """Coupe l'option (liaison refusée ou expirée) et le dit dans le menu."""
        self.music = None
        self.app.cfg["music_presence"] = False
        save_config(self.app.cfg)
        self.music_note = note
        self.app.menu.refresh()

    def _link(self) -> None:
        """Liaison du compte : fenêtre d'autorisation dans Discord, puis jeton remis par NakaBOT."""
        if self.rpc is None:
            return
        old_timeout = getattr(self.rpc, "response_timeout", None)
        try:
            self.rpc.response_timeout = 120  # le temps de cliquer sur « Autoriser »
            self.rpc.send_data(1, {"cmd": "AUTHORIZE", "nonce": str(time.time_ns()),
                                   "args": {"client_id": self.rpc_id, "scopes": ["identify"]}})
            reply = self.rpc.loop.run_until_complete(self.rpc.read_output())
        except Exception:
            self._close()   # connexion dans un état inconnu : on la refait proprement
            self._music_off("Liaison annulée")
            return
        finally:
            if self.rpc is not None and old_timeout is not None:
                self.rpc.response_timeout = old_timeout
        code = str((reply.get("data") or {}).get("code") or "")
        if not code:
            self._music_off("Liaison refusée dans Discord")
            return
        try:
            data = _http_json(f"{MUSIC_SITE}/api/presence/link", {"code": code})
            save_music_token(str(data["token"]))
            self.music_note = ""
        except Exception:
            self._music_off("Liaison impossible avec NakaBOT")

    def _poll_music(self) -> None:
        token = load_music_token()
        if not token:
            self._link()
            token = load_music_token()
            if not token:
                return
        try:
            data = _http_json(f"{MUSIC_SITE}/api/presence/now", token=token)
        except PermissionError:
            clear_music_token()
            self._music_off("Liaison expirée : réactive l'option")
            return
        except Exception:
            return          # NakaBOT injoignable : on garde l'affichage actuel
        if not data.get("playing"):
            self.music = None
            return
        now = time.time()
        start = int(now - float(data.get("position") or 0))
        old = self.music
        same_track = old is not None and (old["title"], old["artist"]) == (data.get("title"), data.get("artist"))
        if same_track and abs(start - old["start"]) <= 3:
            start = old["start"]    # simple décalage de mesure : rien à renvoyer à Discord
        self.music = {
            "title": str(data.get("title") or "")[:128] or "Musique",
            "artist": str(data.get("artist") or "")[:100],
            "duration": int(data.get("duration") or 0), "paused": bool(data.get("paused")),
            "thumbnail": str(data.get("thumbnail") or ""), "channel": str(data.get("channel") or ""),
            "guild_id": str(data.get("guild_id") or ""), "start": start,
        }

    def _music_sig(self) -> str:
        m = self.music
        if m is None:
            return ""
        return json.dumps([m["title"], m["artist"], m["paused"], m["duration"], m["thumbnail"],
                           m["channel"], None if m["paused"] else m["start"]])

    def _music_payload(self, cfg: dict) -> dict:
        """« Écoute <titre> » : artiste, barre de progression, pochette, salon au survol."""
        m = self.music
        activity: dict = {"type": 2, "status_display_type": 2, "instance": True,
                          "details": m["title"]}
        artist = m["artist"] or "NAKA"
        activity["state"] = f"⏸ En pause · {artist}"[:128] if m["paused"] else artist
        if not m["paused"]:
            activity["timestamps"] = {"start": m["start"]}
            if m["duration"] > 0:
                activity["timestamps"]["end"] = m["start"] + m["duration"]
        assets = {"large_text": f"🔊 {m['channel']}"[:128] if m["channel"] else "NAKA"}
        if m["thumbnail"].startswith("https://"):
            assets["large_image"] = m["thumbnail"]
        activity["assets"] = assets
        buttons = [{"label": "Écouter avec nous",
                    "url": f"{MUSIC_SITE}/g/{m['guild_id'] or GUILD_ID}"}]
        join = next((b for b in cfg.get("buttons", []) if "discord.gg" in str(b.get("url", ""))), None)
        if join:
            buttons.append({"label": str(join.get("label", ""))[:32], "url": str(join["url"])})
        activity["buttons"] = buttons
        return {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity},
                "nonce": str(time.time_ns())}

    def run(self) -> None:
        # Discord n'affiche qu'une mise à jour d'activité toutes les ~15 s : on n'envoie
        # l'activité que lorsqu'elle change, et on surveille la connexion par PING.
        last_sig = None
        last_check = time.time()
        last_music = 0.0
        last_trim = time.time() - 50  # premier nettoyage ~10 s après le lancement
        while not self.stop_event.is_set():
            self.app.reload_if_changed()
            cfg = self.app.cfg
            if not self._music_wanted(cfg):
                self.music = None
            elif self.rpc is not None and time.time() - last_music >= MUSIC_POLL:
                last_music = time.time()
                self._poll_music()
                cfg = self.app.cfg
            sig = json.dumps(cfg, sort_keys=True) + self._music_sig()
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

# Windows : noir pur = « verre », le fond laisse voir l'effet acrylique. Linux : fond uni.
BG = "#000000" if IS_WIN else "#1c1c21"
HOVER = "#2a2a31"
TINT = 0xB0211C1C  # teinte du fond acrylique, format AABBGGRR (≈ #1c1c21, 69 % opaque)
FG = "#f2f2f5"
MUTED = "#9a9aa6"
SEP = "#34343d"
ACCENT = "#5865F2"
OFF = "#585863"
GREEN = "#43b581"
RED = "#f23f43"
CARD = "#26262e"        # Linux : fond des cartes arrondies
CARD_HOVER = "#31313b"

# Gabarits (en pixels à 96 ppp, multipliés par l'échelle) : menu compact sous Windows,
# fenêtre plus grande aux boutons plus gros sous Linux.
LAYOUT = {
    "width": 280, "pad": 6, "row": 38, "row_font": 10, "glyph": 12, "switch": (34, 18),
    "pill": (62, 28), "pill_font": 9, "btn": 30, "btn_font": 8, "btn_glyph": 9,
    "logo": 40, "title": 11, "small": 9, "wrap": 260, "panel_btn": 90,
} if IS_WIN else {
    "width": 380, "pad": 16, "row": 48, "row_font": 11, "glyph": 14, "switch": (44, 24),
    "pill": (80, 36), "pill_font": 10, "btn": 42, "btn_font": 10, "btn_glyph": 12,
    "logo": 52, "title": 13, "small": 10, "wrap": 330, "panel_btn": 110,
}

if IS_WIN:
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


    def _cursor() -> tuple[int, int] | None:
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


else:
    def _cursor() -> tuple[int, int] | None:
        return None  # position lue par Tk à l'ouverture du menu


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
    def show(self, update: bool = False) -> None:
        # Tk n'existe que pendant que le menu est ouvert : démarré ici, détruit à la fermeture
        with self.lock:
            self.q.put(("show", (_cursor(), update)))
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
            gc.collect()  # même raison : libérer les restes Tk dans le thread qui les a créés
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
        dpi = self.root.winfo_fpixels("1i")
        self.s = dpi / 96
        families = set(tkfont.families(self.root))
        if IS_WIN:
            self.ui = "Segoe UI"
            self.ui_title = ("Segoe UI Semibold", LAYOUT["title"])
            self.icon_font = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        else:  # police de l'interface du bureau ; les symboles viennent du repli de fontconfig
            self.ui = tkfont.nametofont("TkDefaultFont", root=self.root).actual("family")
            self.ui_title = (self.ui, LAYOUT["title"], "bold")
            self.icon_font = self.ui
            if dpi < 96:  # écran mal déclaré (Xwayland…) : textes au moins à 96 ppp
                self.root.tk.call("tk", "scaling", 96 / 72)
            # L'échelle suit la police telle qu'elle s'affiche vraiment (mise à l'échelle de
            # GNOME, « grands textes »…) : les dessins (interrupteurs, boutons) restent à sa taille.
            probe = tkfont.Font(root=self.root, family=self.ui, size=10)
            linespace = probe.metrics("linespace")
            del probe
            self.s = max(1.0, self.s, linespace / 16)  # 16 px : interligne d'un 10 pt à 96 ppp
            self.diag = f"{dpi:.0f} ppp, interligne 10 pt = {linespace} px"
        size = int(LAYOUT["logo"] * self.s)
        self.logo = ImageTk.PhotoImage(
            Image.open(_resource("icon.png")).convert("RGBA").resize((size, size), Image.LANCZOS))
        self.root.after(0, self._poll)
        self.root.mainloop()
        self._close()
        self.root.destroy()
        self.root = None
        self.logo = None
        # Les objets Tk doivent être libérés dans CE thread : un gc.collect() lancé ailleurs
        # (nettoyage mémoire du PresenceWorker) provoquait un « Tcl_AsyncDelete » fatal.
        gc.collect()

    def _poll(self) -> None:
        try:
            while True:
                cmd, arg = self.q.get_nowait()
                if cmd == "show":
                    pos, update = arg
                    if not IS_WIN and self.win is not None:  # fenêtre déjà ouverte : au premier plan
                        self.win.deiconify()
                        self.win.lift()
                        self.win.focus_force()
                    else:
                        self._open(pos or self.root.winfo_pointerxy())
                    if update:
                        self._on_update()
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
        self.redraws = []
        self.cards = []
        # aucune référence à un widget ne doit survivre au thread Tk qui l'a créé
        self.panel = self.panel_text = self.panel_buttons = self.panel_anchor = None
        self.status_label = None

    def _close_if_click_outside(self) -> None:
        if not IS_WIN:
            return  # Linux : fenêtre normale, fermée par sa barre de titre
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
        if IS_WIN:  # menu flottant près de l'icône, sans bordure
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            pad = 6
        else:  # fenêtre normale (barre de titre du bureau) ; la fermer laisse tourner la présence
            # classe X11 « naka-presence » = StartupWMClass du .desktop (bonne icône dans le dock)
            w = tk.Toplevel(self.root, class_="naka-presence")
            w.withdraw()  # affichée par _place, une fois sa position calculée
            w.title(APP_NAME)
            w.iconphoto(False, self.logo)
            w.resizable(False, False)
            w.protocol("WM_DELETE_WINDOW", self._close)
            pad = LAYOUT["pad"]
        w.configure(bg=BG)
        self.win = w
        self.pos = pos
        self.update_panel = False

        body = tk.Frame(w, bg=BG)
        body.pack(fill="both", expand=True, padx=int(pad * s), pady=int(pad * s))
        tk.Frame(body, bg=BG, width=int(LAYOUT["width"] * s), height=1).pack()

        header = tk.Frame(body, bg=BG)
        header.pack(fill="x", pady=(int(4 * s), int((6 if IS_WIN else 14) * s)), padx=int(6 * s))
        tk.Label(header, image=self.logo, bg=BG).pack(side="left")
        texts = tk.Frame(header, bg=BG)
        texts.pack(side="left", padx=int(12 * s))
        tk.Label(texts, text=APP_NAME, bg=BG, fg=FG, font=self.ui_title,
                 anchor="w").pack(fill="x")
        self.status_label = tk.Label(texts, bg=BG, font=(self.ui, LAYOUT["small"]), anchor="w")
        self.status_label.pack(fill="x")
        if not IS_WIN:
            tk.Label(header, text=f"v{APP_VERSION}", bg=BG, fg=MUTED,
                     font=(self.ui, 9)).pack(side="right", anchor="n")
        if IS_WIN:
            self._open_menu(body)
        else:
            self._open_window(body)
        self._update_state()
        self._place(w, pos)

    def _switch_rows(self, parent) -> None:
        app = self.app
        self._row(parent, glyph("presence"), "Présence Discord", app.toggle_presence,
                  lambda: app.cfg["enabled"], keep_open=True)
        self._row(parent, glyph("autostart"), AUTOSTART_LABEL, app.toggle_autostart,
                  autostart_enabled, keep_open=True)
        self._row(parent, glyph("music"), "Musique du salon vocal", app.toggle_music,
                  lambda: bool(app.cfg.get("music_presence")), keep_open=True)

    def _open_menu(self, body) -> None:
        """Windows : menu compact, sections séparées par des filets."""
        self._separator(body)
        self._switch_rows(body)
        self._separator(body)
        self._type_selector(body)
        self._separator(body)
        self._quick_links(body)
        self._separator(body)
        self._actions(body)
        self._separator(body)
        self._row(body, glyph("quit"), "Quitter", self.app.quit)

    def _open_window(self, body) -> None:
        """Linux : sections dans des cartes aux coins arrondis, gros boutons."""
        self.cards = []
        self._switch_rows(self._card(body))
        self._type_selector(self._card(body, "Affichage sur le profil"))
        self._quick_links(body)
        self._actions(body)
        self.win.update_idletasks()
        for layout in self.cards:  # hauteur des cartes connue avant le placement
            layout()

    def _section(self, parent, text: str) -> None:
        s = self.s
        tk.Label(parent, text=text, bg=parent["bg"], fg=MUTED, font=(self.ui, LAYOUT["small"]),
                 anchor="w").pack(fill="x", padx=int((12 if IS_WIN else 4) * s),
                                  pady=(int(2 * s), int((4 if IS_WIN else 6) * s)))

    def _card(self, parent, title: str = "") -> tk.Frame:
        """Carte arrondie : un Canvas dessine le fond, le contenu est un Frame posé dessus."""
        s = self.s
        if title:
            self._section(parent, title)
        r, inset = int(14 * s), int(6 * s)
        cv = tk.Canvas(parent, bg=parent["bg"], highlightthickness=0, width=1, height=1)
        cv.pack(fill="x", pady=(0, int(14 * s)))
        inner = tk.Frame(cv, bg=CARD)
        item = cv.create_window(inset, inset, window=inner, anchor="nw")

        def layout(_e=None) -> None:
            W = cv.winfo_width() if cv.winfo_width() > 1 else int(LAYOUT["width"] * s)
            H = inner.winfo_reqheight() + 2 * inset
            cv.itemconfigure(item, width=W - 2 * inset)
            if int(cv.cget("height")) != H:
                cv.configure(height=H)
            cv.delete("bg")
            x1, y1, x2, y2 = 0, 0, W - 1, H - 1
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            cv.create_polygon(pts, smooth=True, fill=CARD, outline=CARD, tags="bg")
            cv.tag_lower("bg")

        cv.bind("<Configure>", layout)
        self.cards.append(layout)
        return inner

    def _reposition(self, w: tk.Toplevel, pos: tuple[int, int]) -> None:
        w.update_idletasks()
        if not IS_WIN:
            return  # fenêtre normale : elle reste où l'utilisateur l'a mise, sa taille suit
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

    def _place(self, w: tk.Toplevel, pos: tuple[int, int]) -> None:
        if not IS_WIN:  # centrée à l'ouverture
            w.update_idletasks()
            x = (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2
            y = (w.winfo_screenheight() - w.winfo_reqheight()) // 3
            w.geometry(f"+{max(x, 0)}+{max(y, 0)}")
            w.deiconify()
            w.lift()
            w.focus_force()
            return
        self._reposition(w, pos)
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
        base = parent["bg"]
        hover_bg = CARD_HOVER if base == CARD else HOVER
        row = tk.Frame(parent, bg=base, height=int(LAYOUT["row"] * s))
        row.pack(fill="x")
        row.pack_propagate(False)
        icon = tk.Label(row, text=glyph, font=(self.icon_font, LAYOUT["glyph"]), bg=base, fg=FG)
        if not IS_WIN:
            icon.configure(width=2)  # symboles de largeurs variables : textes alignés
        icon.pack(side="left", padx=int(12 * s) if IS_WIN else (int(10 * s), int(6 * s)))
        label = tk.Label(row, text=text, font=(self.ui, LAYOUT["row_font"]), bg=base, fg=FG,
                         anchor="w")
        label.pack(side="left", fill="x", expand=True)
        widgets = [row, icon, label]
        switch = None
        if state is not None:
            sw, sh = LAYOUT["switch"]
            switch = tk.Canvas(row, width=int(sw * s), height=int(sh * s), bg=base,
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
            widget.bind("<Enter>", lambda _e: paint(hover_bg))
            widget.bind("<Leave>", lambda _e: paint(base))
            widget.bind("<Button-1>", on_click)

    # --- petits boutons : Redémarrer / Mise à jour / Désinstaller ---
    def _button(self, parent, text: str, command, glyph: str = "", primary: bool = False,
                badge=lambda: False, width: int = 0) -> tk.Canvas:
        """Petit bouton arrondi (contour discret, ou plein pour l'action principale)."""
        s = self.s
        H = int(LAYOUT["btn"] * s)
        base = parent["bg"]
        cv = tk.Canvas(parent, height=H, width=width or int(10 * s), bg=base,
                       highlightthickness=0, cursor="hand2")
        state = {"hover": False}
        label_font = (self.ui, LAYOUT["btn_font"])

        def draw(_e=None) -> None:
            W = cv.winfo_width() if cv.winfo_width() > 1 else (width or int(88 * s))
            cv.delete("all")
            r = int((9 if IS_WIN else 12) * s)
            x1, y1, x2, y2 = 1, 1, W - 2, H - 2
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            if primary:
                fill = "#4752c4" if state["hover"] else ACCENT
                outline = fill
            elif IS_WIN:
                fill = HOVER if state["hover"] else base
                outline = SEP
            else:  # Linux : boutons pleins, comme les cartes
                fill = CARD_HOVER if state["hover"] else CARD
                outline = fill
            cv.create_polygon(pts, smooth=True, fill=fill, outline=outline)
            if glyph:
                tw = _text_width(cv, text, label_font)
                gw = int(LAYOUT["btn_glyph"] * 1.8 * s)
                x0 = (W - tw - gw) / 2
                cv.create_text(x0 + gw / 2 - 2 * s, H / 2, text=glyph,
                               font=(self.icon_font, LAYOUT["btn_glyph"]), fill=FG)
                cv.create_text(x0 + gw, H / 2, text=text, font=label_font, fill=FG, anchor="w")
            else:
                cv.create_text(W / 2, H / 2, text=text, font=label_font,
                               fill="white" if primary else FG)
            if badge():  # pastille : mise à jour disponible
                d = int(8 * s)
                x2, y1 = W - int(2 * s), int(1 * s)
                cv.create_oval(x2 - d, y1, x2, y1 + d, fill=RED, outline=base, width=max(1, int(s)))

        def hover(on: bool) -> None:
            state["hover"] = on
            draw()

        cv.bind("<Configure>", draw)
        cv.bind("<Enter>", lambda _e: hover(True))
        cv.bind("<Leave>", lambda _e: hover(False))
        cv.bind("<Button-1>", lambda _e: command())
        # pas d'attribut sur le widget (cycle de références) : la fonction de dessin est
        # gardée par le menu et oubliée à sa fermeture
        self.redraws.append(draw)
        return cv

    def _quick_links(self, parent: tk.Frame) -> None:
        """Site / Musique / Stats : ouvre la page dans le navigateur (et ferme le menu Windows)."""
        s = self.s
        self._section(parent, "Accès rapide")
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=int((6 if IS_WIN else 0) * s), pady=(0, int((4 if IS_WIN else 14) * s)))
        for icon, text, url in QUICK_LINKS:
            def open_page(u=url) -> None:
                if IS_WIN:
                    self._close()
                open_target(u)  # navigateur par défaut
            self._button(bar, text, open_page, glyph=glyph(icon)).pack(
                side="left", fill="x", expand=True, padx=int(2 * s))

    def _actions(self, parent: tk.Frame) -> None:
        s = self.s
        app = self.app
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=int((6 if IS_WIN else 0) * s), pady=int(2 * s))
        items = [("restart", "Redémarrer", lambda: (self._close(), app.restart()), lambda: False),
                 ("update", "Mise à jour", self._on_update, lambda: app.update_info is not None)]
        if uninstaller_path() is not None:
            items.append(("uninstall", "Désinstaller", lambda: (self._close(), app.uninstall()),
                          lambda: False))
        if not IS_WIN:  # Linux : Quitter rejoint la rangée de boutons
            items.append(("quit", "Quitter", app.quit, lambda: False))
        for icon, text, command, badge in items:
            btn = self._button(bar, text, command, glyph=glyph(icon), badge=badge)
            btn.pack(side="left", fill="x", expand=True, padx=int(2 * s))

        # zone d'information / de confirmation de la mise à jour (masquée par défaut)
        self.panel = tk.Frame(parent, bg=BG)
        self.panel_text = tk.Label(self.panel, bg=BG, fg=FG, font=(self.ui, LAYOUT["small"]),
                                   justify="left", anchor="w",
                                   wraplength=int(LAYOUT["wrap"] * s))
        self.panel_text.pack(fill="x", padx=int(10 * s), pady=(int(6 * s), int(4 * s)))
        self.panel_buttons = tk.Frame(self.panel, bg=BG)
        later = self._button(self.panel_buttons, "Plus tard", self._hide_panel, width=int(LAYOUT["panel_btn"] * s))
        later.pack(side="right", padx=int(2 * s))
        install = self._button(self.panel_buttons, "Installer", self._confirm_update,
                               primary=True, width=int(LAYOUT["panel_btn"] * s))
        install.pack(side="right", padx=int(2 * s))
        self.panel_anchor = bar
        self.panel_sig = None

    def _on_update(self) -> None:
        self.update_panel = True
        if self.app.update_state not in ("available", "downloading"):
            self.app.check_updates()  # recherche uniquement sur demande (ou au lancement)
        self._update_state()

    def _confirm_update(self) -> None:
        self.app.install_update()
        self._update_state()

    def _hide_panel(self) -> None:
        self.update_panel = False
        self._update_state()

    def _refresh_panel(self) -> None:
        if self.panel is None:
            return
        app = self.app
        info = app.update_info or {}
        texts = {
            "checking": "Recherche d'une mise à jour…",
            "uptodate": f"Tu as déjà la dernière version (v{APP_VERSION}).",
            "error": "Impossible de vérifier ou de télécharger la mise à jour. "
                     "Réessaie plus tard.",
            "available": f"La version {info.get('version', '')} est disponible "
                         f"(tu as la {APP_VERSION}). "
                         + ("L'installer maintenant ?" if info.get("url")
                            else "Ouvrir la page de téléchargement ?"),
            "downloading": "Téléchargement de la mise à jour… "
                           + ("L'application redémarrera toute seule." if IS_WIN
                              else "Ton mot de passe sera demandé pour l'installer."),
        }
        visible = self.update_panel and app.update_state in texts
        sig = (visible, app.update_state if visible else None)
        if sig == self.panel_sig:
            return
        self.panel_sig = sig
        if visible:
            self.panel_text.configure(text=texts[app.update_state])
            if app.update_state == "available":
                self.panel_buttons.pack(fill="x", padx=int(6 * self.s),
                                        pady=(0, int(6 * self.s)))
            else:
                self.panel_buttons.pack_forget()
            self.panel.pack(fill="x", after=self.panel_anchor)
        else:
            self.panel.pack_forget()
        self._reposition(self.win, self.pos)

    def _type_selector(self, parent: tk.Frame) -> None:
        """Choix « Joue / Écoute / Regarde / Participe » en boutons arrondis."""
        s = self.s
        if IS_WIN:
            self._section(parent, "Affichage sur le profil")
        base = parent["bg"]
        bar = tk.Frame(parent, bg=base)
        bar.pack(padx=int(10 * s), pady=(0, int(4 * s)) if IS_WIN else int(4 * s))
        self.type_buttons = []
        width, height = (int(v * s) for v in LAYOUT["pill"])
        for value, text in ACTIVITY_TYPES:
            cv = tk.Canvas(bar, width=width, height=height, bg=base, highlightthickness=0,
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
        W, H = (int(v * s) for v in LAYOUT["pill"])
        selected = value == self._current_type()
        cv.delete("all")
        hover_bg = CARD_HOVER if cv.cget("bg") == CARD else HOVER
        fill = ACCENT if selected else (hover_bg if hover else None)
        if fill:
            r = int(10 * s)  # rayon des coins
            x1, y1, x2, y2 = 1, 1, W - 1, H - 1
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            cv.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        cv.create_text(W / 2, H / 2, text=text, font=(self.ui, LAYOUT["pill_font"]),
                       fill="white" if selected else FG)

    def _current_type(self) -> int:
        value = self.app.cfg.get("activity_type", 0)
        return value if value in dict(ACTIVITY_TYPES) else 0

    def _draw_switch(self, cv: tk.Canvas, on: bool, bg: str, enabled: bool = True) -> None:
        s = self.s
        W, H = (int(v * s) for v in LAYOUT["switch"])
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
        try:
            for redraw in self.redraws:
                redraw()
            self._refresh_panel()
        except tk.TclError:
            pass
        if self.status_label is not None:
            status = self.app.worker.status
            active = status.startswith("Active")
            note = self.app.worker.music_note
            self.status_label.configure(
                text=("● " if active else "○ ") + status + (f" · {note}" if note else ""),
                fg=GREEN if active and not note else MUTED)


def _text_width(widget, text: str, font) -> int:
    from tkinter import font as tkfont
    return tkfont.Font(root=widget, font=font).measure(text)


# --- application ---------------------------------------------------------------

class App:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.cfg_mtime = config_mtime()
        self.worker = PresenceWorker(self)
        # idle, checking, uptodate, available, downloading, error
        self.update_state = "idle"
        self.update_info: dict | None = None
        self.menu = TrayMenu(self)
        if IS_WIN:
            self.icon = TrayIcon(
                APP_NAME, make_icon(self.cfg["enabled"]), APP_NAME,
                on_double=self.toggle_presence, on_menu=self.menu.show,
            )
        else:
            # AppIndicator (GNOME avec l'extension, KDE, Xfce…) ne transmet pas les clics :
            # menu natif avec l'essentiel, et « Ouvrir » pour le menu complet.
            self.icon = pystray.Icon("naka-presence", make_icon(self.cfg["enabled"]), APP_NAME,
                                     menu=self._native_menu())

    def _native_menu(self):
        item = pystray.MenuItem
        return pystray.Menu(
            item("Ouvrir NAKA Presence", lambda: self.menu.show(), default=True),
            pystray.Menu.SEPARATOR,
            item("Présence Discord", self.toggle_presence, checked=lambda _i: bool(self.cfg["enabled"])),
            item("Musique du salon vocal", self.toggle_music,
                 checked=lambda _i: bool(self.cfg.get("music_presence"))),
            item(AUTOSTART_LABEL, self.toggle_autostart, checked=lambda _i: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item(lambda _i: (f"Mise à jour {self.update_info['version']} disponible…"
                             if self.update_info else "Rechercher une mise à jour"),
                 lambda: self.menu.show(update=True)),
            item("Redémarrer", self.restart),
            item("Quitter", self.quit),
        )

    def _refresh_native_menu(self) -> None:
        if not IS_WIN:
            try:
                self.icon.update_menu()
            except Exception:
                pass

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
        self._refresh_native_menu()

    def update_tooltip(self) -> None:
        title = f"{APP_NAME} — {self.worker.status}"
        if self.update_info is not None:
            title += f"\nMise à jour disponible : v{self.update_info['version']}"
        self.icon.title = title
        self.menu.refresh()
        self._refresh_native_menu()

    # --- mises à jour : recherche au lancement et au clic, jamais en tâche de fond ---
    def check_updates(self) -> None:
        if self.update_state in ("checking", "downloading"):
            return
        self.update_state = "checking"
        self.menu.refresh()
        threading.Thread(target=self._check_updates, daemon=True).start()

    def _check_updates(self) -> None:
        try:
            self.update_info = fetch_latest_release()
            self.update_state = "available" if self.update_info else "uptodate"
        except Exception:
            self.update_state = "error"
        self.update_tooltip()
        trim_memory()

    def install_update(self) -> None:
        if self.update_info is None or self.update_state == "downloading":
            return
        if not self.update_info.get("url"):
            open_target(self.update_info["page"])  # lancé depuis les sources : page de la release
            return
        self.update_state = "downloading"
        self.menu.refresh()
        threading.Thread(target=self._install_update, daemon=True).start()

    def _install_update(self) -> None:
        try:
            setup = download_setup(self.update_info)
        except Exception:
            self.update_state = "error"
            self.menu.refresh()
            return
        if not IS_WIN:
            # apt / dnf installent le paquet en root : pkexec demande le mot de passe
            tool = ["apt-get", "install", "-y"] if setup.suffix == ".deb" else ["dnf", "install", "-y"]
            try:
                done = subprocess.run(["pkexec", *tool, str(setup)], close_fds=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ok = done.returncode == 0
            except OSError:
                ok = False
            if not ok:  # mot de passe refusé ou installation impossible
                self.update_state = "error"
                self.menu.refresh()
                return
            self.restart()
            return
        # l'installateur ferme l'application si besoin, met à jour puis la relance
        save_resume(self.worker.started_at)
        subprocess.Popen([str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                         close_fds=True)
        self.quit()

    def restart(self, *_: object) -> None:
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        save_resume(self.worker.started_at)
        subprocess.Popen(_command() + ["--restart"], close_fds=True, env=env,
                         start_new_session=not IS_WIN)
        self.quit()

    def toggle_presence(self, *_: object) -> None:
        self.cfg["enabled"] = not self.cfg["enabled"]
        self.icon.icon = make_icon(self.cfg["enabled"])
        self._save()

    def set_activity_type(self, value: int) -> None:
        if self.cfg.get("activity_type") != value:
            self.cfg["activity_type"] = value
            self._save()

    def toggle_music(self, *_: object) -> None:
        # à la première activation, le PresenceWorker ouvre la fenêtre d'autorisation de Discord
        self.cfg["music_presence"] = not self.cfg.get("music_presence")
        self.worker.music_note = ""
        self._save()

    def toggle_autostart(self, *_: object) -> None:
        set_autostart(not autostart_enabled())
        self._refresh_native_menu()

    def open_config(self, *_: object) -> None:
        load_config()
        open_target(str(CONFIG_FILE))

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
        threading.Timer(5.0, self.check_updates).start()  # une seule recherche au lancement
        listener = getattr(_already_running, "sock", None)
        if listener is not None:  # Linux : un 2e lancement (lanceur, menu) ouvre la fenêtre
            threading.Thread(target=self._listen, args=(listener,), daemon=True).start()
        if not IS_WIN:
            if autostart_enabled():
                set_autostart(True)  # ancienne entrée sans --background : réécrite
            if "--background" not in sys.argv:  # lancé à la main : fenêtre ouverte
                self.menu.show()
        self.icon.run()

    def _listen(self, listener: socket.socket) -> None:
        while True:
            try:
                conn, _ = listener.accept()
                with conn:
                    if conn.recv(16).startswith(b"show"):
                        self.menu.show()
            except OSError:
                return


def _already_running(wait: float = 0) -> bool:
    """Une seule instance à la fois (mutex nommé, aussi utilisé par l'installateur).

    `wait` : délai pendant lequel on attend la fermeture de l'instance précédente
    (redémarrage depuis le menu).
    """
    deadline = time.time() + wait
    if not IS_WIN:
        # socket Unix « abstraite » : libérée par le noyau à la fin du processus (pas de
        # fichier verrou orphelin après un plantage) ; sert aussi à rouvrir le menu.
        name = f"\0naka-presence-{os.getuid()}"
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(name)
                sock.listen(4)
                _already_running.sock = sock
                return False
            except OSError:
                sock.close()
            if time.time() >= deadline:
                if not wait:  # lancé alors qu'il tourne déjà : on lui demande d'ouvrir le menu
                    try:
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
                            other.settimeout(2)
                            other.connect(name)
                            other.sendall(b"show")
                    except OSError:
                        pass
                return True
            time.sleep(0.25)
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    while True:
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            _already_running.handle = handle
            return False
        kernel32.CloseHandle(handle)
        if time.time() >= deadline:
            return True
        time.sleep(0.25)


def selftest() -> int:
    """Vérification sans Discord (intégration continue Linux) : icône, menu, présence."""
    app = App()
    print("plateforme :", sys.platform, "| icône :", type(app.icon).__module__)
    print("configuration :", CONFIG_FILE, "| paquet :", LINUX_PACKAGED, linux_package_format())
    print("activité :", sorted(app.worker._payload(app.cfg)))
    app.menu.show()
    time.sleep(4)
    win = app.menu.win
    print("menu ouvert :", win is not None, "| police :", getattr(app.menu, "ui", "?"))
    print("échelle :", round(getattr(app.menu, "s", 0), 2), "| mesures :", getattr(app.menu, "diag", ""))
    if win is not None and not IS_WIN:
        print("fenêtre :", repr(win.title()), "| classe :", win.winfo_class(),
              "| sans bordure :", bool(win.overrideredirect()))
    app.menu.refresh()
    time.sleep(1)
    return 0 if win is not None else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if not _already_running(wait=15 if "--restart" in sys.argv else 0):
        App().run()
