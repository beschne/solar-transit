# CLAUDE.md — Solar Transit Predictor

## Project purpose

CLI script that predicts when aircraft will transit in front of the solar disk, to help time astrophotography with a Seestar S30 Pro. The Seestar tracks the sun automatically; the photographer just needs to know **which planes** will cross the solar disk and **when**, so they can be ready at the eyepiece.

## Repository intent

This project will be published as a **public GitHub repo**. The file `observer.yaml` is gitignored and must never be committed — it contains the home location. The template `observer.example.yaml` is the committed substitute.

Claude may commit and push when asked. Do not add a `Co-Authored-By` trailer or any other Claude attribution to commit messages — commits appear solely under the repository owner's name.

## Related project

A private local ADS-B radar project — the ADS-B fetch pattern was reused verbatim here.

---

## Architecture

Single-file script (`solartransit.py`) with clearly separated sections. No web UI, no package structure. 

### File layout

```
solartransit.py          main script (public-safe, no coordinates)
observer.yaml            PRIVATE — real observer coordinates (gitignored)
observer.example.yaml    public template with fake coords and inline docs
requirements.txt         skyfield>=1.49, pyyaml>=6.0
.gitignore               observer.yaml, *.bsp, .venv/
de421.bsp                JPL ephemeris (downloaded by skyfield on first run, ~17 MB)
.venv/                   Python 3.11+ virtual environment
```

### Script sections (in order)

1. Docstring + imports
2. Bootstrap (`_HERE`, `_loader`, config paths, `REFRESH_S`)
3. Config loading — `load_config()` reads `observer.yaml`, fills defaults, computes `ellipsoidal_alt_m`
4. ADS-B — `_fetch_adsb_lol`, `_fetch_adsb_one`, `get_aircraft`
5. Coordinate math — geodetic→ECEF→ENU→az/el, baro→geometric altitude
6. Solar position — skyfield wrapper (`init_skyfield`, `sun_azel`)
7. Angular geometry — `ang_sep`, `_dest_point` (great-circle propagation)
8. Transit prediction — `predict_transit` with parabolic refinement
9. State machine — `classify` → IMMINENT / WATCH / SKIP
10. Alerts — macOS `osascript` notification + `say` speech
11. Terminal rendering — ANSI full-screen redraw every 5 s
12. Main loop

---

## Config file (`observer.yaml`)

```yaml
observer:
  latitude:  48.00000        # WGS84 decimal degrees
  longitude:   8.00000
  altitude_m: 150.0          # MSL height at telescope position
  geoid_offset_m: 47.0       # EGM96 geoid undulation for the area
  label: "My Location"
  fov_min_az: 180            # accessible azimuth range
  fov_max_az: 270

prediction:
  window_seconds:     60     # how far ahead to extrapolate
  step_seconds:       0.5    # interpolation step
  margin_deg:         0.10   # extra margin beyond solar radius
  watch_deg:          3.0    # show in table below this separation
  imminent_seconds:   30     # alert threshold (seconds to transit)
  display_cutoff_deg: 10.0   # hide aircraft beyond this from sun
  qnh_hpa:            1013.25
  radius_nm:          50     # ADS-B search radius
```

### Why altitude precision matters

A 100 m error in observer altitude produces ~0.57° of angular error for an aircraft at 10 km slant range — **larger than the full solar disk (0.53°)**. A predicted transit would simply not show up. The `altitude_m` value should be accurate to ±10 m or better (Google Earth / OpenTopoData, plus measured floor height above ground). The `geoid_offset_m` converts MSL to WGS84 ellipsoidal height; typical values are 35–55 m in central Europe.

---

## Key algorithms

### Solar position

`skyfield` + `de421.bsp` JPL ephemeris. Called as:

```python
app = site.at(t).observe(sun_body).apparent()
alt, az, dist = app.altaz()
r_sun_deg = degrees(asin(695_700 / dist.km))   # actual angular radius, varies ±1.7%/year
```

Solar angular diameter today: ~0.524°. Both sun and aircraft treated geometrically (no differential refraction); at transit geometry both are at the same elevation angle so differential error is negligible (<0.1″).

### Aircraft angular position

Four-step chain, all stdlib math:

1. **Baro → geometric MSL**: `h_msl = alt_baro_m + (QNH − 1013.25) × 8 m/hPa`
2. **MSL → ellipsoidal**: `h_ellip = h_msl + geoid_offset_m`
3. **Geodetic → ECEF** (WGS84): `N = a/√(1−e²sin²φ)`, standard formula
4. **ECEF diff → ENU → az/el**: `az = atan2(E, N) mod 360`, `el = atan2(U, √(E²+N²))`

### Transit prediction

Per aircraft, per 5 s cycle:

1. Propagate lat/lon forward in `step_seconds` increments along `track`/`gs_kt` using the great-circle destination-point formula (`_dest_point`).
2. Hold altitude constant (vertical rate not reliably present in ADS-B).
3. At each step compute aircraft az/el and sun az/el (linearly interpolated between t=0 and t=window — sun moves only ~0.24° in 60 s, linear error <1″).
4. Compute spherical angular separation at each step.
5. **Parabolic refinement**: fit a parabola to the three points bracketing the minimum to obtain sub-step-size `tau_min` and `sep_min`.

Transit is declared when `sep_min ≤ r_sun + margin_deg`.

### Angular separation formula

```python
cos_sep = sin(el1)*sin(el2) + cos(el1)*cos(el2)*cos(az1−az2)
sep = acos(clamp(cos_sep, −1, 1))
```

---

## State machine and alerts

| State | Condition | Terminal | Alert |
|---|---|---|---|
| hidden | `eff_sep > display_cutoff_deg` (10°) | not shown | – |
| **WATCH** | `sep_min ≤ watch_deg` (3°) | yellow `○WATCH` | – |
| **IMMINENT** | `sep_min ≤ r_sun + margin` **and** `tau_min ≤ 30 s` | red `●IMMINENT` | macOS notification + `say` |

Alerts fire once per approach pass. The debounce set `_alerted` is cleared when the aircraft moves back to SKIP state.

Alerts only fire when the **sun is within the balcony FOV** (`fov_min_az`–`fov_max_az`). When the sun is outside the FOV the header shows `FOV ✗` and alerts are suppressed.

---

## ADS-B data sources

Keyless, no API key required. Tried in order, first success wins:

1. **adsb.lol** — ODbL licensed, ADSBExchange v2 format
2. **adsb.one** — community feed, same format

Aircraft record schema: `{ hex, callsign, reg, type, lat, lon, alt_ft (barometric), track (°), gs_kt }`.

Search radius: 50 NM (≈92 km). For sun elevations above ~6° this captures all geometrically possible transiting aircraft.

---

## Observer setup

Observer coordinates (latitude, longitude, altitude, geoid offset, FOV limits) are stored exclusively in `observer.yaml`, which is gitignored and never committed. See `observer.example.yaml` for the full schema and inline documentation.

---

## Running the script

```bash
cd "2026-06-23 Solar Transit"
.venv/bin/python3 solartransit.py
# Stop with Ctrl-C
```

First run downloads `de421.bsp` (~17 MB) automatically.

## Dependencies

```
skyfield>=1.49    # solar position via JPL ephemeris
pyyaml>=6.0       # config file parsing
```

Python 3.11+ required.

---

## Out of scope

- Web UI or map (flightradar.py already covers situational awareness)
- Telescope/mount control of any kind — the Seestar S30 Pro tracks the sun automatically
- Camera / capture triggering
- Historical logging or statistics
- Vertical rate modeling in trajectory prediction (cruise altitude held constant)
- Windows/Linux alert backends (macOS `osascript`/`say` only)
- Multi-observer or network mode
