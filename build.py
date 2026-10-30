"""Build a standalone Windows EXE inside the PaperCollections Conda environment."""
from pathlib import Path
import argparse
import os
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PaperDownloader in the PaperCollections environment")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"),
                        help="Output directory; use a versioned directory while an older EXE is running")
    arguments = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows EXE must be built on Windows.")
    if sys.version_info < (3, 10):
        raise SystemExit("Please install Python 3.10 or later (64-bit recommended).")
    root = Path(__file__).resolve().parent
    dist = arguments.dist_dir if arguments.dist_dir.is_absolute() else root / arguments.dist_dir
    if not (Path(sys.prefix) / "conda-meta").is_dir() or Path(sys.prefix).name.casefold() != "papercollections":
        raise SystemExit("Run: conda run -n PaperCollections python build.py")
    python = Path(sys.executable)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # PyInstaller also searches PATH for native dependencies. When launched via
    # an IDE's interpreter path, unrelated DLLs may precede this Conda env.
    conda_paths = [Path(sys.prefix) / suffix for suffix in
                   ("", "Library/bin", "DLLs", "Scripts")]
    env["PATH"] = os.pathsep.join([*(str(path) for path in conda_paths if path.is_dir()),
                                   env.get("PATH", "")])
    def run(*args: str) -> None:
        subprocess.run([str(python), *args], cwd=root, env=env, check=True)
    run("-m", "pip", "install", "-r", "requirements-build.txt")
    run("-m", "unittest", "discover", "-s", "tests", "-v")
    run("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(dist), "PaperDownloader.spec")
    report = root / "build" / "exe-self-test.json"
    report.unlink(missing_ok=True)
    subprocess.run([str(dist / "PaperDownloader.exe"), "--self-test", str(report)], cwd=root, check=True, timeout=90)
    if not report.exists():
        raise RuntimeError("EXE self-test did not produce a report.")
    print(report.read_text(encoding="utf-8"))
    print(f"Ready: {dist / 'PaperDownloader.exe'}")


if __name__ == "__main__":
    main()
