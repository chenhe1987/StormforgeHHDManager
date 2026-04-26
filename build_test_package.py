import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist146"
BUILD_DIR = ROOT / "build146"
SPEC_FILE = ROOT / "build_146.spec"
PACKAGE_DIR = DIST_DIR / "疾风知硬盘柜管理_v1.3.46"
ZIP_BASE = DIST_DIR / "疾风知硬盘柜管理_v1.3.46"
LOG_FILE = ROOT / "pyinstaller_146_output.txt"


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

    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    LOG_FILE.write_text(
        "STDOUT:\n" + result.stdout + "\nSTDERR:\n" + result.stderr,
        encoding="utf-8",
    )

    print(f"PyInstaller return code: {result.returncode}")

    if result.returncode != 0:
        raise SystemExit(result.returncode)

    if not PACKAGE_DIR.exists():
        raise SystemExit(f"打包失败：未找到输出目录 {PACKAGE_DIR}")

    zip_path = shutil.make_archive(str(ZIP_BASE), "zip", str(PACKAGE_DIR))
    print(f"ZIP created: {zip_path}")


if __name__ == "__main__":
    main()
