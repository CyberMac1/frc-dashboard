"""
CTRE .hoot → .wpilog converter using the owlet CLI tool.

Owlet is automatically downloaded from CTRE's CDN and cached locally.
No manual installation required.

Reference: AdvantageScope owletDownload.ts / owletInterface.ts
Index:     https://redist.ctr-electronics.com/index.json
"""

import hashlib
import json
import os
import platform
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────
INDEX_URL    = "https://redist.ctr-electronics.com/index.json"
CACHE_DIR    = Path(os.path.expanduser("~/.frc-dashboard/owlet"))
MIN_COMPLIANCY = 6  # Phoenix 2024+

# ─── Platform detection ───────────────────────────────────────────────────────

def _owlet_platform() -> str:
    system  = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "macosuniversal"
    elif system == "linux":
        if "aarch64" in machine or "arm64" in machine:
            return "linuxarm64"
        elif "arm" in machine:
            return "linuxarm32"
        else:
            return "linuxx86-64"
    elif system == "windows":
        return "windowsx86-64"
    return ""


# ─── Hoot file helpers ────────────────────────────────────────────────────────

def read_compliancy(hoot_path: str) -> int:
    """Read the compliancy byte from a .hoot file (offset 70, uint8)."""
    with open(hoot_path, "rb") as f:
        f.seek(70)
        data = f.read(1)
    if not data:
        raise ValueError("Could not read compliancy from hoot file")
    return struct.unpack("B", data)[0]


# ─── Owlet download & cache ───────────────────────────────────────────────────

def _fetch_index() -> dict:
    with urllib.request.urlopen(INDEX_URL, timeout=10) as r:
        return json.load(r)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_owlet(compliancy: int) -> Path:
    """
    Return path to an owlet binary that supports the given compliancy.
    Downloads from CTRE CDN if not already cached.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    owlet_platform = _owlet_platform()
    if not owlet_platform:
        raise RuntimeError(f"Unsupported platform: {platform.system()} {platform.machine()}")

    # Check cache first
    existing = _find_cached(compliancy)
    if existing:
        return existing

    # Fetch index and download
    try:
        index = _fetch_index()
    except Exception as e:
        raise RuntimeError(f"Could not fetch owlet index: {e}")

    owlet_tool = next((t for t in index["Tools"] if t["Name"] == "owlet"), None)
    if owlet_tool is None:
        raise RuntimeError("owlet not found in CTRE index")

    # Find latest version for this compliancy
    matching = [v for v in owlet_tool["Items"] if v["Compliancy"] == compliancy]
    if not matching:
        raise RuntimeError(f"No owlet version available for compliancy {compliancy}")

    matching.sort(key=lambda v: v["Version"], reverse=True)
    version_info = matching[0]

    if owlet_platform not in version_info["Urls"]:
        raise RuntimeError(f"owlet not available for platform '{owlet_platform}'")

    url      = version_info["Urls"][owlet_platform]
    expected_md5 = version_info["Urls"].get(owlet_platform + "-md5", "")
    version  = version_info["Version"]
    filename = f"owlet-{version}-C{compliancy}"
    if owlet_platform.startswith("windows"):
        filename += ".exe"

    dest = CACHE_DIR / filename
    print(f"[hoot_converter] Downloading owlet {version} (compliancy {compliancy}) for {owlet_platform}…")

    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        raise RuntimeError(f"Failed to download owlet: {e}")

    # Verify MD5
    if expected_md5:
        actual_md5 = _md5(dest)
        if actual_md5 != expected_md5:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"owlet MD5 mismatch (expected {expected_md5}, got {actual_md5})")

    # Make executable on Unix
    if not owlet_platform.startswith("windows"):
        dest.chmod(0o755)

    print(f"[hoot_converter] owlet cached at {dest}")
    return dest


def _find_cached(compliancy: int):
    """Return path to a cached owlet binary for the given compliancy, if any."""
    if not CACHE_DIR.exists():
        return None
    tag = f"-C{compliancy}"
    for f in CACHE_DIR.iterdir():
        if f.name.startswith("owlet-") and tag in f.name:
            return f
    return None


# ─── Public API ──────────────────────────────────────────────────────────────

def convert_hoot_to_wpilog(hoot_path: str, output_path=None) -> str:
    """
    Convert a .hoot file to .wpilog using owlet.

    Args:
        hoot_path:   Path to the input .hoot file.
        output_path: Where to write the .wpilog. If None, writes to a temp file.

    Returns:
        Path to the output .wpilog file.

    Raises:
        RuntimeError on any failure (too old, not Pro-licensed, download failure, etc.)
    """
    compliancy = read_compliancy(hoot_path)

    if compliancy < MIN_COMPLIANCY:
        raise RuntimeError(
            f"Hoot file compliancy {compliancy} is too old "
            f"(minimum {MIN_COMPLIANCY}, requires Phoenix 2024+)"
        )

    owlet_bin = _ensure_owlet(compliancy)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wpilog", prefix="hoot_")
        os.close(fd)

    result = subprocess.run(
        [str(owlet_bin), hoot_path, output_path, "-f", "wpilog"],
        capture_output=True, text=True, timeout=60
    )

    if result.returncode != 0:
        # owlet sometimes exits with code 1 after a successful conversion due to
        # a benign "Could not read to end of input file" cleanup error.
        # Treat it as success if the output file exists and is non-empty.
        output_ok = (os.path.exists(output_path) and
                     os.path.getsize(output_path) > 0)
        if not output_ok:
            raise RuntimeError(
                f"owlet conversion failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("owlet produced an empty or missing output file")

    return output_path


def get_owlet_status() -> dict:
    """Return a dict describing owlet cache state (useful for UI/diagnostics)."""
    platform_key = _owlet_platform()
    cached = []
    if CACHE_DIR.exists():
        cached = [f.name for f in CACHE_DIR.iterdir() if f.name.startswith("owlet-")]
    return {
        "platform": platform_key,
        "cache_dir": str(CACHE_DIR),
        "cached_versions": sorted(cached),
        "supported": bool(platform_key),
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 hoot_converter.py <file.hoot> [output.wpilog]")
        sys.exit(1)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        out = convert_hoot_to_wpilog(src, dst)
        print(f"Converted: {out}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
