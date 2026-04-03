"""
sentinel_bridge.py — Display Bridge for Company Portal → Sentinel
Synthos · v3.0

Sits between the company portal (company_server.py) and the Sentinel
Pi Display system. Provides a clean Python API for the portal to:

  • Read / write Sentinel configuration (themes, brightness, day/night)
  • Trigger scene changes via the IPC state file
  • Manage display assets (boot, idle, informational scene files)
  • Monitor a drop folder for incoming display assets
  • Detect whether a Sentinel display is attached

Environment:
    SENTINEL_HOME       Path to Sentinel installation (default: /home/pi/sentinel)
    SENTINEL_STATE      Path to IPC state file (default: /tmp/sentinel_state.json)
    DISPLAY_DROP_DIR    Path to drop folder for incoming assets

Does NOT import any Sentinel code — communicates entirely through
config files, state files, and the filesystem.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("sentinel_bridge")

# ── Path Resolution ──────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent                      # src/
_SYNTHOS_HOME = _HERE.parent                                 # synthos_build/

SENTINEL_HOME = Path(os.getenv("SENTINEL_HOME", "/home/pi/sentinel"))
SENTINEL_CONFIG = SENTINEL_HOME / "sentinel_config.json"
SENTINEL_STATE = Path(os.getenv("SENTINEL_STATE", "/tmp/sentinel_state.json"))
SENTINEL_SCENES = SENTINEL_HOME / "scenes"
SENTINEL_LOGS = SENTINEL_HOME / "logs"

# Drop folder for incoming display assets (portal uploads land here first)
DISPLAY_DROP_DIR = Path(os.getenv(
    "DISPLAY_DROP_DIR",
    str(_SYNTHOS_HOME / "data" / "display_uploads")
))

# Asset categories and their target directories inside Sentinel
ASSET_CATEGORIES = {
    "boot":          SENTINEL_SCENES,    # Boot animation .py files
    "idle":          SENTINEL_SCENES,    # Idle animation .py files
    "informational": SENTINEL_SCENES,    # Trade/News/Weather/Alert .py files
    "theme":         SENTINEL_HOME,      # Theme config overlays (.json)
}

ALLOWED_EXTENSIONS = {".py", ".json", ".png", ".bmp", ".ttf"}

# Valid scene names for IPC
VALID_SCENES = {
    "SENTINEL_BOOT", "SENTINEL_IDLE", "SENTINEL_SETTINGS",
    "SENTINEL_TRADE", "SENTINEL_NEWS", "SENTINEL_WEATHER", "SENTINEL_ALERT",
}


# ── Screen Detection ─────────────────────────────────────────────────────────

def detect_display() -> dict:
    """
    Detect whether a Sentinel-compatible display is attached.

    Returns dict:
        present  (bool)  — True if a display is likely connected
        method   (str)   — How it was detected (framebuffer, spi, i2c, service, none)
        details  (str)   — Human-readable description
        fb_device (str)  — Framebuffer device path if found
    """
    result = {"present": False, "method": "none", "details": "", "fb_device": ""}

    # 1. Check /dev/fb1 (most common for goodtft 3.5" TFT)
    if os.path.exists("/dev/fb1"):
        result.update(present=True, method="framebuffer", fb_device="/dev/fb1",
                      details="Framebuffer /dev/fb1 detected (TFT display)")
        return result

    # 2. Check /dev/fb0 with TFT driver loaded (Pi without HDMI might use fb0)
    try:
        with open("/proc/modules", "r") as f:
            modules = f.read().lower()
        if "fbtft" in modules or "fb_ili" in modules or "fb_st7789" in modules:
            result.update(present=True, method="framebuffer", fb_device="/dev/fb0",
                          details="FBTFT driver module loaded (TFT display on fb0)")
            return result
    except (FileNotFoundError, PermissionError):
        pass

    # 3. Check SPI devices (TFT screens often use SPI)
    spi_devices = list(Path("/dev").glob("spidev*"))
    if spi_devices:
        # Check if goodtft or similar driver overlay is active
        try:
            dtoverlay_output = subprocess.run(
                ["dtoverlay", "-l"], capture_output=True, text=True, timeout=5
            )
            overlays = dtoverlay_output.stdout.lower()
            if any(x in overlays for x in ("tft", "ili", "st7789", "waveshare", "piscreen")):
                result.update(present=True, method="spi",
                              details=f"SPI display overlay active: {spi_devices[0]}")
                return result
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 4. Check I2C for small OLED displays
    try:
        i2c_output = subprocess.run(
            ["i2cdetect", "-y", "1"], capture_output=True, text=True, timeout=5
        )
        # Common OLED addresses: 0x3c, 0x3d
        if "3c" in i2c_output.stdout or "3d" in i2c_output.stdout:
            result.update(present=True, method="i2c",
                          details="I2C display detected at 0x3c/0x3d (OLED)")
            return result
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 5. Check if sentinel systemd service exists (already installed)
    try:
        svc_check = subprocess.run(
            ["systemctl", "is-enabled", "sentinel.service"],
            capture_output=True, text=True, timeout=5
        )
        if svc_check.returncode == 0:
            result.update(present=True, method="service",
                          details="sentinel.service is enabled (display previously configured)")
            return result
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    result["details"] = "No display hardware detected"
    return result


def is_display_available() -> bool:
    """Quick check: is a Sentinel display present and configured?"""
    return (
        detect_display()["present"]
        or SENTINEL_CONFIG.exists()
        or os.path.exists("/etc/systemd/system/sentinel.service")
    )


# ── Configuration Management ─────────────────────────────────────────────────

def read_config() -> dict:
    """Read sentinel_config.json. Returns empty dict on error."""
    try:
        with open(SENTINEL_CONFIG) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("Cannot read Sentinel config: %s", e)
        return {}


def write_config(config: dict) -> bool:
    """Write sentinel_config.json atomically. Returns True on success."""
    try:
        SENTINEL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(SENTINEL_CONFIG.parent), delete=False, suffix=".tmp"
        )
        json.dump(config, tmp, indent=2)
        tmp.close()
        os.replace(tmp.name, str(SENTINEL_CONFIG))
        return True
    except Exception as e:
        log.error("Failed to write Sentinel config: %s", e)
        return False


def get_display_status() -> dict:
    """
    Full display status snapshot for the portal.

    Returns:
        connected (bool), config (dict), current_scene (dict),
        service_active (bool), assets (dict), brightness (dict)
    """
    config = read_config()
    state = _read_state()

    # Check systemd service status
    service_active = False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "sentinel.service"],
            capture_output=True, text=True, timeout=5
        )
        service_active = result.stdout.strip() == "active"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return {
        "connected": is_display_available(),
        "service_active": service_active,
        "config": config,
        "current_scene": state,
        "brightness": {
            "day": config.get("day_brightness", 90),
            "night": config.get("night_brightness", 40),
        },
        "theme": {
            "active": config.get("theme", {}).get("active", "retro"),
            "available": [
                t["name"] for t in config.get("theme", {}).get("available", [])
            ],
        },
        "daynight_mode": config.get("daynight_mode", "auto"),
        "animations": config.get("animations", {}),
        "assets": list_assets(),
    }


# ── Scene Control (IPC) ─────────────────────────────────────────────────────

def _read_state() -> dict:
    """Read current scene from IPC state file."""
    try:
        with open(SENTINEL_STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"scene": "SENTINEL_IDLE", "detail": "", "timestamp": ""}


def set_scene(scene: str, detail: str = "") -> bool:
    """
    Write a scene change to the IPC state file.
    Returns True on success.
    """
    if scene not in VALID_SCENES:
        log.warning("Invalid scene name: %s", scene)
        return False

    payload = {
        "scene": scene,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(SENTINEL_STATE.parent), delete=False, suffix=".tmp"
        )
        json.dump(payload, tmp)
        tmp.close()
        os.replace(tmp.name, str(SENTINEL_STATE))
        log.info("Scene changed: %s (detail=%s)", scene, detail)
        return True
    except Exception as e:
        log.error("Failed to write scene state: %s", e)
        return False


def get_current_scene() -> dict:
    """Return the current scene and detail."""
    return _read_state()


# ── Brightness Control ───────────────────────────────────────────────────────

def set_brightness(day: Optional[int] = None, night: Optional[int] = None) -> bool:
    """
    Update brightness values in sentinel_config.json.
    Pass day=None or night=None to leave that value unchanged.
    """
    config = read_config()
    if not config:
        return False

    if day is not None:
        config["day_brightness"] = max(0, min(100, day))
    if night is not None:
        config["night_brightness"] = max(0, min(100, night))

    return write_config(config)


# ── Theme Control ────────────────────────────────────────────────────────────

def set_theme(theme_name: str) -> bool:
    """Set the active theme. Returns False if theme doesn't exist."""
    config = read_config()
    if not config:
        return False

    available = [t["name"] for t in config.get("theme", {}).get("available", [])]
    if theme_name not in available:
        log.warning("Theme '%s' not in available themes: %s", theme_name, available)
        return False

    config["theme"]["active"] = theme_name
    return write_config(config)


def get_themes() -> list[dict]:
    """Return list of available themes with implementation status."""
    config = read_config()
    return config.get("theme", {}).get("available", [])


# ── Day/Night Mode ───────────────────────────────────────────────────────────

def set_daynight_mode(mode: str) -> bool:
    """Set day/night mode to 'auto', 'force_day', or 'force_night'."""
    if mode not in ("auto", "force_day", "force_night"):
        return False
    config = read_config()
    if not config:
        return False
    config["daynight_mode"] = mode
    return write_config(config)


# ── Animation Control ────────────────────────────────────────────────────────

def set_boot_animation(animation_name: str) -> bool:
    """Set the active boot animation."""
    config = read_config()
    available = config.get("animations", {}).get("available_boot", [])
    if animation_name not in available:
        return False
    config["animations"]["boot"] = animation_name
    return write_config(config)


def set_idle_animation(animation_name: str) -> bool:
    """Set the active idle animation."""
    config = read_config()
    available = config.get("animations", {}).get("available_idle", [])
    if animation_name not in available:
        return False
    config["animations"]["idle"] = animation_name
    return write_config(config)


# ── Asset Management ─────────────────────────────────────────────────────────

def list_assets() -> dict:
    """
    List all display assets organized by category.

    Returns:
        {
            "boot": [{"name": "retro_terminal", "file": "boot.py", ...}],
            "idle": [{"name": "retro_matrix", "file": "idle.py", ...}],
            "informational": [...],
            "themes": [...]
        }
    """
    assets: dict[str, list] = {"boot": [], "idle": [], "informational": [], "themes": []}

    # Scan scenes directory
    if SENTINEL_SCENES.exists():
        for f in sorted(SENTINEL_SCENES.glob("*.py")):
            if f.name.startswith("__"):
                continue
            stat = f.stat()
            entry = {
                "file": f.name,
                "name": f.stem,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            # Categorize by filename convention
            if "boot" in f.name.lower():
                assets["boot"].append(entry)
            elif "idle" in f.name.lower():
                assets["idle"].append(entry)
            elif f.name == "stubs.py" or f.name == "settings.py":
                pass  # Skip system files
            else:
                assets["informational"].append(entry)

    # Scan themes from config
    config = read_config()
    for theme in config.get("theme", {}).get("available", []):
        assets["themes"].append({
            "name": theme["name"],
            "implemented": theme.get("implemented", False),
            "primary": theme.get("primary", ""),
            "background": theme.get("background", ""),
            "accent": theme.get("accent", ""),
        })

    return assets


def install_asset(source_path: Path, category: str, name: Optional[str] = None) -> dict:
    """
    Install a display asset from the drop folder into Sentinel's file structure.

    Args:
        source_path: Path to the file to install
        category: One of 'boot', 'idle', 'informational', 'theme'
        name: Optional display name (defaults to filename stem)

    Returns:
        {"ok": bool, "message": str, "installed_path": str}
    """
    if category not in ASSET_CATEGORIES:
        return {"ok": False, "message": f"Invalid category: {category}"}

    if not source_path.exists():
        return {"ok": False, "message": f"Source file not found: {source_path}"}

    ext = source_path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {"ok": False, "message": f"File type not allowed: {ext}"}

    target_dir = ASSET_CATEGORIES[category]
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / source_path.name

    try:
        # Copy file to Sentinel structure
        shutil.copy2(str(source_path), str(target_path))
        log.info("Installed display asset: %s → %s", source_path.name, target_path)

        # If it's an animation, register it in config
        display_name = name or source_path.stem
        if category in ("boot", "idle") and ext == ".py":
            _register_animation(category, display_name)

        return {
            "ok": True,
            "message": f"Installed {source_path.name} as {category} asset",
            "installed_path": str(target_path),
        }
    except Exception as e:
        log.error("Asset install failed: %s", e)
        return {"ok": False, "message": str(e)}


def remove_asset(filename: str, category: str) -> dict:
    """Remove a display asset. Refuses to remove core system files."""
    protected = {"boot.py", "idle.py", "settings.py", "stubs.py", "__init__.py"}
    if filename in protected:
        return {"ok": False, "message": f"Cannot remove core file: {filename}"}

    target_dir = ASSET_CATEGORIES.get(category, SENTINEL_SCENES)
    target_path = target_dir / filename

    if not target_path.exists():
        return {"ok": False, "message": f"File not found: {filename}"}

    try:
        target_path.unlink()
        log.info("Removed display asset: %s", filename)

        # Unregister from config if it was an animation
        stem = Path(filename).stem
        _unregister_animation(category, stem)

        return {"ok": True, "message": f"Removed {filename}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _register_animation(category: str, name: str):
    """Add an animation to the config's available list."""
    config = read_config()
    key = f"available_{category}"
    anims = config.get("animations", {})
    available = anims.get(key, [])
    if name not in available:
        available.append(name)
        anims[key] = available
        config["animations"] = anims
        write_config(config)
        log.info("Registered %s animation: %s", category, name)


def _unregister_animation(category: str, name: str):
    """Remove an animation from the config's available list."""
    config = read_config()
    key = f"available_{category}"
    anims = config.get("animations", {})
    available = anims.get(key, [])
    if name in available:
        available.remove(name)
        anims[key] = available
        # If active animation was removed, fall back to first available
        active_key = category  # "boot" or "idle"
        if anims.get(active_key) == name and available:
            anims[active_key] = available[0]
        config["animations"] = anims
        write_config(config)


# ── Drop Folder Watcher ──────────────────────────────────────────────────────

_watcher_thread: Optional[threading.Thread] = None
_watcher_running = False


def _categorize_file(filename: str) -> str:
    """Guess asset category from filename conventions."""
    lower = filename.lower()
    if "boot" in lower:
        return "boot"
    elif "idle" in lower:
        return "idle"
    elif "theme" in lower or filename.endswith(".json"):
        return "theme"
    else:
        return "informational"


def _process_drop_folder():
    """Scan drop folder and install any new assets."""
    if not DISPLAY_DROP_DIR.exists():
        return

    for f in DISPLAY_DROP_DIR.iterdir():
        if f.name.startswith(".") or f.is_dir():
            continue
        if f.suffix.lower() not in ALLOWED_EXTENSIONS:
            log.warning("Skipping unsupported file in drop folder: %s", f.name)
            continue

        category = _categorize_file(f.name)
        result = install_asset(f, category)

        if result["ok"]:
            # Clean up — remove from drop folder after successful install
            try:
                f.unlink()
                log.info("Cleaned up drop folder: %s", f.name)
            except Exception as e:
                log.warning("Failed to clean up %s: %s", f.name, e)
        else:
            log.error("Failed to install %s: %s", f.name, result["message"])


def _watcher_loop(poll_interval: float = 5.0):
    """Background loop that polls the drop folder."""
    global _watcher_running
    while _watcher_running:
        try:
            _process_drop_folder()
        except Exception as e:
            log.error("Drop folder watcher error: %s", e)
        time.sleep(poll_interval)


def start_watcher(poll_interval: float = 5.0):
    """Start the drop folder watcher in a background thread."""
    global _watcher_thread, _watcher_running

    if _watcher_running:
        return

    DISPLAY_DROP_DIR.mkdir(parents=True, exist_ok=True)
    _watcher_running = True
    _watcher_thread = threading.Thread(
        target=_watcher_loop, args=(poll_interval,), daemon=True
    )
    _watcher_thread.start()
    log.info("Display drop folder watcher started: %s (poll=%ss)", DISPLAY_DROP_DIR, poll_interval)


def stop_watcher():
    """Stop the drop folder watcher."""
    global _watcher_running
    _watcher_running = False
    log.info("Display drop folder watcher stopped")


# ── Sentinel Service Management ──────────────────────────────────────────────

def restart_display_service() -> dict:
    """Restart the sentinel systemd service."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "sentinel.service"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return {"ok": True, "message": "Display service restarted"}
        return {"ok": False, "message": result.stderr.strip()}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def get_display_logs(lines: int = 50) -> list[str]:
    """Read recent lines from sentinel.log."""
    log_file = SENTINEL_LOGS / "sentinel.log"
    if not log_file.exists():
        return []
    try:
        with open(log_file) as f:
            all_lines = f.readlines()
        return [l.rstrip() for l in all_lines[-lines:]]
    except Exception:
        return []


# ── Installer Helpers ────────────────────────────────────────────────────────

def install_sentinel(sentinel_repo: str = "git@github.com:personalprometheus-blip/Sentinel.git") -> dict:
    """
    Clone and install the Sentinel display system.
    Called by the company installer when a display is detected.

    Returns:
        {"ok": bool, "message": str}
    """
    if SENTINEL_CONFIG.exists():
        return {"ok": True, "message": "Sentinel already installed"}

    try:
        # Clone the repo
        SENTINEL_HOME.mkdir(parents=True, exist_ok=True)
        log.info("Cloning Sentinel repository to %s", SENTINEL_HOME)

        result = subprocess.run(
            ["git", "clone", sentinel_repo, str(SENTINEL_HOME)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0 and "already exists" not in result.stderr:
            return {"ok": False, "message": f"Git clone failed: {result.stderr.strip()}"}

        # Install Python dependencies
        req_file = SENTINEL_HOME / "requirements.txt"
        if req_file.exists():
            log.info("Installing Sentinel dependencies")
            subprocess.run(
                ["pip3", "install", "-q", "-r", str(req_file)],
                capture_output=True, text=True, timeout=120
            )

        # Create logs directory
        SENTINEL_LOGS.mkdir(parents=True, exist_ok=True)

        # Install systemd service
        service_src = SENTINEL_HOME / "sentinel.service"
        service_dst = Path("/etc/systemd/system/sentinel.service")
        if service_src.exists():
            subprocess.run(
                ["sudo", "cp", str(service_src), str(service_dst)],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ["sudo", "systemctl", "enable", "sentinel.service"],
                capture_output=True, timeout=10
            )
            log.info("Sentinel systemd service installed and enabled")

        return {"ok": True, "message": "Sentinel installed successfully"}

    except Exception as e:
        log.error("Sentinel installation failed: %s", e)
        return {"ok": False, "message": str(e)}


# ── Module Self-Test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    print("=== Sentinel Bridge Self-Test ===")
    print()

    # Detection
    display = detect_display()
    print(f"Display detected: {display['present']} ({display['method']})")
    print(f"  Details: {display['details']}")
    print()

    # Config
    config = read_config()
    if config:
        print(f"Config loaded: {len(config)} keys")
        print(f"  Active theme: {config.get('theme', {}).get('active', 'N/A')}")
        print(f"  Day brightness: {config.get('day_brightness', 'N/A')}")
        print(f"  Night brightness: {config.get('night_brightness', 'N/A')}")
    else:
        print("Config: Not found (Sentinel not installed here)")
    print()

    # Assets
    assets = list_assets()
    for cat, items in assets.items():
        print(f"  {cat}: {len(items)} assets")
    print()

    # Drop folder
    print(f"Drop folder: {DISPLAY_DROP_DIR}")
    print(f"  Exists: {DISPLAY_DROP_DIR.exists()}")

    print()
    print("=== Self-Test Complete ===")
