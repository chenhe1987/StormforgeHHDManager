import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
SPEC_FILE = ROOT / "build_159.spec"
PACKAGE_DIR = DIST_DIR / "疾风知硬盘柜管理_v1.3.59"
ZIP_BASE = DIST_DIR / "疾风知硬盘柜管理_v1.3.59"
LOG_FILE = ROOT / "pyinstaller_159_output.txt"


def _windows_creation_flags():
    flags = 0
    for attr in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS"):
        flags |= getattr(subprocess, attr, 0)
    return flags


def main():
    DIST_DIR.mkdir(exist_ok=True)

    cmd = [
        "python",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        str(SPEC_FILE),
    ]

    creationflags = _windows_creation_flags() if os.name == "nt" else 0
    with LOG_FILE.open("w", encoding="utf-8", newline="\n") as log_fp:
        log_fp.write("COMMAND:\n")
        log_fp.write(" ".join(cmd) + "\n\n")
        log_fp.flush()

        result = subprocess.run(
            cmd,
            cwd=ROOT,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
        )

    print(f"PyInstaller return code: {result.returncode}")
    print(f"Build log: {LOG_FILE}")

    if result.returncode != 0:
        raise SystemExit(result.returncode)

    if not PACKAGE_DIR.exists():
        raise SystemExit(f"打包失败：未找到输出目录 {PACKAGE_DIR}")

    zip_path = shutil.make_archive(str(ZIP_BASE), "zip", str(PACKAGE_DIR))
    print(f"ZIP created: {zip_path}")


if __name__ == "__main__":
    main()
