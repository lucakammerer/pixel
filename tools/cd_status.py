"""State of the host's deploy job (deploy/cd.sh): live commit, newest commit on main, last decision, paused or not.

ssh luca-kammerer 'sudo -u pixel python3 /opt/pixel/app/tools/cd_status.py'
"""

import subprocess
from pathlib import Path

CD = Path("/opt/pixel/cd")


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=False, capture_output=True, text=True).stdout.strip()


def main() -> None:
    log = (CD / "cd.log").read_text().splitlines() if (CD / "cd.log").exists() else []
    print("live   ", run("git", "-C", "/opt/pixel/app", "log", "-1", "--format=%h %s"))
    print("main   ", run("git", "-C", str(CD / "repo"), "log", "-1", "--format=%h %s", "origin/main"))
    print("last   ", log[-1] if log else "-")
    print("paused ", "yes" if (CD / "PAUSE").exists() else "no")
    print("timer  ", run("systemctl", "is-active", "pixel-cd.timer"))


if __name__ == "__main__":
    main()
