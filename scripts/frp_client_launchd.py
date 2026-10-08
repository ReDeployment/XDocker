"""Manage only XDocker's own macOS FRPC LaunchAgent; no secrets in plist/argv."""
import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def descriptor(root, binary):
    label = "com.redeployment.xdocker.frpc." + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return {
        "Label": label,
        "ProgramArguments": [str(binary), "-c", str(root / "data/frp/frpc.ini")],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(root / "data/frp/frpc.log"),
        "StandardErrorPath": str(root / "data/frp/frpc.log"),
    }


def manage(action, binary):
    executable = Path(shutil.which(binary) or binary).resolve()
    config = descriptor(ROOT, executable)
    target = f"gui/{os.getuid()}/{config['Label']}"
    plist = Path.home() / "Library/LaunchAgents" / (config["Label"] + ".plist")
    existing = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    if action == "stop":
        if existing.returncode == 0:
            subprocess.run(["launchctl", "bootout", target], check=True, capture_output=True)
        if plist.exists():
            saved = plistlib.loads(plist.read_bytes())
            if saved.get("WorkingDirectory") != str(ROOT):
                raise SystemExit("Refusing to remove a LaunchAgent belonging to another checkout")
            plist.unlink()
        (ROOT / "data/frp/frpc.pid").unlink(missing_ok=True)
        print("Stopped XDocker's managed client LaunchAgent.")
        return
    if existing.returncode == 0:
        raise SystemExit("Managed LaunchAgent already exists; inspect its private log or stop-client first")
    plist.parent.mkdir(parents=True, exist_ok=True)
    if plist.exists():
        saved = plistlib.loads(plist.read_bytes())
        if saved.get("WorkingDirectory") != str(ROOT):
            raise SystemExit("Existing LaunchAgent belongs to another checkout")
    with plist.open("wb") as file:
        os.fchmod(file.fileno(), 0o600)
        plistlib.dump(config, file)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True, capture_output=True)
    time.sleep(2)
    state = subprocess.run(["launchctl", "print", target], check=True, capture_output=True, text=True).stdout
    match = re.search(r"\bpid = (\d+)", state)
    if not match:
        raise SystemExit("LaunchAgent installed but FRPC is not running; inspect data/frp/frpc.log")
    (ROOT / "data/frp/frpc.pid").write_text(match.group(1) + "\n")
    print("Started XDocker's FRPC LaunchAgent; it survives terminal closure and starts at user login.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("binary", nargs="?", default="frpc")
    args = parser.parse_args()
    manage(args.action, args.binary)
