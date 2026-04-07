# FRC Robot Dashboard

A local web dashboard for analyzing WPILib robot logs (`.wpilog`) from FRC competitions. Built for **Team 555**, it provides per-match diagnostics, competition-wide trend tracking, and Phoenix6 CAN device support via companion `.hoot` files.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Pages](#pages)
  - [Match](#match-page)
  - [Trends](#trends-page)
  - [Settings](#settings-page)
- [Log File Format](#log-file-format)
- [Subsystems Tracked](#subsystems-tracked)
- [Issue Detection](#issue-detection)
- [Phoenix6 Support](#phoenix6-support)
- [API Reference](#api-reference)
- [Settings File](#settings-file)

---

## Getting Started

### Requirements

- Python 3.9+
- Flask (`pip install -r requirements.txt`)
- A modern web browser

### Running

```bash
python3 app.py
```

The dashboard will be available at **http://localhost:5001** (port 5000 is reserved by macOS AirPlay).

**Optional arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `--port` | `5000` | Port to listen on |
| `--host` | `0.0.0.0` | Bind address |
| `--folder` | App directory | Default log folder on startup |

**Example:**
```bash
python3 app.py --port 5001 --folder ~/logs/competition
```

### Selecting a Log Folder

Click **Browse…** in the top bar to open a native macOS folder picker. The folder path is saved per-team in the settings file and restored automatically on subsequent loads.

You can also type a path directly into the folder input and click **↺** to re-scan.

---

## Pages

### Match Page

Analyzes a single `.wpilog` file in detail.

**Top bar controls:**
- **Browse…** — opens a native folder picker
- **↺** — re-scans the current folder
- **Match dropdown** — select any competition match from the folder
- **Latest** — loads the most recently modified match
- **Match Period toggle** — switches charts between the full log duration and the enabled match period only

**Left column:**
- Match info strip (event, match type, FMS status, timing)
- Stat cards — Battery, CAN Bus, Loop/CPU, Radio
- Phoenix6 devices (loaded from companion `.hoot` files)
- CAN devices
- Network clients (Limelights, dashboards, etc.)

**Right column:**
- Issues list — all detected problems with severity badges
- Battery Voltage chart
- CAN Bus Utilization chart
- Loop Cycle Time chart

---

### Trends Page

Analyzes all competition matches in the selected folder and displays cross-match trends.

**Summary strip:** total matches, average battery minimum, brownout count, max speed, critical issues, average CAN utilization.

**Charts:**
1. Battery Min per Match
2. Drive Speed (m/s) — max and average
3. Max Temperature by subsystem
4. Issues per Match — warnings and criticals stacked

**Match table:** per-match breakdown with battery, speed, CAN%, overruns, temperatures, network issues, and issue counts. Click any row to jump to that match.

> Practice matches can be hidden from all trend charts via the **Settings → Trends Charts** toggle.

---

### Settings Page

**Phoenix6 Fault Mode**
Controls which fault data is shown on Phoenix6 device cards.
- **Faults** — active faults (reset each match)
- **Sticky Faults** — historical faults that persist until manually cleared

**Trends Charts**
- **Show All** — include Practice matches in trend charts
- **Hide Practice** — filter out Practice matches (show Qualification and Elimination only)

**Subsystem Manager**
Organizes devices into named subsystems that appear on the Match page.

- Create subsystems with a name and icon
- Drag devices from the pool on the right into a subsystem slot
- Rename individual devices with the pencil icon
- Delete subsystems as needed

Device types:
- **Phoenix6** — TalonFX, CANcoder, Pigeon 2, etc.
- **CAN** — Power Distribution Hub, generic CAN devices
- **NT** — NetworkTables clients (Limelights, dashboards)

All settings are saved automatically and persisted per team number.

---

## Log File Format

The dashboard reads `.wpilog` files produced by **AdvantageKit**.

### Filename Convention

```
akit_YY-MM-DD_HH-MM-SS_EVENT_MATCHID.wpilog
```

| Segment | Example | Description |
|---------|---------|-------------|
| `YY-MM-DD` | `24-04-07` | Date |
| `HH-MM-SS` | `15-30-45` | Time |
| `EVENT` | `NJFLA` | Event code (optional) |
| `MATCHID` | `Q2`, `P1`, `E3` | Match type + number |

**Match type codes:**

| Code | Type |
|------|------|
| `Q` | Qualification |
| `P` | Practice |
| `E` | Elimination |

### Key Signal Paths

| Path | Description |
|------|-------------|
| `/SystemStats/BatteryVoltage` | Battery voltage |
| `/SystemStats/CANBus/*` | CAN utilization and error counts |
| `/SystemStats/CPUTempCelsius` | roboRIO CPU temperature |
| `/DriverStation/*` | Match info, enable state, alliance |
| `/RealOutputs/LoggedRobot/*` | Loop cycle times |
| `/RealOutputs/Drive/Speed` | Drive speed (m/s) |
| `/Flywheel/*` | Flywheel subsystem data |
| `/Hood/*` | Hood subsystem data |
| `/Turret/*` | Turret subsystem data |
| `/Vision/*` | Camera connection status |
| `/PowerDistribution/*` | PDH voltage, current, faults |
| `/RadioStatus/*` | Radio connection status |
| `/RealOutputs/Alerts/*` | Robot code errors and warnings |

---

## Subsystems Tracked

### Drive
| Field | Description |
|-------|-------------|
| `max_speed_mps` | Peak speed (m/s) |
| `mean_speed_mps` | Average speed (m/s) |
| `field_relative` | Field-relative mode active |

### Flywheel
| Field | Description |
|-------|-------------|
| `max_velocity_rps` | Peak velocity (RPS) |
| `mean_velocity_rps` | Average velocity (RPS) |
| `max_temp_c` | Peak temperature (°C) |
| `max_current_a` | Peak current draw (A) |
| `left_connected` | Left motor connection status |
| `right_connected` | Right motor connection status |
| `reached_setpoint` | Whether setpoint was reached |

### Hood
| Field | Description |
|-------|-------------|
| `max_angle_deg` | Peak angle (degrees) |
| `max_temp_c` | Peak temperature (°C) |
| `max_current_a` | Peak current draw (A) |
| `connected` | Motor connection status |

### Turret
| Field | Description |
|-------|-------------|
| `max_temp_c` | Peak temperature (°C) |
| `max_current_a` | Peak current draw (A) |
| `connected` | Motor connection status |

### Vision
- `Camera0`, `Camera1` — connection status per camera

### Power Distribution (PDH/PDP)
- Voltage, total current, temperature, active faults, sticky faults

### Network Clients
- Connection status for each NT client (Limelights, dashboards, etc.)

---

## Issue Detection

Issues are detected automatically and displayed with severity badges.

| Severity | Color | Meaning |
|----------|-------|---------|
| **Critical** | Red | Significant problem that affected match performance |
| **Warning** | Yellow | Potential concern worth investigating |

### Thresholds

| Issue | Severity | Threshold |
|-------|----------|-----------|
| Brownout | Critical | Voltage < 6.3 V |
| Battery critical | Critical | Min < 6.5 V |
| Battery low | Warning | Min < 8.0 V |
| CAN overloaded | Critical | Utilization > 80% |
| CAN high | Warning | Utilization > 65% |
| CAN errors | Warning | Any RX errors or off-bus events |
| Loop severe overruns | Critical | > 5 cycles > 100 ms |
| Loop overruns | Warning | > 15 cycles > 25 ms |
| Subsystem motor disconnected | Critical | Motor not detected |
| Subsystem overheating | Warning | > 70°C |
| Vision camera disconnected | Warning | Camera not detected |
| Radio disconnected | Critical | Disconnected at end of match |
| NT client disconnected | Warning | Client dropped during match |
| Robot code errors | Error | From `/RealOutputs/Alerts/errors` |

---

## Phoenix6 Support

The dashboard loads companion `.hoot` files (CTRE Phoenix 6 device logs) to display per-device telemetry.

### File Discovery

`.hoot` files are matched to a `.wpilog` by looking in:
1. A subdirectory named `{EVENT}_{MATCHID}/` adjacent to the `.wpilog`
2. Flat `.hoot` files in the same directory matching `*_{MATCHID}*.hoot`

### Data Extracted Per Device

| Field | Description |
|-------|-------------|
| `max_temp_c` | Peak motor temperature (°C) |
| `max_stator_a` | Peak stator current (A) |
| `max_supply_a` | Peak supply current (A) |
| `faults` | List of active faults |
| `sticky_faults` | List of persistent faults |
| `fault_times` | Timestamps of each fault onset |
| `magnet_health` | CANcoder magnet quality |
| `bus_label` | RIO CAN bus or CANivore name |

> Current-limiting faults (`SupplyCurrLimit`, `StatorCurrLimit`) are filtered out as normal behavior and do not trigger warnings.

### Fault Display Modes

Controlled in **Settings → Phoenix6 Fault Mode:**
- **Faults** — shows faults active right now
- **Sticky Faults** — shows all faults that occurred during the match

---

## API Reference

All endpoints are served by the local Flask server.

| Endpoint | Method | Params | Description |
|----------|--------|--------|-------------|
| `/` | GET | — | Serves the dashboard |
| `/api/scan` | GET | `folder` | Scans a folder for `.wpilog` files |
| `/api/match` | GET | `file` | Fully analyzes a single match file |
| `/api/competition` | GET | `folder` | Trend summary across all competition matches |
| `/api/hoot_devices` | GET | `file` | Phoenix6 device data from `.hoot` files |
| `/api/settings` | GET | `team` | Load team settings |
| `/api/settings` | POST | JSON body | Save team settings |
| `/api/pick_folder` | GET | — | Opens native macOS folder picker |
| `/api/health` | GET | — | Server health check |

**Cache TTLs:**
- Match analysis: 5 minutes
- Competition trends: 1 minute
- Hoot devices: 10 minutes

---

## Settings File

Settings are saved automatically as `team{NUMBER}_settings.json` in the app directory.

```json
{
  "team": 555,
  "folder": "/path/to/logs",
  "fault_mode": "faults",
  "hide_practice_trends": false,
  "subsystems": [
    {
      "id": "smniwcanrcfn",
      "name": "Drive System",
      "icon": "bi-arrows-move",
      "devices": [
        {
          "key": "phoenix6::TalonFX-1",
          "kind": "phoenix6",
          "label": "SW-FL-Drive-1"
        },
        {
          "key": "can::Power Distribution Hub",
          "kind": "can"
        },
        {
          "key": "nt::limelight-one@1",
          "kind": "nt"
        }
      ]
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `team` | integer | Team number |
| `folder` | string | Last used log folder (restored on load) |
| `fault_mode` | `"faults"` \| `"sticky"` | Phoenix6 fault display mode |
| `hide_practice_trends` | boolean | Hide Practice matches from trend charts |
| `subsystems` | array | Subsystem definitions with assigned devices |

---

*Built for FRC Team 555*
