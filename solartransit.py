#!/usr/bin/env python3
"""
solartransit.py — Know when aircraft will transit in front of the sun.

Fetches real-time ADS-B positions, computes each aircraft's angular
position as seen from the observer, and predicts whether its flight
path will intersect the solar disk — and when.

Start:  python3 solartransit.py
Stop:   Ctrl-C

Setup:  cp observer.example.yaml observer.yaml  # fill in your coords
        pip install -r requirements.txt
"""

import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from skyfield.api import Loader, wgs84

# ── Bootstrap ─────────────────────────────────────────────────────────────────

_HERE   = Path(__file__).parent
_loader = Loader(str(_HERE))    # skyfield stores de421.bsp here

CONFIG_FILE  = _HERE / "observer.yaml"
EXAMPLE_FILE = _HERE / "observer.example.yaml"
REFRESH_S    = 5
USER_AGENT   = "solartransit/1.0 (personal hobby use)"

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(
            f"\nERROR: {CONFIG_FILE} not found.\n"
            f"Copy {EXAMPLE_FILE.name} → observer.yaml and fill in your coordinates.\n"
        )
    with CONFIG_FILE.open() as fh:
        cfg = yaml.safe_load(fh)

    obs = cfg.get("observer") or {}
    for key in ("latitude", "longitude", "altitude_m", "geoid_offset_m"):
        if key not in obs:
            sys.exit(f"ERROR: observer.yaml missing key: observer.{key}")

    obs["ellipsoidal_alt_m"] = obs["altitude_m"] + obs["geoid_offset_m"]

    p = cfg.setdefault("prediction", {})
    p.setdefault("window_seconds",     60)
    p.setdefault("step_seconds",       0.5)
    p.setdefault("margin_deg",         0.10)
    p.setdefault("watch_deg",          3.0)
    p.setdefault("imminent_seconds",   30)
    p.setdefault("display_cutoff_deg", 10.0)
    p.setdefault("qnh_hpa",            1013.25)
    p.setdefault("radius_nm",          50)

    return cfg

# ── ADS-B ─────────────────────────────────────────────────────────────────────

def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def _normalize_adsbx(ac: dict) -> dict:
    alt = ac.get("alt_baro")
    if alt == "ground":
        alt = 0
    return {
        "hex":      ac.get("hex"),
        "callsign": (ac.get("flight") or "").strip() or None,
        "reg":      ac.get("r"),
        "type":     ac.get("t"),
        "lat":      ac.get("lat"),
        "lon":      ac.get("lon"),
        "alt_ft":   alt,
        "track":    ac.get("track"),
        "gs_kt":    ac.get("gs"),
    }

def _fetch_adsb_lol(lat, lon, radius_nm):
    url  = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}"
    return [_normalize_adsbx(a) for a in _http_get_json(url).get("ac", [])]

def _fetch_adsb_one(lat, lon, radius_nm):
    url  = f"https://api.adsb.one/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}"
    return [_normalize_adsbx(a) for a in _http_get_json(url).get("ac", [])]

_SOURCES = [("ADSB.lol", _fetch_adsb_lol), ("ADSB.one", _fetch_adsb_one)]

def get_aircraft(lat: float, lon: float, radius_nm: float) -> dict:
    last_err = None
    for name, fn in _SOURCES:
        try:
            planes = fn(lat, lon, radius_nm)
            planes = [p for p in planes if p["lat"] is not None and p["lon"] is not None]
            return {"source": name, "aircraft": planes}
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            last_err = f"{name}: {e}"
    return {"source": None, "aircraft": [], "error": last_err or "no source reachable"}

# ── Coordinate math ───────────────────────────────────────────────────────────

def _geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple:
    a  = 6_378_137.0
    e2 = 6.694379990141316e-3    # WGS84 first eccentricity squared
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    N = a / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    x = (N + alt_m) * math.cos(lat) * math.cos(lon)
    y = (N + alt_m) * math.cos(lat) * math.sin(lon)
    z = (N * (1.0 - e2) + alt_m) * math.sin(lat)
    return x, y, z

def _ecef_diff_to_enu(dx, dy, dz, obs_lat_deg, obs_lon_deg) -> tuple:
    lat = math.radians(obs_lat_deg)
    lon = math.radians(obs_lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    sm, cm = math.sin(lon), math.cos(lon)
    e =  -sm*dx  +  cm*dy
    n =  -sl*cm*dx  - sl*sm*dy  + cl*dz
    u =   cl*cm*dx  + cl*sm*dy  + sl*dz
    return e, n, u

def _enu_to_azel(e, n, u) -> tuple:
    az    = math.degrees(math.atan2(e, n)) % 360.0
    el    = math.degrees(math.atan2(u, math.hypot(e, n)))
    slant = math.sqrt(e*e + n*n + u*u)
    return az, el, slant

def aircraft_azel(ac_lat, ac_lon, ac_alt_ft,
                  obs_lat, obs_lon, obs_ellip_m,
                  geoid_m, qnh_hpa) -> tuple | None:
    """Return (az°, el°, slant_km) for an aircraft as seen from the observer."""
    if ac_lat is None or ac_lon is None or not isinstance(ac_alt_ft, (int, float)):
        return None
    # Barometric → geometric MSL (ISA lapse-rate correction, ~8 m/hPa near sea level)
    h_msl   = ac_alt_ft * 0.3048 + (qnh_hpa - 1013.25) * 8.0
    h_ellip = h_msl + geoid_m

    obs_xyz = _geodetic_to_ecef(obs_lat, obs_lon, obs_ellip_m)
    ac_xyz  = _geodetic_to_ecef(ac_lat,  ac_lon,  h_ellip)
    dx, dy, dz = ac_xyz[0]-obs_xyz[0], ac_xyz[1]-obs_xyz[1], ac_xyz[2]-obs_xyz[2]
    e, n, u = _ecef_diff_to_enu(dx, dy, dz, obs_lat, obs_lon)
    az, el, slant = _enu_to_azel(e, n, u)
    return az, el, slant / 1000.0   # slant in km

# ── Solar position ────────────────────────────────────────────────────────────

def init_skyfield(obs_lat, obs_lon, obs_ellip_m):
    ts   = _loader.timescale()
    eph  = _loader("de421.bsp")
    body = eph["sun"]
    site = eph["earth"] + wgs84.latlon(obs_lat, obs_lon, elevation_m=obs_ellip_m)
    return ts, site, body

def sun_azel(site, body, t) -> tuple:
    """Return (az°, el°, angular_radius°) for the sun at skyfield time t."""
    app            = site.at(t).observe(body).apparent()
    alt, az, dist  = app.altaz()
    r_deg          = math.degrees(math.asin(695_700.0 / dist.km))
    return az.degrees, alt.degrees, r_deg

# ── Angular geometry ──────────────────────────────────────────────────────────

def ang_sep(az1, el1, az2, el2) -> float:
    """Spherical angular separation between two sky directions (degrees)."""
    a1, e1 = math.radians(az1), math.radians(el1)
    a2, e2 = math.radians(az2), math.radians(el2)
    c = (math.sin(e1)*math.sin(e2) + math.cos(e1)*math.cos(e2)*math.cos(a1-a2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))

def _dest_point(lat_deg, lon_deg, bearing_deg, dist_m) -> tuple:
    """Advance a position along a great circle (spherical-Earth approximation)."""
    R   = 6_371_000.0
    ang = dist_m / R
    b   = math.radians(bearing_deg)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    lat2 = math.asin(math.sin(lat)*math.cos(ang)
                     + math.cos(lat)*math.sin(ang)*math.cos(b))
    lon2 = lon + math.atan2(math.sin(b)*math.sin(ang)*math.cos(lat),
                             math.cos(ang) - math.sin(lat)*math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)

# ── Transit prediction ────────────────────────────────────────────────────────

def predict_transit(ac: dict, pred_cfg: dict,
                    sun_az0, sun_el0, sun_az1, sun_el1,
                    obs_lat, obs_lon, obs_ellip_m,
                    geoid_m, qnh_hpa) -> dict | None:
    """
    Extrapolate aircraft position over the prediction window at fixed step_seconds
    intervals, compute angular separation from the sun at each step, and return
    the closest-approach distance and time.

    Sun position is linearly interpolated between t=0 and t=window (the sun moves
    ~0.24° over 60 s, making linear interpolation accurate to <1 arcsecond).

    Returns {sep_min, tau_min} or None if aircraft data is insufficient.
    """
    if not all(ac.get(k) is not None for k in ("lat","lon","alt_ft","track","gs_kt")):
        return None

    window = pred_cfg["window_seconds"]
    step   = pred_cfg["step_seconds"]
    gs_ms  = ac["gs_kt"] * 0.514444
    seps: list[tuple[float, float]] = []   # (tau_s, separation_deg)

    for i in range(int(window / step) + 1):
        tau  = i * step
        frac = tau / window if window > 0 else 0.0

        lat2, lon2 = _dest_point(ac["lat"], ac["lon"], ac["track"], gs_ms * tau)
        azel = aircraft_azel(lat2, lon2, ac["alt_ft"],
                             obs_lat, obs_lon, obs_ellip_m, geoid_m, qnh_hpa)
        if azel is None:
            continue
        ac_az, ac_el, _ = azel
        s_az = sun_az0 + (sun_az1 - sun_az0) * frac
        s_el = sun_el0 + (sun_el1 - sun_el0) * frac
        seps.append((tau, ang_sep(ac_az, ac_el, s_az, s_el)))

    if not seps:
        return None

    idx = min(range(len(seps)), key=lambda i: seps[i][1])
    tau_min, sep_min = seps[idx]

    # Parabolic refinement: fits a parabola to the three points around the minimum
    # to locate the true minimum between discrete steps.
    if 0 < idx < len(seps) - 1:
        _, s0 = seps[idx - 1]
        _, s1 = seps[idx]
        _, s2 = seps[idx + 1]
        denom = s0 - 2.0*s1 + s2
        if abs(denom) > 1e-12:
            x = 0.5 * (s0 - s2) / denom   # fraction in [-1, 1]
            if abs(x) <= 1.0:
                tau_min = seps[idx][0] + x * step
                sep_min = s1 - 0.25 * (s0 - s2) * x

    return {"sep_min": max(0.0, sep_min), "tau_min": tau_min}

# ── State machine ─────────────────────────────────────────────────────────────

def classify(pred: dict, r_sun: float, pred_cfg: dict) -> str:
    sep, tau = pred["sep_min"], pred["tau_min"]
    if sep <= r_sun + pred_cfg["margin_deg"] and tau <= pred_cfg["imminent_seconds"]:
        return "IMMINENT"
    if sep <= pred_cfg["watch_deg"]:
        return "WATCH"
    return "SKIP"

# ── Alerts ────────────────────────────────────────────────────────────────────

_alerted: set[str] = set()

def maybe_alert(ac: dict, pred: dict | None, state: str, pred_cfg: dict):
    key = ac.get("hex") or ac.get("callsign") or "?"
    if pred is None or state == "SKIP":
        _alerted.discard(key)
        return
    if state == "IMMINENT" and key not in _alerted:
        _alerted.add(key)
        label = ac.get("callsign") or ac.get("hex") or "?"
        secs  = max(0, int(pred["tau_min"]))
        _notify("☉ Solar Transit",
                f"{label} transit in {secs} s  (sep {pred['sep_min']:.2f}°)")
        _speak(f"Solar transit in {secs} seconds")

def _notify(title: str, msg: str):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Glass"'],
            capture_output=True, check=False
        )
    except FileNotFoundError:
        pass

def _speak(text: str):
    try:
        subprocess.Popen(["say", text])
    except FileNotFoundError:
        print("\a", end="", flush=True)

# ── Terminal rendering ────────────────────────────────────────────────────────

_R, _B, _D = "\x1b[0m", "\x1b[1m", "\x1b[2m"
_RED, _YEL, _GRN = "\x1b[31m", "\x1b[33m", "\x1b[32m"

def _az_in_fov(az: float, fov_min, fov_max) -> bool:
    if fov_min is None or fov_max is None:
        return True
    if fov_max >= fov_min:
        return fov_min <= az <= fov_max
    return az >= fov_min or az <= fov_max   # handles 350°–10° style wraparound

def render(rows, sun_az, sun_el, r_sun, source, obs_label,
           fov_min, fov_max, pred_cfg, error):
    fov_ok  = _az_in_fov(sun_az, fov_min, fov_max)
    now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    print("\x1b[2J\x1b[H", end="")   # clear screen

    sun_info = (f"az {sun_az:5.1f}°  el {sun_el:5.1f}°  ⌀ {2*r_sun:.3f}°"
                if sun_el > 0 else f"{_D}below horizon{_R}")
    fov_tag  = (f"{_GRN}FOV ✓{_R}" if fov_ok
                else f"{_D}FOV ✗ (sun outside {fov_min}°–{fov_max}°){_R}")
    err_tag  = f"   {_RED}⚠ {error}{_R}" if error else ""

    print(f"{_B}☉  SOLAR TRANSIT PREDICTOR — {obs_label}{_R}  {_D}{now_str}{_R}")
    print(f"   Sun: {sun_info}   {fov_tag}{err_tag}"
          f"   {_D}src {source or '–'}{_R}")
    print()

    if not rows:
        print(f"  {_D}No aircraft within {pred_cfg['display_cutoff_deg']:.0f}° of the sun.{_R}")
    else:
        HDR = (f"  {'STATE':<10} {'CALLSIGN':<10} {'TYPE':<6}"
               f"  {'ALT':>8}  {'CUR-SEP':>8}  {'MIN-SEP':>8}  {'IN':>8}")
        print(f"{_D}{HDR}{_R}")
        print("  " + "─" * 68)

        for r in rows:
            ac, state, pred = r["ac"], r["state"], r["pred"]
            cs    = (ac.get("callsign") or ac.get("hex") or "?")[:10]
            typ   = (ac.get("type") or "–")[:6]
            alt_s = (f"{ac['alt_ft']/3281.0:4.1f} km"
                     if isinstance(ac.get("alt_ft"), (int, float)) else "    –  ")
            sep_s = f"{r['cur_sep']:.2f}°" if r.get("cur_sep") is not None else "    –  "
            if pred:
                min_s = f"{pred['sep_min']:.2f}°"
                in_s  = f"{pred['tau_min']:.1f} s"
            else:
                min_s = in_s = "    –  "

            if state == "IMMINENT":
                col, st = f"{_B}{_RED}", "●IMMINENT "
            elif state == "WATCH":
                col, st = _YEL, "○WATCH    "
            else:
                col, st = _D, "          "

            print(f"  {col}{st}{_R}{cs:<10} {typ:<6}"
                  f"  {alt_s:>8}  {sep_s:>8}  {min_s:>8}  {in_s:>8}")

    imm = sum(1 for r in rows if r["state"] == "IMMINENT")
    wat = sum(1 for r in rows if r["state"] == "WATCH")
    print()
    print(f"  {_D}{len(rows)} displayed · {imm} imminent · {wat} watch"
          f"  ·  refresh {REFRESH_S} s{_R}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg      = load_config()
    obs      = cfg["observer"]
    pred_cfg = cfg["prediction"]

    lat         = obs["latitude"]
    lon         = obs["longitude"]
    obs_ellip_m = obs["ellipsoidal_alt_m"]
    geoid_m     = obs["geoid_offset_m"]
    qnh_hpa     = pred_cfg["qnh_hpa"]
    radius_nm   = pred_cfg["radius_nm"]
    obs_label   = obs.get("label", "Observer")
    fov_min     = obs.get("fov_min_az")
    fov_max     = obs.get("fov_max_az")
    cutoff      = pred_cfg["display_cutoff_deg"]

    print("Initialising skyfield (downloads de421.bsp ~17 MB on first run) …",
          flush=True)
    ts, site, sun_body = init_skyfield(lat, lon, obs_ellip_m)
    print("Ready.\n")

    try:
        while True:
            t0 = time.monotonic()

            t_now    = ts.now()
            t_future = ts.tt_jd(t_now.tt + pred_cfg["window_seconds"] / 86400.0)

            sun_az0, sun_el0, r_sun = sun_azel(site, sun_body, t_now)
            sun_az1, sun_el1, _     = sun_azel(site, sun_body, t_future)

            data   = get_aircraft(lat, lon, radius_nm)
            planes = data["aircraft"]
            source = data.get("source")
            error  = data.get("error")

            rows = []

            if sun_el0 > 0:    # skip geometry when sun is below horizon
                for ac in planes:
                    if not isinstance(ac.get("alt_ft"), (int, float)):
                        continue

                    azel = aircraft_azel(
                        ac["lat"], ac["lon"], ac["alt_ft"],
                        lat, lon, obs_ellip_m, geoid_m, qnh_hpa
                    )
                    if azel is None:
                        continue
                    ac_az, ac_el, slant_km = azel

                    if ac_el < 0:    # aircraft below horizon from observer
                        continue

                    cur_sep = ang_sep(ac_az, ac_el, sun_az0, sun_el0)

                    pred = predict_transit(
                        ac, pred_cfg,
                        sun_az0, sun_el0, sun_az1, sun_el1,
                        lat, lon, obs_ellip_m, geoid_m, qnh_hpa
                    )

                    eff_sep = pred["sep_min"] if pred else cur_sep
                    if eff_sep > cutoff and cur_sep > cutoff:
                        continue

                    state = classify(pred, r_sun, pred_cfg) if pred else "SKIP"

                    fov_ok = _az_in_fov(sun_az0, fov_min, fov_max)
                    if fov_ok:
                        maybe_alert(ac, pred, state, pred_cfg)

                    rows.append({"ac": ac, "cur_sep": cur_sep, "pred": pred,
                                 "slant_km": slant_km, "state": state})

            rows.sort(key=lambda r: (
                r["pred"]["sep_min"] if r["pred"] else r.get("cur_sep") or 999.0
            ))

            render(rows, sun_az0, sun_el0, r_sun, source, obs_label,
                   fov_min, fov_max, pred_cfg, error)

            time.sleep(max(0.0, REFRESH_S - (time.monotonic() - t0)))

    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
