"""
FRC Robot Log Analyzer.
Parses wpilog files and extracts actionable match statistics, detects issues,
and produces competition-wide trend data.
"""
import os
import re
import statistics
from wpilog_parser import WPILogParser

# --- Thresholds ---
BROWNOUT_VOLTAGE = 6.3       # V: roboRIO brownout cutoff
BATTERY_CRITICAL = 6.5      # V
BATTERY_WARN = 8.0           # V
CAN_WARN = 0.65              # 65% utilization
CAN_CRITICAL = 0.80
LOOP_OVERRUN_MS = 25.0       # ms: 20ms target + 25% margin
LOOP_SEVERE_MS = 100.0       # ms
TEMP_WARN_C = 70.0
TEMP_CRITICAL_C = 85.0

MATCH_TYPE_NAMES = {0: "None", 1: "Practice", 2: "Qualification", 3: "Elimination"}
MATCH_TYPE_CODES = {0: "", 1: "P", 2: "Q", 3: "E"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(records: dict, field: str, default=None):
    """Return the last recorded value for a field."""
    vals = records.get(field)
    return vals[-1][1] if vals else default


def _series(records: dict, field: str) -> list:
    """Return [(ts, val)] for a field."""
    return records.get(field, [])


def _numeric(series: list) -> list:
    """Extract just the numeric values from a series."""
    return [v for _, v in series if isinstance(v, (int, float))]


def _downsample(series: list, max_points: int = 400) -> list:
    """Reduce a time series to at most max_points by uniform averaging."""
    if len(series) <= max_points:
        return series
    step = len(series) / max_points
    result = []
    i = 0.0
    while i < len(series):
        end = min(int(i + step), len(series))
        chunk = series[int(i):end]
        if chunk:
            avg = sum(v for _, v in chunk) / len(chunk)
            result.append((round(chunk[0][0], 2), round(avg, 4)))
        i += step
    return result


def _downsample_preserve_extremes(series: list, max_points: int = 600) -> list:
    """
    Downsample preserving local minima and maxima (important for battery voltage).
    For each bucket, emits both min and max points so dips are never lost.
    """
    if len(series) <= max_points:
        return series
    bucket_size = max(1, len(series) // (max_points // 2))
    result = []
    for i in range(0, len(series), bucket_size):
        chunk = series[i:i + bucket_size]
        if not chunk:
            continue
        min_pt = min(chunk, key=lambda p: p[1])
        max_pt = max(chunk, key=lambda p: p[1])
        # Keep both, in time order, deduplicating if they're the same point
        pts = sorted(set([min_pt, max_pt]), key=lambda p: p[0])
        for ts, v in pts:
            result.append((round(ts, 2), round(v, 4)))
    return result


def _count_rising_edges(series: list) -> int:
    """Count False→True transitions in a boolean series."""
    count, prev = 0, None
    for _, v in series:
        if prev is not None and v and not prev:
            count += 1
        prev = v
    return count


def _condition_starts(series: list, condition_fn) -> list:
    """Return timestamps of every False→True transition of condition_fn(value)."""
    result, prev = [], None
    for ts, v in series:
        try:
            curr = bool(condition_fn(v))
        except Exception:
            curr = False
        if prev is not None and curr and not prev:
            result.append(round(ts, 2))
        prev = curr
    return result


def _all_drop_ts(series: list) -> list:
    """Return timestamps of every True→False transition in a boolean series."""
    result, prev = [], None
    for ts, v in series:
        curr = bool(v)
        if prev is True and not curr:
            result.append(round(ts, 2))
        prev = curr
    return result


def _get_enable_periods(records: dict) -> list:
    """
    Extract contiguous enabled periods.
    Returns list of dicts: {start, end, duration, auto}
    """
    enabled = _series(records, '/DriverStation/Enabled')
    auto_series = _series(records, '/DriverStation/Autonomous')
    if not enabled:
        return []

    # Build a quick lookup: is a given timestamp in auto?
    auto_true_times = {t for t, v in auto_series if v}

    periods = []
    start = None

    for ts, val in enabled:
        if val and start is None:
            start = ts
        elif not val and start is not None:
            # Determine if this period was autonomous
            is_auto = any(start <= t <= ts for t in auto_true_times)
            periods.append({
                'start': round(start, 2),
                'end': round(ts, 2),
                'duration': round(ts - start, 2),
                'auto': is_auto
            })
            start = None

    if start is not None and enabled:
        ts = enabled[-1][0]
        is_auto = any(start <= t <= ts for t in auto_true_times)
        periods.append({
            'start': round(start, 2),
            'end': round(ts, 2),
            'duration': round(ts - start, 2),
            'auto': is_auto
        })

    return periods


def _safe_mean(vals):
    return round(statistics.mean(vals), 3) if vals else 0


def _safe_max(vals, default=0):
    return round(max(vals), 3) if vals else default


def _safe_min(vals, default=0):
    return round(min(vals), 3) if vals else default


# ---------------------------------------------------------------------------
# Main analysis functions
# ---------------------------------------------------------------------------

def analyze_match(filepath: str) -> dict:
    """
    Fully analyze a single .wpilog file.
    Returns a structured dict with all dashboard data.
    """
    parser = WPILogParser()
    records, extra_header = parser.parse(filepath)
    filename = os.path.basename(filepath)

    issues = []  # accumulated list of {severity, message}

    # ── Robot metadata ──────────────────────────────────────────────────────
    robot = {
        "team": int(_get(records, '/SystemStats/TeamNumber') or 0),
        "serial": _get(records, '/SystemStats/SerialNumber', "Unknown"),
        "project": _get(records, '/RealMetadata/ProjectName', ""),
        "branch": _get(records, '/RealMetadata/GitBranch', ""),
        "logger": extra_header or "WPILog",
    }

    # ── Match info ───────────────────────────────────────────────────────────
    match_number = int(_get(records, '/DriverStation/MatchNumber') or 0)
    match_type_raw = int(_get(records, '/DriverStation/MatchType') or 0)
    event_raw = _get(records, '/DriverStation/EventName', "") or ""
    event_name = event_raw.strip().upper()

    # Fallback: try to extract event from filename
    if not event_name:
        m = re.search(r'njfla', filename, re.IGNORECASE)
        if m:
            event_name = "NJFLA"

    type_code = MATCH_TYPE_CODES.get(match_type_raw, "")
    match_label = f"{type_code}{match_number}" if match_number else ""

    match = {
        "number": match_number,
        "type": MATCH_TYPE_NAMES.get(match_type_raw, "Unknown"),
        "type_code": type_code,
        "event": event_name,
        "label": match_label,
        "fms_attached": bool(_get(records, '/DriverStation/FMSAttached', False)),
        "ds_attached": bool(_get(records, '/DriverStation/DSAttached', False)),
        "alliance": int(_get(records, '/DriverStation/AllianceStation') or 0),
    }

    # ── Timing ───────────────────────────────────────────────────────────────
    enable_periods = _get_enable_periods(records)
    auto_duration = sum(p['duration'] for p in enable_periods if p['auto'])
    teleop_duration = sum(p['duration'] for p in enable_periods if not p['auto'])

    all_timestamps = []
    for field_data in records.values():
        if field_data:
            all_timestamps.append(field_data[0][0])
            all_timestamps.append(field_data[-1][0])
    total_duration = (max(all_timestamps) - min(all_timestamps)) if len(all_timestamps) >= 2 else 0

    timing = {
        "total_duration_s": round(total_duration, 1),
        "auto_duration_s": round(auto_duration, 1),
        "teleop_duration_s": round(teleop_duration, 1),
        "enable_periods": enable_periods,
    }

    # ── Battery ───────────────────────────────────────────────────────────────
    batt_series = _series(records, '/SystemStats/BatteryVoltage')
    brownout_series = _series(records, '/SystemStats/BrownedOut')

    # Filter out zero/boot readings (robot not yet initialised)
    batt_series_clean = [(t, v) for t, v in batt_series if v and v > 2.0]
    batt_vals = [v for _, v in batt_series_clean]
    brownout_count = _count_rising_edges(brownout_series)

    batt_min = _safe_min(batt_vals)
    batt_max = _safe_max(batt_vals)

    battery = {
        "min": batt_min,
        "max": batt_max,
        "mean": _safe_mean(batt_vals),
        "brownout_count": brownout_count,
        # Use min-preserving downsample so dips are never averaged away
        "voltage_timeline": _downsample_preserve_extremes(batt_series_clean, 600),
        # Dynamic chart bounds: always show brownout line and some headroom below min
        "chart_min": round(max(4.0, batt_min - 1.5), 1) if batt_min else 4.0,
        "chart_max": round(min(15.0, batt_max + 0.5), 1) if batt_max else 14.0,
    }

    if brownout_count > 0:
        issues.append({"severity": "critical",
                        "message": f"Brownout detected (min {battery['min']}V)",
                        "times": _condition_starts(brownout_series, lambda v: v)})
    elif battery["min"] < BATTERY_CRITICAL and battery["min"] > 0:
        issues.append({"severity": "critical",
                        "message": f"Battery near brownout: {battery['min']}V",
                        "times": _condition_starts(batt_series_clean, lambda v: 0 < v < BATTERY_CRITICAL)})
    elif battery["min"] < BATTERY_WARN and battery["min"] > 0:
        issues.append({"severity": "warning",
                        "message": f"Low battery voltage: {battery['min']}V",
                        "times": _condition_starts(batt_series_clean, lambda v: 0 < v < BATTERY_WARN)})

    # ── CAN Bus ──────────────────────────────────────────────────────────────
    can_util_series = _series(records, '/SystemStats/CANBus/Utilization')
    can_util_vals = _numeric(can_util_series)
    can_off = int(_get(records, '/SystemStats/CANBus/OffCount') or 0)
    can_rx_series  = _series(records, '/SystemStats/CANBus/ReceiveErrorCount')
    can_tx_series  = _series(records, '/SystemStats/CANBus/TransmitErrorCount')
    can_rx_err = int(can_rx_series[-1][1] if can_rx_series else 0)
    can_tx_err = int(can_tx_series[-1][1] if can_tx_series else 0)
    can_txfull = int(_get(records, '/SystemStats/CANBus/TxFullCount') or 0)

    can_bus = {
        "max_utilization": _safe_max(can_util_vals),
        "mean_utilization": _safe_mean(can_util_vals),
        "off_count": can_off,
        "rx_error_count": can_rx_err,
        "tx_error_count": can_tx_err,
        "tx_full_count": can_txfull,
        "utilization_timeline": [(round(t, 2), round(v, 4)) for t, v in _downsample(can_util_series, 300)],
        "rx_error_timeline":    [(round(t, 2), int(v))      for t, v in _downsample(can_rx_series, 300)],
        "tx_error_timeline":    [(round(t, 2), int(v))      for t, v in _downsample(can_tx_series, 300)],
    }

    if can_bus["max_utilization"] > CAN_CRITICAL:
        issues.append({"severity": "critical",
                        "message": f"CAN bus utilization critical: {can_bus['max_utilization']*100:.0f}%",
                        "times": _condition_starts(can_util_series, lambda v: v > CAN_CRITICAL)})
    elif can_bus["max_utilization"] > CAN_WARN:
        issues.append({"severity": "warning",
                        "message": f"High CAN bus utilization: {can_bus['max_utilization']*100:.0f}%",
                        "times": _condition_starts(can_util_series, lambda v: v > CAN_WARN)})

    if can_rx_err > 0 or can_off > 0:
        off_series    = _series(records, '/SystemStats/CANBus/OffCount')
        can_err_times = (_condition_starts(can_rx_series, lambda v: v and v > 0) +
                         _condition_starts(off_series, lambda v: v and v > 0))
        can_err_times.sort()
        issues.append({"severity": "warning",
                        "message": f"CAN bus errors — RX: {can_rx_err}, Off-bus: {can_off}",
                        "times": can_err_times})

    # ── Loop Performance ─────────────────────────────────────────────────────
    cycle_series = _series(records, '/RealOutputs/LoggedRobot/FullCycleMS')
    user_series = _series(records, '/RealOutputs/LoggedRobot/UserCodeMS')
    cpu_series = _series(records, '/SystemStats/CPUTempCelsius')

    # Skip first 10s of startup (always has at least one big spike)
    startup_cutoff = (cycle_series[0][0] + 10.0) if cycle_series else 0
    filtered_cycles = [(t, v) for t, v in cycle_series if t > startup_cutoff and v < 5000]
    cycle_vals = [v for _, v in filtered_cycles]
    cpu_vals = _numeric(cpu_series)

    overruns = sum(1 for v in cycle_vals if v > LOOP_OVERRUN_MS)
    severe_overruns = sum(1 for v in cycle_vals if v > LOOP_SEVERE_MS)
    p95 = round(sorted(cycle_vals)[int(len(cycle_vals) * 0.95)], 2) if len(cycle_vals) > 20 else 0

    performance = {
        "mean_cycle_ms": _safe_mean(cycle_vals),
        "p95_cycle_ms": p95,
        "max_cycle_ms": _safe_max(cycle_vals),
        "overrun_count": overruns,
        "severe_overrun_count": severe_overruns,
        "cpu_temp_mean": _safe_mean(cpu_vals),
        "cpu_temp_max": _safe_max(cpu_vals),
        "cycle_timeline": [(round(t, 2), round(min(v, 200), 2)) for t, v in _downsample(filtered_cycles, 400)],
    }

    if severe_overruns > 5:
        issues.append({"severity": "critical",
                        "message": "Severe loop overruns >100ms",
                        "times": [round(t, 2) for t, v in filtered_cycles if v > LOOP_SEVERE_MS]})
    elif overruns > 15:
        issues.append({"severity": "warning",
                        "message": "Loop overruns >25ms",
                        "times": [round(t, 2) for t, v in filtered_cycles if v > LOOP_OVERRUN_MS]})

    # ── Radio ─────────────────────────────────────────────────────────────────
    radio_series = _series(records, '/RadioStatus/Connected')
    radio_drops = _count_rising_edges([(t, not v) for t, v in radio_series])  # count disconnects
    radio_connected = bool(_get(records, '/RadioStatus/Connected', True))

    radio = {
        "connected": radio_connected,
        "drop_count": radio_drops,
    }

    if not radio_connected:
        issues.append({"severity": "critical", "message": "Radio disconnected at end of match",
                        "times": [round(radio_series[-1][0], 2)] if radio_series else []})
    elif radio_drops > 0:
        issues.append({"severity": "warning",
                        "message": "Radio connection dropped",
                        "times": _all_drop_ts(radio_series)})

    # ── Power Distribution ────────────────────────────────────────────────────
    pdp_v_vals = [v for _, v in _series(records, '/PowerDistribution/Voltage') if v and v > 0]
    pdp_i_vals = [v for _, v in _series(records, '/PowerDistribution/TotalCurrent') if v and v > 0]
    pdp_faults = int(_get(records, '/PowerDistribution/Faults') or 0)
    pdp_sticky = int(_get(records, '/PowerDistribution/StickyFaults') or 0)
    pdp_temp_vals = _numeric(_series(records, '/PowerDistribution/Temperature'))

    power = {
        "voltage_mean": _safe_mean(pdp_v_vals),
        "max_current": _safe_max(pdp_i_vals),
        "mean_current": _safe_mean(pdp_i_vals),
        "faults": pdp_faults,
        "sticky_faults": pdp_sticky,
        "temp_max": _safe_max(pdp_temp_vals),
    }

    if pdp_faults:
        issues.append({"severity": "critical", "message": f"Power distribution faults active: {pdp_faults}",
                        "times": _condition_starts(_series(records, '/PowerDistribution/Faults'), lambda v: v and v > 0)})
    if pdp_sticky:
        issues.append({"severity": "warning", "message": f"Power distribution sticky faults: {pdp_sticky}",
                        "times": _condition_starts(_series(records, '/PowerDistribution/StickyFaults'), lambda v: v and v > 0)})

    # ── Alerts from robot code ────────────────────────────────────────────────
    def collect_alerts(field):
        """Return {message: [ts_of_each_new_appearance, ...]} tracking rising edges."""
        seen = {}        # msg -> [ts, ...]
        prev_active = set()
        for ts, v in _series(records, field):
            if isinstance(v, list):
                current_active = {(s or "").strip() for s in v if len((s or "").strip()) >= 5}
                for msg in current_active - prev_active:
                    seen.setdefault(msg, []).append(round(ts, 2))
                prev_active = current_active
        return seen

    alerts = {
        "errors":      collect_alerts('/RealOutputs/Alerts/errors'),
        "warnings":    collect_alerts('/RealOutputs/Alerts/warnings'),
        "infos":       collect_alerts('/RealOutputs/Alerts/infos'),
        "pp_errors":   collect_alerts('/RealOutputs/PathPlanner/errors'),
        "pp_warnings": collect_alerts('/RealOutputs/PathPlanner/warnings'),
    }

    for msg, times in sorted(alerts["errors"].items()):
        issues.append({"severity": "error", "message": msg, "times": times})
    for msg, times in sorted(alerts["pp_errors"].items()):
        issues.append({"severity": "error", "message": f"[PathPlanner] {msg}", "times": times})
    for msg, times in sorted(alerts["warnings"].items()):
        issues.append({"severity": "warning", "message": msg, "times": times})
    for msg, times in sorted(alerts["pp_warnings"].items()):
        issues.append({"severity": "warning", "message": f"[PathPlanner] {msg}", "times": times})
    for msg, times in sorted(alerts["infos"].items()):
        issues.append({"severity": "info", "message": msg, "times": times})

    # ── Subsystems ────────────────────────────────────────────────────────────
    subsystems = {}

    # Drive (Swerve)
    speed_series = _series(records, '/RealOutputs/Drive/Speed')
    if speed_series:
        speed_vals = _numeric(speed_series)
        subsystems["Drive"] = {
            "type": "Swerve Drive",
            "max_speed_mps": _safe_max(speed_vals),
            "mean_speed_mps": _safe_mean(speed_vals),
            "field_relative": bool(_get(records, '/RealOutputs/Drive/FieldRelative', True)),
        }

    # Flywheel
    if any(k.startswith('/Flywheel/') for k in records):
        fw_vel = _numeric(_series(records, '/Flywheel/Velocity'))
        fw_temp = _numeric(_series(records, '/Flywheel/TempCelsius'))
        fw_current = _numeric(_series(records, '/Flywheel/CurrentDrawAmps'))
        fw_left = _get(records, '/Flywheel/LeftMotorConnected')
        fw_right = _get(records, '/Flywheel/RightMotorConnected')
        fw_at_sp = bool(_get(records, '/Flywheel/IsAtSetpoint', False))

        fw_data = {
            "left_connected": bool(fw_left) if fw_left is not None else None,
            "right_connected": bool(fw_right) if fw_right is not None else None,
            "max_velocity_rps": _safe_max(fw_vel),
            "mean_velocity_rps": _safe_mean(fw_vel),
            "max_temp_c": _safe_max(fw_temp),
            "max_current_a": _safe_max(fw_current),
            "reached_setpoint": fw_at_sp,
        }
        if fw_left is False or fw_right is False:
            issues.append({"severity": "critical", "message": "Flywheel motor(s) disconnected"})
        if fw_temp and max(fw_temp) > TEMP_WARN_C:
            issues.append({"severity": "warning", "message": f"Flywheel overheating: {max(fw_temp):.0f}°C"})
        subsystems["Flywheel"] = fw_data

    # Hood
    if any(k.startswith('/Hood/') for k in records):
        hood_temp = _numeric(_series(records, '/Hood/TempCelcius'))
        hood_conn = _get(records, '/Hood/MotorConnected')
        hood_angle = _numeric(_series(records, '/Hood/HoodAngle'))
        hood_current = _numeric(_series(records, '/Hood/CurrentDrawAmps'))

        hood_data = {
            "connected": bool(hood_conn) if hood_conn is not None else None,
            "max_temp_c": _safe_max(hood_temp),
            "max_angle_deg": _safe_max(hood_angle),
            "max_current_a": _safe_max(hood_current),
        }
        if hood_conn is False:
            issues.append({"severity": "critical", "message": "Hood motor disconnected"})
        if hood_temp and max(hood_temp) > TEMP_WARN_C:
            issues.append({"severity": "warning", "message": f"Hood motor overheating: {max(hood_temp):.0f}°C"})
        subsystems["Hood"] = hood_data

    # Turret
    if any(k.startswith('/Turret/') for k in records):
        turret_temp = _numeric(_series(records, '/Turret/TempCelcius'))
        turret_conn = _get(records, '/Turret/MotorConnected')
        turret_current = _numeric(_series(records, '/Turret/CurrentDrawAmps'))

        turret_data = {
            "connected": bool(turret_conn) if turret_conn is not None else None,
            "max_temp_c": _safe_max(turret_temp),
            "max_current_a": _safe_max(turret_current),
        }
        if turret_conn is False:
            issues.append({"severity": "critical", "message": "Turret motor disconnected"})
        if turret_temp and max(turret_temp) > TEMP_WARN_C:
            issues.append({"severity": "warning", "message": f"Turret motor overheating: {max(turret_temp):.0f}°C"})
        subsystems["Turret"] = turret_data

    # Vision cameras
    cam0 = _get(records, '/Vision/Camera0/Connected')
    cam1 = _get(records, '/Vision/Camera1/Connected')
    if cam0 is not None or cam1 is not None:
        vision = {
            "Camera0": {"connected": bool(cam0) if cam0 is not None else None},
            "Camera1": {"connected": bool(cam1) if cam1 is not None else None},
        }
        if cam0 is False:
            issues.append({"severity": "warning", "message": "Vision camera 0 disconnected"})
        if cam1 is False:
            issues.append({"severity": "warning", "message": "Vision camera 1 disconnected"})
        subsystems["Vision"] = vision

    # NT Clients (limelights, dashboards, etc.)
    nt_clients = {}
    for key in records:
        m = re.match(r'/SystemStats/NTClients/(.+)/Connected', key)
        if m:
            client_name = m.group(1)
            connected = bool(_get(records, key, False))
            nt_clients[client_name] = connected
            if not connected:
                drop_times = _all_drop_ts(_series(records, key))
                issues.append({"severity": "warning",
                                "message": f"NT Client disconnected: {client_name}",
                                "times": drop_times})

    # ── PDP channel current analysis ─────────────────────────────────────────
    pdp_channel_data = _series(records, '/PowerDistribution/ChannelCurrent')
    channel_max = {}
    channel_mean_acc = {}
    channel_count = {}
    for _, chans in pdp_channel_data:
        if isinstance(chans, list):
            for i, amp in enumerate(chans):
                if amp > 0:
                    channel_max[i] = max(channel_max.get(i, 0), amp)
                    channel_mean_acc[i] = channel_mean_acc.get(i, 0) + amp
                    channel_count[i] = channel_count.get(i, 0) + 1

    pdp_channels = []
    for i in sorted(channel_max.keys()):
        if channel_max[i] > 0.2:
            mean_a = channel_mean_acc[i] / channel_count[i] if channel_count[i] else 0
            pdp_channels.append({"channel": i, "max_a": round(channel_max[i], 2), "mean_a": round(mean_a, 2)})

    # ── CAN device list (from subsystem data in wpilog) ────────────────────
    can_devices = []
    if 'Flywheel' in subsystems:
        fw = subsystems['Flywheel']
        for side, conn_key in [("Left", "left_connected"), ("Right", "right_connected")]:
            can_devices.append({
                "name": f"Flywheel {side}",
                "subsystem": "Flywheel",
                "bus": "drivetrain",
                "connected": fw.get(conn_key),
                "max_temp_c": fw.get("max_temp_c"),
                "max_current_a": fw.get("max_current_a"),
                "note": f"Max vel: {fw.get('max_velocity_rps', 0)} RPS",
            })
    if 'Hood' in subsystems:
        hd = subsystems['Hood']
        can_devices.append({
            "name": "Hood Motor",
            "subsystem": "Hood",
            "bus": "rio",
            "connected": hd.get("connected"),
            "max_temp_c": hd.get("max_temp_c"),
            "max_current_a": hd.get("max_current_a"),
            "note": f"Max angle: {hd.get('max_angle_deg', 0)}°",
        })
    if 'Turret' in subsystems:
        tr = subsystems['Turret']
        can_devices.append({
            "name": "Turret Motor",
            "subsystem": "Turret",
            "bus": "rio",
            "connected": tr.get("connected"),
            "max_temp_c": tr.get("max_temp_c"),
            "max_current_a": tr.get("max_current_a"),
        })
    # PDH/PDP as a device
    if power.get("max_current", 0) > 0 or power.get("faults", 0) >= 0:
        can_devices.append({
            "name": "Power Distribution Hub",
            "subsystem": "PDH",
            "bus": "rio",
            "connected": True,
            "max_current_a": power.get("max_current"),
            "max_temp_c": power.get("temp_max") or None,
            "note": f"Peak draw: {power.get('max_current', 0)}A",
        })

    # ── Companion hoot files ───────────────────────────────────────────────
    companion = find_companion_files(filepath)

    # ── Deduplicate issues ────────────────────────────────────────────────────
    seen_msgs = set()
    deduped = []
    severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    for issue in sorted(issues, key=lambda x: severity_order.get(x["severity"], 9)):
        if issue["message"] not in seen_msgs:
            seen_msgs.add(issue["message"])
            deduped.append(issue)

    # ── Match-period filtered stats ──────────────────────────────────────────
    # Window = first Enabled edge → last Disabled edge
    match_start = enable_periods[0]['start'] if enable_periods else None
    match_end   = enable_periods[-1]['end']   if enable_periods else None

    if match_start is not None and match_end is not None:
        def _trim(s): return [(t, v) for t, v in s if match_start <= t <= match_end]

        mb_series = _trim(batt_series_clean)
        mb_vals   = [v for _, v in mb_series if v > 2]
        mb_min    = _safe_min(mb_vals)
        mb_max    = _safe_max(mb_vals)

        mc_series    = _trim(can_util_series)
        mc_rx_series = _trim(can_rx_series)
        mc_tx_series = _trim(can_tx_series)
        mc_vals   = _numeric(mc_series)

        ml_series = [(t, v) for t, v in filtered_cycles if match_start <= t <= match_end]
        ml_vals   = [v for _, v in ml_series]
        ml_overruns = sum(1 for v in ml_vals if v > LOOP_OVERRUN_MS)
        ml_severe   = sum(1 for v in ml_vals if v > LOOP_SEVERE_MS)
        ml_p95      = round(sorted(ml_vals)[int(len(ml_vals) * 0.95)], 2) if len(ml_vals) > 20 else 0

        match_stats = {
            "period": {"start": round(match_start, 2), "end": round(match_end, 2)},
            "battery": {
                "min": mb_min,
                "max": mb_max,
                "mean": _safe_mean(mb_vals),
                "brownout_count": brownout_count,
                "voltage_timeline": _downsample_preserve_extremes(mb_series, 400),
                "chart_min": round(max(4.0, mb_min - 1.5), 1) if mb_min else 4.0,
                "chart_max": round(min(15.0, mb_max + 0.5), 1) if mb_max else 14.0,
            },
            "can_bus": {
                "max_utilization": _safe_max(mc_vals),
                "mean_utilization": _safe_mean(mc_vals),
                "off_count": can_off,
                "rx_error_count": can_rx_err,
                "tx_error_count": can_tx_err,
                "tx_full_count": can_txfull,
                "utilization_timeline": [(round(t, 2), round(v, 4)) for t, v in _downsample(mc_series,    300)],
                "rx_error_timeline":    [(round(t, 2), int(v))      for t, v in _downsample(mc_rx_series, 300)],
                "tx_error_timeline":    [(round(t, 2), int(v))      for t, v in _downsample(mc_tx_series, 300)],
            },
            "performance": {
                "mean_cycle_ms": _safe_mean(ml_vals),
                "p95_cycle_ms": ml_p95,
                "max_cycle_ms": _safe_max(ml_vals),
                "overrun_count": ml_overruns,
                "severe_overrun_count": ml_severe,
                "cpu_temp_max": performance["cpu_temp_max"],
                "cpu_temp_mean": performance["cpu_temp_mean"],
                "cycle_timeline": [(round(t, 2), round(min(v, 200), 2)) for t, v in _downsample(ml_series, 400)],
            },
        }
    else:
        match_stats = None

    return {
        "file": filename,
        "filepath": filepath,
        "robot": robot,
        "match": match,
        "timing": timing,
        "battery": battery,
        "can_bus": can_bus,
        "performance": performance,
        "radio": radio,
        "power": power,
        "alerts": alerts,
        "issues": deduped,
        "subsystems": subsystems,
        "nt_clients": nt_clients,
        "can_devices": can_devices,
        "pdp_channels": pdp_channels,
        "companion_files": companion,
        "match_stats": match_stats,    # filtered to enabled window (or None)
    }


# ---------------------------------------------------------------------------
# Companion hoot file discovery
# ---------------------------------------------------------------------------

def find_companion_files(wpilog_path: str) -> dict:
    """
    Find .hoot files associated with the given wpilog.
    Looks for a sibling directory named {EVENT}_{MATCH_ID} and for flat .hoot
    files with a matching timestamp prefix in the same directory.
    """
    dirname = os.path.dirname(wpilog_path)
    filename = os.path.basename(wpilog_path)

    parts = re.match(r'akit_(\d{2}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(.*?)\.wpilog', filename)
    if not parts:
        return {"hoot_devices": []}

    suffix = parts.group(3).strip('_')
    suffix_parts = [p for p in suffix.split('_') if p]
    match_id, event = "", ""
    for p in suffix_parts:
        if re.match(r'^(q|p|e)\d+$', p, re.IGNORECASE):
            match_id = p.upper()
        elif p and not re.match(r'^p\d{4,}$', p, re.IGNORECASE):
            event = p.upper()

    hoot_devices = []

    def _read_hoot_info(hoot_path: str, filename: str) -> dict:
        with open(hoot_path, 'rb') as fh:
            hdr = fh.read(64)
        dev_name = hdr.split(b'\x00')[0].decode('utf-8', errors='replace').strip()
        is_rio = '_rio_' in filename
        serial_m = re.search(r'_([0-9A-F]{32})_', filename)
        serial = serial_m.group(1)[:8] if serial_m else 'rio'
        return {
            "filepath": hoot_path,
            "filename": filename,
            "device_name": dev_name or ("roboRIO CAN Bus" if is_rio else "CANivore"),
            "bus_type": "roboRIO CAN" if is_rio else "CANivore",
            "serial_short": serial,
            "size_kb": os.path.getsize(hoot_path) // 1024,
        }

    # Option 1: subdirectory named {EVENT}_{MATCH_ID}
    if event and match_id:
        hoot_dir = os.path.join(dirname, f"{event}_{match_id}")
        if os.path.isdir(hoot_dir):
            for f in sorted(os.listdir(hoot_dir)):
                if f.endswith('.hoot'):
                    try:
                        hoot_devices.append(_read_hoot_info(os.path.join(hoot_dir, f), f))
                    except Exception:
                        pass

    # Option 2: flat .hoot files in the same directory with match ID in name
    if match_id:
        for f in sorted(os.listdir(dirname)):
            if f.endswith('.hoot') and match_id.lower() in f.lower():
                try:
                    hoot_devices.append(_read_hoot_info(os.path.join(dirname, f), f))
                except Exception:
                    pass

    return {"hoot_devices": hoot_devices, "event": event, "match_id": match_id}


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

def scan_folder(folder_path: str) -> dict:
    """
    Scan a directory for .wpilog files and return match metadata.
    Does NOT parse file contents — just inspects filenames and sizes.
    """
    if not os.path.isdir(folder_path):
        return {"error": f"Not a directory: {folder_path}", "files": [], "matches": []}

    wpilog_files = []
    for root, dirs, files in os.walk(folder_path):
        depth = root[len(folder_path):].count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        for f in sorted(files):
            if f.endswith('.wpilog'):
                wpilog_files.append(os.path.join(root, f))

    wpilog_files.sort(key=lambda p: os.path.getmtime(p))

    matches = []
    for filepath in wpilog_files:
        filename = os.path.basename(filepath)
        info = _parse_filename(filename, filepath)
        matches.append(info)

    latest_comp = next((m for m in reversed(matches) if m["is_competition"]), None)
    latest = matches[-1] if matches else None

    return {
        "folder": folder_path,
        "total_files": len(matches),
        "competition_files": sum(1 for m in matches if m["is_competition"]),
        "matches": matches,
        "latest": latest_comp or latest,
    }


def _parse_filename(filename: str, filepath: str) -> dict:
    """Extract match metadata from an akit_* filename."""
    parts = re.match(
        r'akit_(\d{2}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(.*?)\.wpilog', filename)

    if parts:
        date_str = parts.group(1)
        time_str = parts.group(2)
        suffix = parts.group(3).strip('_')
        suffix_parts = [p for p in suffix.split('_') if p]

        match_id = ""
        event = ""
        for p in suffix_parts:
            if re.match(r'^(q|p|e)\d+$', p, re.IGNORECASE):
                match_id = p.upper()
            elif p and not re.match(r'^p\d{4,}$', p, re.IGNORECASE):
                event = p.upper()

        label = f"{event} {match_id}".strip() if match_id else (event if event else "Practice")

        # Human-readable type name and dropdown display label
        _type_map = {"Q": "Qualification", "P": "Practice", "E": "Elimination"}
        type_code = match_id[0] if match_id else ""
        match_num  = match_id[1:] if match_id else ""
        type_name  = _type_map.get(type_code, "")
        display_label = (f"{event} - {type_name} {match_num}".strip()
                         if (event and type_name and match_num)
                         else label)

        return {
            "filepath": filepath,
            "filename": filename,
            "label": label,
            "display_label": display_label,   # e.g. "NJFLA - Qualification 2"
            "event": event,
            "match_id": match_id,
            "match_type_code": type_code,      # "Q", "P", "E"
            "match_type_name": type_name,
            "match_number": match_num,
            "date": f"20{date_str} {time_str.replace('-', ':')}",
            "size_mb": round(os.path.getsize(filepath) / 1024 / 1024, 1),
            "is_competition": bool(match_id),
            "mtime": os.path.getmtime(filepath),
        }
    else:
        return {
            "filepath": filepath,
            "filename": filename,
            "label": filename.replace('.wpilog', ''),
            "event": "",
            "match_id": "",
            "date": "",
            "size_mb": round(os.path.getsize(filepath) / 1024 / 1024, 1),
            "is_competition": False,
            "mtime": os.path.getmtime(filepath),
        }


# ---------------------------------------------------------------------------
# Competition trend analysis
# ---------------------------------------------------------------------------

def _match_sort_key(m: dict) -> tuple:
    """Sort key: Q < P < E, then by number."""
    mid = m.get("match_id", "")
    if mid.startswith("Q"):
        return (0, int(mid[1:]) if mid[1:].isdigit() else 0)
    elif mid.startswith("P"):
        return (1, int(mid[1:]) if mid[1:].isdigit() else 0)
    elif mid.startswith("E"):
        return (2, int(mid[1:]) if mid[1:].isdigit() else 0)
    return (9, 0)


def analyze_competition(folder_path: str, max_matches: int = 25) -> dict:
    """
    Analyze all competition matches in a folder for trend data.
    Returns lightweight per-match summaries (no timelines).
    """
    scan = scan_folder(folder_path)
    competition_matches = [m for m in scan["matches"] if m["is_competition"]]
    competition_matches.sort(key=_match_sort_key)

    # Deduplicate: keep the latest file per match_id (in case logs are in multiple subdirs)
    seen_ids: dict = {}
    for m in competition_matches:
        mid = m["match_id"]
        if mid not in seen_ids or m["mtime"] > seen_ids[mid]["mtime"]:
            seen_ids[mid] = m
    competition_matches = sorted(seen_ids.values(), key=_match_sort_key)[:max_matches]

    trend = []
    for m in competition_matches:
        try:
            data = analyze_match(m["filepath"])

            # Drive speed
            drive = data["subsystems"].get("Drive", {})
            max_speed = drive.get("max_speed_mps", 0)
            avg_speed = drive.get("mean_speed_mps", 0)

            # Temperatures — collect max across all subsystems
            temps = {}
            for sub_name, sub in data["subsystems"].items():
                t = sub.get("max_temp_c")
                if t and t > 0:
                    temps[sub_name] = t

            # Network issues: count disconnected cameras + dropped radio + NT clients offline
            network_issues = sum(1 for i in data["issues"]
                                 if any(k in i["message"].lower() for k in
                                        ["camera", "radio", "nt client", "vision", "limelight"]))

            trend.append({
                "label": m["match_id"],
                "file": m["filename"],
                "battery_min": data["battery"]["min"],
                "battery_mean": data["battery"]["mean"],
                "brownout_count": data["battery"]["brownout_count"],
                "issue_count": len(data["issues"]),
                "critical_count": sum(1 for i in data["issues"] if i["severity"] == "critical"),
                "warning_count": sum(1 for i in data["issues"] if i["severity"] == "warning"),
                "can_util_max": data["can_bus"]["max_utilization"],
                "can_errors": (data["can_bus"]["rx_error_count"] or 0) + (data["can_bus"]["off_count"] or 0),
                "loop_overruns": data["performance"]["overrun_count"],
                "cpu_temp_max": data["performance"]["cpu_temp_max"],
                "auto_s": data["timing"]["auto_duration_s"],
                "teleop_s": data["timing"]["teleop_duration_s"],
                "max_speed_mps": round(max_speed, 2),
                "avg_speed_mps": round(avg_speed, 2),
                "temps": temps,
                "network_issues": network_issues,
                "errors": data["alerts"]["errors"],
                "warnings": data["alerts"]["warnings"],
                "all_issues": [{"severity": i["severity"], "message": i["message"]}
                               for i in data["issues"]],
            })
        except Exception as exc:
            trend.append({
                "label": m["match_id"],
                "file": m["filename"],
                "parse_error": str(exc),
            })

    return {"matches": trend, "total_analyzed": len(trend)}
