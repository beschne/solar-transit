# Solar Transit Predictor

A command-line tool that tells you **which aircraft will transit in front of the sun and when** — so you can be ready at your telescope.

The script fetches live ADS-B position data, computes each aircraft's angular position as seen from your location, and predicts whether its flight path will intersect the solar disk within the next 60 seconds. Alerts fire via macOS notification and speech before the transit.

Designed for use with a **Seestar S30 Pro** (or any telescope tracking the sun) where the scope stays locked on the sun and aircraft fly through the frame automatically.

## Strategy

The script is the last step in a chain. Work through these prerequisites in order — if any one of them is missing, there is nothing to photograph.

1. **Clear sky.** Partial cloud cover may be enough if the solar disk is visible, but you need an unobstructed view at the moment of transit, which lasts less than two seconds.

2. **Sun in your field of view.** Your observing location must have a line of sight to the sun. If you are on a balcony or behind a window, confirm the sun is within your accessible azimuth range before setting up. The script shows `FOV ✗` in the header when the sun is outside your configured window.

3. **Telescope with a certified solar filter, aligned and tracking.** Point the scope at the sun, confirm the solar disk is sharp and centred, and let the tracking run. For a Seestar S30 Pro: engage solar mode and let the scope lock on and follow the sun. **Never point any optical instrument at the sun without a certified solar filter — permanent eye damage and sensor destruction will result.**

4. **Run the script.** Only now does it make sense to start `solartransit.py`. The scope is already on the sun; the script watches the sky for approaching aircraft and alerts you before they cross the disk. When the alarm fires, you have roughly 30 seconds to start recording before the transit begins.

### Capture tip — record video, extract frames later

A transit across the full solar disk lasts **less than two seconds**. Trying to time a single shot or even a timelapse is very likely to miss it. The reliable approach is:

1. When the `●IMMINENT` alert fires, **start a video recording** on your camera or telescope app.
2. Let it roll through the transit and a few seconds beyond.
3. Afterwards, extract individual frames with [ffmpeg](https://ffmpeg.org):

```bash
# Extract every frame as a PNG (e.g. from a 4K clip)
ffmpeg -i clip.mp4 -q:v 2 frames/frame_%04d.png

# Or extract only around the known transit time (e.g. seconds 14–17 of the clip)
ffmpeg -i clip.mp4 -ss 00:00:14 -t 3 -q:v 2 frames/frame_%04d.png
```

At 30 fps you get 30 frames per second of footage; at 60 fps you get 60 — enough to find the exact frame where the aircraft is centred on the solar disk.

---

## How it works

```
ADS-B APIs (adsb.lol / adsb.one)
        ↓  real-time aircraft positions (lat, lon, alt, track, speed)
  coordinate math
        ↓  aircraft → azimuth / elevation from observer (ECEF → ENU)
  skyfield + de421.bsp
        ↓  sun → azimuth / elevation + angular radius
  trajectory prediction
        ↓  extrapolate aircraft 60 s forward along track
  angular separation scan
        ↓  find closest approach to solar disk (parabolic refinement)
  alert when transit ≤ 30 s away
```

Refresh cycle: 5 s.

## Setup

**Requirements:** Python 3.11+, internet access.

```bash
git clone <this-repo>
cd solar-transit

# Create environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Configure your observer location (see section below)
cp observer.example.yaml observer.yaml
# → edit observer.yaml with your coordinates
```

## Observer configuration

Copy `observer.example.yaml` to `observer.yaml` and fill in your details. This file is gitignored and will never be committed.

```yaml
observer:
  latitude:  50.00000      # WGS84 decimal degrees — ~5 decimals = 1 m
  longitude:   8.00000
  altitude_m: 150.0        # height above mean sea level at telescope position
  geoid_offset_m: 47.0     # EGM96 geoid undulation for your area (Germany: ~44–50 m)
  label: "My Balcony"

  # Optional: restrict alerts to your telescope's field of view (azimuth in degrees)
  fov_min_az: 180
  fov_max_az: 270
```

### Getting accurate altitude

Altitude precision is critical. A **100 m error produces ~0.57° of angular error** for an aircraft at 10 km slant range — larger than the full solar disk (0.53°). A predicted transit would simply not appear.

- Use **[opentopodata.org](https://api.opentopodata.org)** or Google Earth to get your MSL elevation to ±1 m.
- Add the measured height of your balcony/rooftop above ground.
- The `geoid_offset_m` converts MSL to WGS84 ellipsoidal height. Look up your value at [geoid.bgi.obs-mip.fr](https://geoid.bgi.obs-mip.fr/).

### Finding your geoid offset (Germany)

| Region | Approx. offset |
|---|---|
| Hamburg | +38 m |
| Berlin | +37 m |
| Frankfurt / Taunus | +47 m |
| Munich | +48 m |
| Stuttgart | +48 m |

For exact values use the link above.

## Running

```bash
.venv/bin/python3 solartransit.py
```

First run downloads the JPL ephemeris file `de421.bsp` (~17 MB, cached next to the script). Stop with `Ctrl-C`.

## Terminal display

```
☉  SOLAR TRANSIT PREDICTOR — My Balcony        2026-06-23 15:42:07 CEST
   Sun: az 228.4°  el 38.1°  ⌀ 0.524°   FOV ✓   src ADSB.lol

  STATE      CALLSIGN   TYPE    ALT       CUR-SEP   MIN-SEP       IN
  ─────────────────────────────────────────────────────────────────────
  ●IMMINENT  DLH4AB     A320   10.8 km    0.41°     0.06°      12.4 s
  ○WATCH     RYR81QJ    B738    9.8 km    1.92°     0.71°      38.1 s
             EWG7KL     A319   10.1 km    6.30°       –           –

  3 displayed · 1 imminent · 1 watch  ·  refresh 5 s
```

- `●IMMINENT` (red): transit predicted within the alert window — macOS notification + speech fires
- `○WATCH` (yellow): aircraft approaching within 3° of the sun
- `FOV ✗`: sun is currently outside your configured balcony/window field of view; alerts suppressed
- **CUR-SEP**: angular separation right now
- **MIN-SEP**: predicted closest approach over the next 60 s
- **IN**: seconds until closest approach

## Configuration reference

All thresholds can be tuned in `observer.yaml`:

| Key | Default | Description |
|---|---|---|
| `window_seconds` | 60 | Prediction look-ahead (seconds) |
| `watch_deg` | 3.0 | Show aircraft in table when min-sep < this |
| `imminent_seconds` | 30 | Fire alert when transit is this many seconds away |
| `margin_deg` | 0.10 | Extra margin added to solar radius for transit threshold |
| `display_cutoff_deg` | 10.0 | Hide aircraft further than this from the sun |
| `qnh_hpa` | 1013.25 | Local QNH for barometric → geometric altitude correction |
| `radius_nm` | 50 | ADS-B search radius (nautical miles; 50 NM ≈ 92 km) |

## Data sources

Aircraft positions come from free, keyless ADS-B community feeds with automatic fallback:

1. **[adsb.lol](https://adsb.lol)** — ODbL licensed
2. **[adsb.one](https://adsb.one)** — community feed

Solar position uses the [Skyfield](https://rhodesmill.org/skyfield/) library with the JPL DE421 ephemeris, giving sub-arcminute accuracy.

## Accuracy notes

- **Solar disk**: ~0.524° diameter (varies ±1.7% over the year; computed from actual Earth–Sun distance).
- **Transit duration**: a plane at cruise altitude (10 km) and 50 km horizontal range crosses the full solar disk in about 1–2 s. Prediction accuracy needs to be better than that → observer altitude must be correct.
- **ADS-B latency**: positions are typically 5–15 s old. The trajectory extrapolation compensates for this.
- **Altitude correction**: ADS-B reports barometric altitude. The script applies a QNH correction (`(QNH − 1013.25) × 8 m/hPa`) and adds the geoid offset. Set the correct local QNH for best accuracy.
- **Vertical rate**: not modeled — altitude is held constant during extrapolation. For cruising aircraft this is accurate; for climbing/descending aircraft (near airports) min-sep predictions are less reliable.

## Dependencies

```
skyfield>=1.49   # solar position (JPL ephemeris)
pyyaml>=6.0      # config file parsing
```

Everything else uses the Python standard library.
