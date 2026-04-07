"""
Phoenix6 device data extractor from converted .hoot → .wpilog files.

Uses selective single-pass parsing with mmap: only decodes Phoenix6/* signals
we care about (temperatures, currents, voltages, fault flags).
Memory-efficient: mmap avoids loading the full ~1GB wpilog into RAM.
"""
import mmap
import os
import re
import statistics
import struct

from hoot_converter import convert_hoot_to_wpilog

# ── Signals to extract ────────────────────────────────────────────────────────
# All Fault_* and StickyFault_* are also included via _is_wanted()
WANTED_SIGNALS = {
    'DeviceTemp', 'ProcessorTemp', 'AncillaryDeviceTemp',
    'StatorCurrent', 'SupplyCurrent', 'TorqueCurrent',
    'SupplyVoltage', 'MotorVoltage', 'DutyCycle',
    'FaultField', 'StickyFaultField',
    'MagnetHealth', 'AbsolutePosition',
    'ControlMode', 'Velocity', 'RobotEnable',
}

TEMP_WARN_C     = 70.0
TEMP_CRITICAL_C = 85.0
STATOR_WARN_A   = 80.0

# Faults that are normal current-limiting behavior, not hardware problems.
# Show them on the card but don't set has_fault or generate issues.
CURRENT_LIMIT_FAULTS = {'SupplyCurrLimit', 'StatorCurrLimit'}


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_hoot_file(hoot_path: str, wpilog_cache_dir: str = None) -> dict:
    """
    Convert a .hoot file and extract Phoenix6 device data.

    Args:
        hoot_path:       Path to the .hoot file.
        wpilog_cache_dir: Directory to cache converted .wpilog files.
                          Defaults to same directory as the hoot file.

    Returns:
        {
          "devices": [...],  # list of device summary dicts
          "issues":  [...],  # list of {severity, device, message}
          "hoot_path": str,
          "wpilog_path": str,
        }
    """
    # Determine a stable output path (avoid re-converting if already done)
    base = os.path.splitext(os.path.basename(hoot_path))[0]
    cache_dir = wpilog_cache_dir or os.path.dirname(hoot_path)
    os.makedirs(cache_dir, exist_ok=True)
    wpilog_path = os.path.join(cache_dir, base + "_converted.wpilog")

    # Convert if not already present or if hoot is newer
    if (not os.path.exists(wpilog_path) or
            os.path.getmtime(hoot_path) > os.path.getmtime(wpilog_path)):
        wpilog_path = convert_hoot_to_wpilog(hoot_path, wpilog_path)

    result = _parse_phoenix6_from_wpilog(wpilog_path)
    result["hoot_path"]   = hoot_path
    result["wpilog_path"] = wpilog_path
    return result


# ── Selective wpilog parser ───────────────────────────────────────────────────

def _is_wanted(signal: str) -> bool:
    return (signal in WANTED_SIGNALS or
            signal.startswith('Fault_') or
            signal.startswith('StickyFault_'))


def _parse_phoenix6_from_wpilog(wpilog_path: str) -> dict:
    """
    Single-pass selective parser using mmap for memory efficiency.
    Builds per-field time-series only for Phoenix6 signals we care about.
    """
    file_size = os.path.getsize(wpilog_path)
    if file_size < 12:
        return {"devices": [], "issues": []}

    with open(wpilog_path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            return _parse_mmap(mm)
        finally:
            mm.close()


def _parse_mmap(mm) -> dict:
    size = len(mm)
    if mm[:6] != b'WPILOG':
        return {"devices": [], "issues": []}

    extra_len = struct.unpack_from('<I', mm, 8)[0]
    offset = 12 + extra_len

    entries = {}   # entry_id -> (name, dtype)
    wanted  = {}   # entry_id -> (name, dtype)  ← subset we decode
    records = {}   # field_name -> [(ts_sec, val)]

    while offset < size - 2:
        if offset >= size:
            break
        h = mm[offset]
        offset += 1

        id_size         = (h & 0x3) + 1
        payload_size_sz = ((h >> 2) & 0x3) + 1
        ts_size         = ((h >> 4) & 0x7) + 1
        needed = id_size + payload_size_sz + ts_size
        if offset + needed > size:
            break

        entry_id    = int.from_bytes(mm[offset:offset + id_size],          'little')
        offset += id_size
        payload_len = int.from_bytes(mm[offset:offset + payload_size_sz],  'little')
        offset += payload_size_sz
        timestamp   = int.from_bytes(mm[offset:offset + ts_size],          'little') / 1_000_000.0
        offset += ts_size

        if offset + payload_len > size:
            break

        if entry_id == 0:
            # Control record — look for START (type byte 0x00)
            if payload_len > 0 and mm[offset] == 0 and payload_len >= 13:
                try:
                    payload = mm[offset:offset + payload_len]
                    p = 1
                    new_id   = struct.unpack_from('<I', payload, p)[0]; p += 4
                    name_len = struct.unpack_from('<I', payload, p)[0]; p += 4
                    if p + name_len <= payload_len:
                        name = payload[p:p + name_len].decode('utf-8', errors='replace'); p += name_len
                        if p + 4 <= payload_len:
                            type_len = struct.unpack_from('<I', payload, p)[0]; p += 4
                            if p + type_len <= payload_len:
                                dtype = payload[p:p + type_len].decode('utf-8', errors='replace')
                                entries[new_id] = (name, dtype)
                                m = re.match(r'Phoenix6/[A-Za-z]+-\d+/(.+)', name)
                                if m and _is_wanted(m.group(1)):
                                    wanted[new_id] = (name, dtype)
                                    records[name] = []
                except Exception:
                    pass

        elif entry_id in wanted:
            name, dtype = wanted[entry_id]
            payload = mm[offset:offset + payload_len]
            val = None
            if dtype == 'double' and payload_len >= 8:
                val = struct.unpack_from('<d', payload)[0]
            elif dtype == 'string':
                val = payload.decode('utf-8', errors='replace')
            if val is not None:
                records[name].append((timestamp, val))

        offset += payload_len

    return _summarize_devices(records)


# ── Device aggregation ────────────────────────────────────────────────────────

def _summarize_devices(records: dict) -> dict:
    """Aggregate per-device statistics from raw signal series."""
    raw = {}  # "TalonFX-20" -> {"type": ..., "id": ..., "signals": {sig: [(ts,v)]}}

    for name, series in records.items():
        m = re.match(r'Phoenix6/([A-Za-z]+)-(\d+)/(.+)', name)
        if not m:
            continue
        dev_type, dev_id_str, signal = m.group(1), m.group(2), m.group(3)
        key = f"{dev_type}-{dev_id_str}"
        if key not in raw:
            raw[key] = {"type": dev_type, "id": int(dev_id_str), "signals": {}}
        raw[key]["signals"][signal] = series

    devices = []
    issues  = []

    for key, info in sorted(raw.items(), key=lambda x: (x[1]["type"], x[1]["id"])):
        dev_type = info["type"]
        dev_id   = info["id"]
        sigs     = info["signals"]

        def vals(sig):
            return [v for _, v in sigs.get(sig, []) if isinstance(v, (int, float))]

        dev = {
            "name":          key,
            "device_type":   dev_type,
            "device_id":     dev_id,
            "connected":     True,
            "has_fault":     False,
            "faults":        [],
            "sticky_faults": [],
        }

        # ── Temperature ──────────────────────────────────────────────────────
        temps = vals('DeviceTemp')
        if temps:
            dev["max_temp_c"]  = round(max(temps), 1)
            dev["mean_temp_c"] = round(statistics.mean(temps), 1)

        proc_temps = vals('ProcessorTemp')
        if proc_temps:
            dev["max_proc_temp_c"] = round(max(proc_temps), 1)

        # ── Currents (TalonFX) ───────────────────────────────────────────────
        stator = vals('StatorCurrent')
        if stator:
            dev["max_stator_a"]  = round(max(stator), 1)
            dev["mean_stator_a"] = round(statistics.mean(stator), 1)

        supply_cur = vals('SupplyCurrent')
        if supply_cur:
            dev["max_supply_a"] = round(max(supply_cur), 1)

        torque_cur = vals('TorqueCurrent')
        if torque_cur:
            dev["max_torque_a"] = round(max(torque_cur), 1)

        # ── Voltage ──────────────────────────────────────────────────────────
        supply_v = vals('SupplyVoltage')
        if supply_v:
            dev["min_supply_v"] = round(min(supply_v), 2)
            dev["mean_supply_v"] = round(statistics.mean(supply_v), 2)

        # ── Duty cycle ───────────────────────────────────────────────────────
        duty = vals('DutyCycle')
        if duty:
            dev["max_duty_cycle"] = round(max(abs(v) for v in duty), 3)

        # ── Fault flags ──────────────────────────────────────────────────────
        # current_limit_faults: current-limiting is a normal protection feature,
        # so record them separately and don't set has_fault for them alone.
        current_limit_active = []
        fault_onset_times   = {}   # {fault_name: [t1, t2, ...]}  — 0→1 transitions
        sticky_onset_times  = {}   # same for sticky faults

        def _onsets(series_data):
            """Return timestamps of every 0→1 transition (fault becoming active)."""
            times, prev = [], 0.0
            for t, v in series_data:
                if v > 0.5 and prev <= 0.5:
                    times.append(round(t, 2))
                prev = v
            return times

        for sig, series_data in sigs.items():
            if sig.startswith('Fault_') and sig != 'FaultField':
                fault_name = sig[6:]   # strip "Fault_"
                if any(v > 0.5 for _, v in series_data):
                    dev["faults"].append(fault_name)
                    onsets = _onsets(series_data)
                    if onsets:
                        fault_onset_times[fault_name] = onsets
                    if fault_name in CURRENT_LIMIT_FAULTS:
                        current_limit_active.append(fault_name)
                    else:
                        dev["has_fault"] = True
            elif sig.startswith('StickyFault_'):
                fault_name = sig[12:]  # strip "StickyFault_"
                if any(v > 0.5 for _, v in series_data):
                    dev["sticky_faults"].append(fault_name)
                    onsets = _onsets(series_data)
                    if onsets:
                        sticky_onset_times[fault_name] = onsets
                    if fault_name not in CURRENT_LIMIT_FAULTS:
                        dev["has_fault"] = True

        dev["fault_times"]        = fault_onset_times
        dev["sticky_fault_times"] = sticky_onset_times
        dev["current_limited"] = bool(current_limit_active)

        # ── CANcoder magnet health ────────────────────────────────────────────
        if dev_type == 'CANcoder':
            health_series = sigs.get('MagnetHealth', [])
            if health_series:
                last_health = health_series[-1][1]
                dev["magnet_health"] = last_health
                if last_health and "Green" not in last_health:
                    dev["has_fault"] = True

        # ── Per-device issues ─────────────────────────────────────────────────
        if dev_type == 'TalonFX':
            max_t = dev.get("max_temp_c")
            if max_t is not None and max_t >= TEMP_CRITICAL_C:
                issues.append({
                    "severity": "critical", "device": key,
                    "message":  f"{key} overheating: {max_t}°C",
                })
            elif max_t is not None and max_t >= TEMP_WARN_C:
                issues.append({
                    "severity": "warning", "device": key,
                    "message":  f"{key} running hot: {max_t}°C",
                })

            # Emit one issue per fault so each gets its own timestamp list
            real_faults = [f for f in dev["faults"] if f not in CURRENT_LIMIT_FAULTS]
            for f in real_faults:
                issues.append({
                    "severity": "warning", "device": key,
                    "message":  f"{key}: {f}",
                    "times":    fault_onset_times.get(f, []),
                })

        elif dev_type == 'CANcoder':
            health = dev.get("magnet_health", "")
            if health and "Green" not in health:
                issues.append({
                    "severity": "warning", "device": key,
                    "message":  f"{key} magnet health: {health}",
                })

        devices.append(dev)

    return {"devices": devices, "issues": issues}
