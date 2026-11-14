"""Where the models go: this computer or an external drive.

Also checks that a drive's format can hold large model files and, on macOS,
can erase and reformat an external drive after an explicit typed confirmation.
"""
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("MODEL_DOWNLOADER_CONFIG", HERE / "config.json"))
SYSTEM = platform.system()  # "Darwin", "Linux" or "Windows"
FOLDER = "MODELS"
SUBFOLDERS = ("GGUF", "MLX", "SAFETENSORS")
ALLOW_IMAGES = os.environ.get("MODELS_ALLOW_DISK_IMAGES") == "1"  # for testing the formatter on a disk image
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,10}$")

_root = None
_tokens = {}


# ---------------------------------------------------------------- location

def load():
    """Read the saved location. MODELS_DIR in the environment wins over the config file."""
    global _root
    env = os.environ.get("MODELS_DIR")
    if env:
        _root = Path(env).expanduser()
        return
    try:
        saved = json.loads(CONFIG.read_text()).get("models_dir")
        _root = Path(saved) if saved else None
    except (OSError, ValueError):
        _root = None


def root():
    return _root


def ready():
    """True when a location is chosen and can be written to right now."""
    return bool(_root) and _root.is_dir() and os.access(_root, os.W_OK)


def set_root(path):
    global _root
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        raise ValueError("Give the full path to a folder, for example /Users/you/MODELS.")
    if not p.parent.is_dir():
        raise ValueError(f"{p.parent} does not exist.")
    try:
        p.mkdir(exist_ok=True)
        for sub in SUBFOLDERS:
            (p / sub).mkdir(exist_ok=True)
    except OSError as e:
        raise ValueError(f"Cannot write to {p}: {e.strerror or e}")
    _root = p
    try:
        CONFIG.write_text(json.dumps({"models_dir": str(p)}, indent=2))
    except OSError:
        pass
    return p


# ---------------------------------------------------------------- drive facts

def _diskutil(*args):
    try:
        out = subprocess.run(["diskutil", *args], capture_output=True, timeout=20)
        return plistlib.loads(out.stdout) if out.returncode == 0 and out.stdout else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return {}


def fs_family(name):
    n = (name or "").lower()
    if "exfat" in n:
        return "exfat"
    if "fat" in n or "msdos" in n:
        return "fat32"
    if "apfs" in n:
        return "apfs"
    if "hfs" in n or "mac os extended" in n:
        return "hfs"
    if "ntfs" in n:
        return "ntfs"
    if n.startswith(("ext", "btrfs", "xfs", "zfs", "f2fs")):
        return "linux"
    return "unknown"


FS_LABEL = {"exfat": "exFAT", "fat32": "FAT32", "apfs": "APFS", "hfs": "Mac OS Extended", "ntfs": "NTFS",
            "linux": "Linux format", "unknown": "Unknown format"}


def verdict(family, writable):
    """(level, message): level is good, warn or bad."""
    if family == "fat32":
        return "bad", ("FAT32 cannot hold any file larger than 4 GB, and most models are bigger than that. "
                       "This drive needs to be reformatted as exFAT before it can be used.")
    if family == "ntfs" and SYSTEM == "Darwin":
        return "bad", ("This is a Windows-only format (NTFS). A Mac can read it but cannot save to it. "
                       "Reformat it as exFAT to use it on both Mac and Windows.")
    if not writable:
        return "bad", "This drive is read-only, so nothing can be saved to it."
    if family == "exfat":
        return "good", "exFAT: no file size limit, and it works on Mac, Windows and Linux. Ideal for moving models between computers."
    if family in ("apfs", "hfs"):
        return "warn", ("Mac format: fast and reliable on a Mac, but Windows and most Linux computers cannot read it. "
                        "Fine if the models will only ever be used on Macs.")
    if family == "ntfs":
        return "warn", "NTFS: great on Windows and usable on Linux, but Macs can only read it, not save to it."
    if family == "linux":
        return "warn", "Linux format: fine on Linux, but Macs and Windows computers cannot read it."
    return "warn", "I do not recognise this format. It may work, but exFAT is the safe choice for model files."


def _usage(path):
    try:
        u = shutil.disk_usage(path)
        return u.free, u.total
    except OSError:
        return 0, 0


def _mac_volumes():
    out = []
    base = Path("/Volumes")
    try:
        root_dev = os.stat("/").st_dev
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        p = base / name
        try:
            if p.is_symlink() or not os.path.ismount(p) or os.stat(p).st_dev == root_dev:
                continue
        except OSError:
            continue
        info = _diskutil("info", "-plist", str(p))
        image = info.get("BusProtocol") == "Disk Image"
        if image and not ALLOW_IMAGES:
            continue
        fam = fs_family(info.get("FilesystemType") or info.get("FilesystemName"))
        writable = bool(info.get("WritableVolume", os.access(p, os.W_OK)))
        out.append({"name": name, "mount": str(p), "family": fam, "writable": writable,
                    "can_format": (not info.get("Internal", True)) and (ALLOW_IMAGES or not image)})
    return out


def _linux_volumes():
    out = []
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if not mount.startswith(("/media/", "/run/media/", "/mnt/")):
            continue
        out.append({"name": Path(mount).name, "mount": mount, "family": fs_family(parts[2]),
                    "writable": os.access(mount, os.W_OK), "can_format": False})
    return out


def _windows_volumes():
    out = []
    try:
        import ctypes
        import string
        k32 = ctypes.windll.kernel32
        system = os.environ.get("SystemDrive", "C:").upper()
        mask = k32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            drive = f"{letter}:"
            if not mask & (1 << i) or drive == system or k32.GetDriveTypeW(drive + "\\") not in (2, 3):
                continue
            label, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
            if not k32.GetVolumeInformationW(drive + "\\", label, 261, None, None, None, fs, 261):
                continue
            out.append({"name": f"{label.value or 'Drive'} ({drive})", "mount": drive + "\\", "family": fs_family(fs.value),
                        "writable": os.access(drive + "\\", os.W_OK), "can_format": False})
    except Exception:
        pass
    return out


def volumes():
    return {"Darwin": _mac_volumes, "Linux": _linux_volumes, "Windows": _windows_volumes}.get(SYSTEM, list)()


def locations():
    """Everything the 'where should I save?' screen needs."""
    home = Path.home()
    free, total = _usage(home)
    local = {"kind": "local", "name": "This computer", "path": str(home / FOLDER), "free": free, "total": total,
             "level": "good" if free > 30e9 else "warn",
             "advice": ("Saves into a MODELS folder in your home folder. Simple, and the fastest to load from."
                        if free > 30e9 else
                        f"Only {free / 1e9:.0f} GB free here. Fine for small models, but big ones will not fit."),
             "format": "", "can_format": False}
    options = [local]
    for v in volumes():
        free, total = _usage(v["mount"])
        level, advice = verdict(v["family"], v["writable"])
        options.append({"kind": "external", "name": v["name"], "path": str(Path(v["mount"]) / FOLDER), "mount": v["mount"],
                        "free": free, "total": total, "level": level, "advice": advice,
                        "format": FS_LABEL[v["family"]], "can_format": v["can_format"] and SYSTEM == "Darwin"})
    return {"current": str(_root) if _root else "", "ready": ready(), "system": SYSTEM, "options": options}


# ---------------------------------------------------------------- formatting (macOS only)

def _whole_disk(target):
    """The physical whole-disk identifier (like 'disk4') behind a volume or mount point."""
    info = _diskutil("info", "-plist", str(target))
    stores = info.get("APFSPhysicalStores") or []
    if stores:
        info = _diskutil("info", "-plist", stores[0].get("APFSPhysicalStore", ""))
    return info.get("ParentWholeDisk", "")


def _disk_volumes(whole):
    names = []
    listing = _diskutil("list", "-plist", whole)
    for disk in listing.get("AllDisksAndPartitions", []):
        for part in disk.get("Partitions", []) + disk.get("APFSVolumes", []):
            if part.get("VolumeName") and part.get("MountPoint"):
                names.append(part["VolumeName"])
    return names


def _check_erasable(mount):
    """Refuse anything that is not clearly a separate external drive. Returns (whole, info)."""
    if SYSTEM != "Darwin":
        raise ValueError("The built-in formatter only works on a Mac. Use your system's disk tool instead.")
    mount = str(mount)
    vol = next((v for v in _mac_volumes() if v["mount"] == mount), None)
    if not vol:
        raise ValueError("That drive is not connected any more.")
    if not vol["can_format"]:
        raise ValueError("That is a built-in disk. I only format external drives.")
    whole = _whole_disk(mount)
    info = _diskutil("info", "-plist", whole)
    if not whole or not info:
        raise ValueError("I could not identify that drive, so I will not touch it.")
    if info.get("Internal", True):
        raise ValueError("That is a built-in disk. I only format external drives.")
    for keep in ("/", Path.home(), HERE):
        if _whole_disk(keep) == whole or os.stat(keep).st_dev == os.stat(mount).st_dev:
            raise ValueError("That drive holds this computer's system, your home folder or this app. I will not erase it.")
    return whole, info


def format_prepare(mount):
    whole, info = _check_erasable(mount)
    free, total = _usage(mount)
    for t in [t for t, v in _tokens.items() if v["expires"] < time.time()]:
        del _tokens[t]
    token = secrets.token_urlsafe(18)
    name = Path(mount).name
    _tokens[token] = {"whole": whole, "mount": str(mount), "name": name, "size": info.get("TotalSize", 0),
                      "expires": time.time() + 600}
    return {"token": token, "name": name, "model": (info.get("MediaName") or "").strip(), "size": info.get("TotalSize", 0),
            "used": max(0, total - free), "also": [n for n in _disk_volumes(whole) if n != name]}


def format_run(token, confirm, fs, name):
    plan = _tokens.pop(token, None)
    if not plan or plan["expires"] < time.time():
        raise ValueError("That took too long. Close this box and start again.")
    if confirm != plan["name"]:
        raise ValueError("The name you typed does not match the drive. Nothing was erased.")
    if fs not in ("ExFAT", "APFS"):
        raise ValueError("Pick exFAT or APFS.")
    if not NAME_RE.match(name or ""):
        raise ValueError("The new name can use letters, numbers, spaces, - and _, up to 11 characters.")
    whole, info = _check_erasable(plan["mount"])
    if whole != plan["whole"] or info.get("TotalSize", 0) != plan["size"]:
        raise ValueError("The drive changed since you opened this box. Nothing was erased.")
    try:
        out = subprocess.run(["diskutil", "eraseDisk", fs, name, "GPT", "/dev/" + whole],
                             capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError("Formatting took too long and was stopped. Check the drive in Disk Utility.")
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip().splitlines()[-1:] or ["diskutil failed"]
        raise ValueError("Formatting failed: " + tail[0])
    new_mount = Path("/Volumes") / name
    for _ in range(20):
        if os.path.ismount(new_mount):
            break
        time.sleep(0.5)
    else:
        raise ValueError("The drive was formatted but did not reappear. Unplug it and plug it back in.")
    return set_root(new_mount / FOLDER)
