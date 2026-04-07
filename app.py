"""
FRC Robot Dashboard - Flask server.
Run:  python3 app.py [--port 5000] [--folder /path/to/logs]
Open: http://localhost:5000
"""
import os
import sys
import argparse
import threading
import time

from flask import Flask, render_template, jsonify, request

from analyzer import analyze_match, scan_folder, analyze_competition, find_companion_files

app = Flask(__name__)

# Default to the parent of this file (the logs folder)
DEFAULT_FOLDER = os.path.dirname(os.path.abspath(__file__))

# Simple in-memory cache: key -> (data, timestamp)
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # seconds (5 min) - re-parse if file is newer


def _cached(key: str, compute_fn, ttl: int = CACHE_TTL):
    with _cache_lock:
        if key in _cache:
            data, ts = _cache[key]
            if time.time() - ts < ttl:
                return data
    result = compute_fn()
    with _cache_lock:
        _cache[key] = (result, time.time())
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html', default_folder=DEFAULT_FOLDER)


@app.route('/api/scan')
def api_scan():
    """Scan a folder for wpilog files. Returns list of available matches."""
    folder = request.args.get('folder', DEFAULT_FOLDER)
    folder = os.path.expanduser(os.path.expandvars(folder))
    try:
        result = scan_folder(folder)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route('/api/match')
def api_match():
    """Fully analyze a single wpilog file."""
    filepath = request.args.get('file')
    if not filepath:
        return jsonify({"error": "file parameter required"}), 400
    filepath = os.path.expanduser(os.path.expandvars(filepath))
    if not os.path.exists(filepath):
        return jsonify({"error": f"File not found: {filepath}"}), 404

    # Cache key includes mtime so stale data is never served
    mtime = os.path.getmtime(filepath)
    cache_key = f"match:{filepath}:{mtime}"
    try:
        result = _cached(cache_key, lambda: analyze_match(filepath))
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route('/api/competition')
def api_competition():
    """Analyze all competition matches in a folder for trend data."""
    folder = request.args.get('folder', DEFAULT_FOLDER)
    folder = os.path.expanduser(os.path.expandvars(folder))
    cache_key = f"competition:{folder}"
    try:
        # Shorter TTL so new matches appear quickly during a competition
        result = _cached(cache_key, lambda: analyze_competition(folder), ttl=60)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route('/api/hoot_devices')
def api_hoot_devices():
    """
    Convert companion .hoot files for a given wpilog and return
    real Phoenix6 device data (temps, currents, faults).
    Caches converted wpilog files alongside the source hoot files.
    """
    filepath = request.args.get('file')
    if not filepath:
        return jsonify({"error": "file parameter required"}), 400
    filepath = os.path.expanduser(os.path.expandvars(filepath))
    if not os.path.exists(filepath):
        return jsonify({"error": f"File not found: {filepath}"}), 404

    cache_key = f"hoot_devices:{filepath}:{os.path.getmtime(filepath)}"
    try:
        def _compute():
            from hoot_analyzer import analyze_hoot_file
            companion = find_companion_files(filepath)
            hoot_list = companion.get("hoot_devices", [])

            all_devices = []
            all_issues  = []
            processed   = []

            for hd in hoot_list:
                hoot_path = hd.get("filepath")
                if not hoot_path or not os.path.exists(hoot_path):
                    continue
                try:
                    result = analyze_hoot_file(
                        hoot_path,
                        wpilog_cache_dir=os.path.dirname(hoot_path),
                    )
                    for dev in result.get("devices", []):
                        dev["bus_label"] = hd.get("bus_type", "CAN")
                        dev["bus_serial"] = hd.get("serial_short", "")
                    all_devices.extend(result.get("devices", []))
                    all_issues.extend(result.get("issues", []))
                    processed.append({
                        "filename":    hd["filename"],
                        "device_name": hd["device_name"],
                        "bus_type":    hd["bus_type"],
                        "size_kb":     hd["size_kb"],
                    })
                except Exception as exc:
                    processed.append({
                        "filename": hd.get("filename", "?"),
                        "error":    str(exc),
                    })

            return {
                "devices":    all_devices,
                "issues":     all_issues,
                "hoot_files": processed,
            }

        result = _cached(cache_key, _compute, ttl=600)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route('/api/settings')
def api_get_settings():
    """Return subsystem settings for a team number."""
    import json as _json
    team = request.args.get('team', '0')
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f'team{team}_settings.json')
    if os.path.exists(path):
        with open(path) as f:
            return jsonify(_json.load(f))
    return jsonify({"team": int(team), "subsystems": []})


@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    """Save subsystem settings for a team number."""
    import json as _json
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400
    team = data.get('team', 0)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f'team{team}_settings.json')
    with open(path, 'w') as f:
        _json.dump(data, f, indent=2)
    return jsonify({"ok": True})


@app.route('/api/pick_folder')
def api_pick_folder():
    """Open a native macOS folder picker via osascript and return the chosen path."""
    import subprocess
    try:
        result = subprocess.run(
            ['osascript', '-e',
             'POSIX path of (choose folder with prompt "Select Log Folder")'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            folder = result.stdout.strip()
            return jsonify({"folder": folder or None})
        # User cancelled (returncode 1) — not an error
        return jsonify({"folder": None})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route('/api/health')
def api_health():
    return jsonify({"status": "ok", "default_folder": DEFAULT_FOLDER})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='FRC Robot Dashboard')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Bind host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5000,
                        help='Port (default: 5000)')
    parser.add_argument('--folder', default=None,
                        help='Default log folder to scan on startup')
    args = parser.parse_args()

    if args.folder:
        DEFAULT_FOLDER = os.path.expanduser(args.folder)

    print(f"\n  FRC Dashboard  →  http://localhost:{args.port}")
    print(f"  Log folder     →  {DEFAULT_FOLDER}\n")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
