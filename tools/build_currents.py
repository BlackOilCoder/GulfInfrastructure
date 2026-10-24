#!/usr/bin/env python3
"""
Bluewater ocean-currents builder (Copernicus Marine Service / CMEMS).

Downloads Gulf of America current forecasts from GLOBAL_ANALYSISFORECAST_PHY_001_024 and
writes them as small PNG frames that index.html animates as Windy-style particles. It also
writes a full-depth "profile" set used by the map's right-click "Show depth graph" tool.

One-time setup
    pip install copernicusmarine truststore
    copernicusmarine login        (free account: https://data.marine.copernicus.eu/register)

Run from the app folder (the one containing index.html):
    python tools/build_currents.py                       # surface hourly 5 days + depth profiles
    python tools/build_currents.py --days 7 --step 3     # 7 days, every 3 hours
    python tools/build_currents.py --depths 50 100 200   # add sub-surface animation levels (6-hourly)
    python tools/build_currents.py --surface ocean       # ocean currents only (no tide / wave drift)
    python tools/build_currents.py --profile-step 6      # depth profiles every 6 h (bigger download)
    python tools/build_currents.py --no-profile          # skip depth profiles

Run it once or twice a day (tools/update_currents.bat works with Windows Task Scheduler).

Output
    data/currents/manifest.json
    data/currents/<set>-<run>/<YYYYMMDDTHHMM>.png
    data/currents/profile-<run>/<YYYYMMDDTHHMM>.png

Each run writes into new, uniquely named folders and then points the manifest at them, so
nothing has to be renamed or deleted while OneDrive (or antivirus) has files open. Folders
from earlier runs are removed afterwards on a best-effort basis.

Frame encoding (8-bit RGB PNG, row 0 = north):
    R = eastward velocity, G = northward velocity, B = 255 sea / 0 land, seabed or no data
    value_m_s = (q - 1) / 254 * 2 * range - range
Surface/level sets use one range per set. Profile frames stack every depth level vertically
(level 0 = top rows) and store one range per level per frame ("ranges" in the manifest).
"""
import argparse
import datetime as dt
import json
import math
import os
import pathlib
import shutil
import stat
import struct
import sys
import time
import zlib
from config import data_directory

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = data_directory() / "currents"

PRODUCT = "GLOBAL_ANALYSISFORECAST_PHY_001_024"
DOI = "10.48670/moi-00016"
SURFACE_DATASET = "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i"   # hourly surface, incl. tides + wave drift
DEPTH_DATASET = "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i"    # 6-hourly, all depth levels
DEFAULT_BBOX = (-98.0, 18.0, -80.0, 31.0)                      # west, south, east, north
MAX_CANVAS_ROWS = 16000                                         # browser canvas height safety limit

SURFACE_VARIABLES = {
    "total": [("utotal", "vtotal"), ("uo", "vo")],
    "ocean": [("uo", "vo")],
}
SET_LABELS = {
    "utotal": "Surface · total (ocean + tide + wave drift)",
    "uo": "Surface · ocean currents",
}


# --------------------------------------------------------------------------- file-system helpers
def _clear_readonly_and_retry(func, path, _exc):
    """rmtree error handler: OneDrive/Explorer can leave files read-only."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path, attempts=3):
    """Deletes a folder, retrying briefly. Returns True if it is gone."""
    for attempt in range(attempts):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_clear_readonly_and_retry)
            else:
                shutil.rmtree(path, onerror=_clear_readonly_and_retry)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(1 + attempt)
    return not path.exists()


def write_text_atomic(path, text, attempts=5):
    """Writes via a temporary file + replace, retrying if the target is briefly locked."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(1 + attempt)
    path.write_text(text, encoding="utf-8")
    try:
        tmp.unlink()
    except OSError:
        pass


def clean_old_outputs(keep):
    """Removes folders not referenced by the new manifest (best effort)."""
    left = []
    for entry in OUT.iterdir():
        if not entry.is_dir() or entry.name in keep:
            continue
        if not remove_tree(entry, attempts=2):
            left.append(entry.name)
    if left:
        print(f"Note: could not remove {len(left)} old folder(s) ({', '.join(left)}); "
              "they are no longer used and will be retried next run.")


# --------------------------------------------------------------------------- setup helpers
def setup_tls(ca_bundle):
    """Corporate TLS inspection: trust the Windows certificate store (or an explicit bundle)."""
    if ca_bundle:
        os.environ["SSL_CERT_FILE"] = ca_bundle
        os.environ["REQUESTS_CA_BUNDLE"] = ca_bundle
        print(f"TLS: using CA bundle {ca_bundle}")
        return
    try:
        import truststore
        truststore.inject_into_ssl()
        print("TLS: using the Windows certificate store (truststore)")
    except ImportError:
        print("TLS: truststore not installed. If you see CERTIFICATE_VERIFY_FAILED, run: pip install truststore")


def explain_failure(exc):
    text = str(exc)
    low = text.lower()
    print(f"\nCopernicus Marine request failed: {text}", file=sys.stderr)
    if "certificate" in low or "ssl" in low:
        print("  -> TLS problem: pip install truststore  (or pass --ca-bundle path\\to\\company-root.pem)", file=sys.stderr)
    if any(word in low for word in ("credential", "username", "password", "401", "unauthor", "login")):
        print("  -> Credentials: run  copernicusmarine login  once (free account at data.marine.copernicus.eu)", file=sys.stderr)


def coord_name(ds, *names):
    for name in names:
        if name in ds.coords or name in ds.dims:
            return name
    raise SystemExit(f"Dataset has none of the coordinates {names}; found {list(ds.coords)}")


# --------------------------------------------------------------------------- PNG encoding
def write_png(path, rgb):
    """Minimal RGB PNG writer (stdlib zlib) using the 'Sub' filter, which suits smooth fields."""
    import numpy as np
    height, width, _ = rgb.shape
    rows = rgb.reshape(height, width * 3).astype(np.int16)
    sub = rows.copy()
    sub[:, 3:] -= rows[:, :-3]
    filtered = np.empty((height, width * 3 + 1), np.uint8)
    filtered[:, 0] = 1
    filtered[:, 1:] = (sub % 256).astype(np.uint8)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(filtered.tobytes(), 9))
                     + chunk(b"IEND", b""))


def encode_frame(u, v, value_range):
    import numpy as np
    mask = np.isfinite(u) & np.isfinite(v)

    def quantize(a):
        q = np.rint((np.nan_to_num(a, nan=0.0) + value_range) / (2 * value_range) * 254) + 1
        return np.clip(q, 1, 255).astype(np.uint8)

    rgb = np.zeros(u.shape + (3,), np.uint8)
    rgb[..., 0] = np.where(mask, quantize(u), 0)
    rgb[..., 1] = np.where(mask, quantize(v), 0)
    rgb[..., 2] = np.where(mask, 255, 0)
    return rgb


def level_range(u, v):
    """Smallest 0.05 m/s step above the level's peak speed component (min 0.1 m/s)."""
    import numpy as np
    with np.errstate(all="ignore"):
        peak = float(np.nanmax([np.nanmax(np.abs(u)), np.nanmax(np.abs(v))])) if np.isfinite(u).any() else 0.0
    if not math.isfinite(peak):
        peak = 0.0
    return max(0.1, math.ceil(peak * 1.02 / 0.05) * 0.05)


# --------------------------------------------------------------------------- data access
def open_currents(cm, dataset_id, bbox, start, end, candidates, depth, credentials, max_depth=None):
    kwargs = dict(dataset_id=dataset_id,
                  minimum_longitude=bbox[0], maximum_longitude=bbox[2],
                  minimum_latitude=bbox[1], maximum_latitude=bbox[3],
                  start_datetime=start.strftime("%Y-%m-%dT%H:%M:%S"),
                  end_datetime=end.strftime("%Y-%m-%dT%H:%M:%S"),
                  **credentials)
    if max_depth is not None:
        kwargs.update(minimum_depth=0, maximum_depth=max_depth)
    elif depth is not None:
        kwargs.update(minimum_depth=0, maximum_depth=depth * 1.5 + 10)
    try:
        ds = cm.open_dataset(**kwargs)
    except Exception as exc:
        explain_failure(exc)
        raise SystemExit(1)

    pair = next(((u, v) for u, v in candidates if u in ds.data_vars and v in ds.data_vars), None)
    if not pair:
        raise SystemExit(f"{dataset_id}: none of {candidates} found; variables are {list(ds.data_vars)}")
    ds = ds[list(pair)]
    if max_depth is not None:
        return ds, pair, None

    actual_depth = None
    if "depth" in ds.dims:
        ds = ds.isel(depth=0) if depth is None else ds.sel(depth=depth, method="nearest")
    if "depth" in ds.coords and ds["depth"].size == 1:
        actual_depth = float(ds["depth"].values)
    return ds, pair, actual_depth


def grid_info(ds, lat_name, lon_name):
    lons = ds[lon_name].values.astype(float)
    lats = ds[lat_name].values.astype(float)
    width, height = len(lons), len(lats)
    if width < 2 or height < 2:
        raise SystemExit(f"grid is too small ({width}×{height}); check --bbox")
    return width, height, {
        "width": width,
        "height": height,
        "lon0": round(float(lons[0]), 6),
        "lat0": round(float(lats[0]), 6),
        "dLon": round(float((lons[-1] - lons[0]) / (width - 1)), 8),
        "dLat": round(float((lats[0] - lats[-1]) / (height - 1)), 8),
    }


def keep_steps(times, step):
    import numpy as np
    hours = (times - times[0]) / np.timedelta64(1, "h")
    return [i for i, h in enumerate(hours) if abs(h / step - round(h / step)) < 1e-6], hours


def build_set(ds, pair, set_id, label, dataset_id, step, actual_depth, run_id):
    import numpy as np
    t_name = coord_name(ds, "time")
    lat_name = coord_name(ds, "latitude", "lat")
    lon_name = coord_name(ds, "longitude", "lon")
    ds = ds.sortby(lat_name, ascending=False).sortby(lon_name)

    times = ds[t_name].values
    if len(times) == 0:
        raise SystemExit(f"[{set_id}] no time steps in the requested window (the forecast may not reach that far)")
    keep, hours = keep_steps(times, step)
    ds = ds.isel({t_name: keep})
    print(f"[{set_id}] downloading {len(keep)} time steps of {pair[0]}/{pair[1]} …")
    ds = ds.load()

    u_all = ds[pair[0]].transpose(t_name, lat_name, lon_name).values.astype("float32")
    v_all = ds[pair[1]].transpose(t_name, lat_name, lon_name).values.astype("float32")
    times = ds[t_name].values
    width, height, grid = grid_info(ds, lat_name, lon_name)

    with np.errstate(all="ignore"):
        peak = float(np.nanmax([np.nanmax(np.abs(u_all)), np.nanmax(np.abs(v_all))]))
    if not math.isfinite(peak):
        raise SystemExit(f"[{set_id}] no valid current values in the requested area")
    value_range = max(0.5, math.ceil(peak * 1.02 * 10) / 10)

    folder_name = f"{set_id}-{run_id}"
    folder = OUT / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    for k, t in enumerate(times):
        iso = np.datetime_as_string(t, unit="s") + "Z"
        name = iso[:16].replace("-", "").replace(":", "") + ".png"
        write_png(folder / name, encode_frame(u_all[k], v_all[k], value_range))
        frames.append({"time": iso, "file": f"{folder_name}/{name}"})

    step_hours = float(np.median(np.diff(hours[keep]))) if len(keep) > 1 else float(step)
    size_kb = sum(p.stat().st_size for p in folder.glob("*.png")) / 1024
    print(f"[{set_id}] wrote {len(frames)} frames ({width}×{height}, ±{value_range} m/s, {size_kb:,.0f} KB)")
    return {
        "id": set_id,
        "folder": folder_name,
        "label": label,
        "datasetId": dataset_id,
        "variables": list(pair),
        "depth": actual_depth,
        "stepHours": round(step_hours, 3),
        "range": value_range,
        "encoding": "rgb8: R=u, G=v, B=mask; value=(q-1)/254*2*range-range",
        "grid": grid,
        "frames": frames,
    }


def build_profile(ds, pair, step, run_id):
    """All depth levels per time step, stacked vertically in one PNG per time step."""
    import numpy as np
    t_name = coord_name(ds, "time")
    lat_name = coord_name(ds, "latitude", "lat")
    lon_name = coord_name(ds, "longitude", "lon")
    if "depth" not in ds.dims:
        raise SystemExit("[profile] the 3D dataset has no depth dimension")
    ds = ds.sortby(lat_name, ascending=False).sortby(lon_name).sortby("depth")
    depths = [float(d) for d in ds["depth"].values]
    width, height, grid = grid_info(ds, lat_name, lon_name)
    if len(depths) * height > MAX_CANVAS_ROWS:
        keep_levels = MAX_CANVAS_ROWS // height
        print(f"[profile] limiting to the top {keep_levels} of {len(depths)} levels (browser canvas size)")
        ds = ds.isel(depth=slice(0, keep_levels))
        depths = depths[:keep_levels]

    times = ds[t_name].values
    if len(times) == 0:
        raise SystemExit("[profile] no time steps in the requested window")
    keep, hours = keep_steps(times, step)
    print(f"[profile] downloading {len(keep)} time steps × {len(depths)} depth levels "
          f"(0–{depths[-1]:,.0f} m) of {pair[0]}/{pair[1]} …")

    folder_name = f"profile-{run_id}"
    folder = OUT / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    for n, k in enumerate(keep, 1):
        snap = ds.isel({t_name: k}).load()          # one time step at a time keeps memory low
        u = snap[pair[0]].transpose("depth", lat_name, lon_name).values.astype("float32")
        v = snap[pair[1]].transpose("depth", lat_name, lon_name).values.astype("float32")
        ranges, blocks = [], []
        for level in range(len(depths)):
            r = level_range(u[level], v[level])
            ranges.append(r)
            blocks.append(encode_frame(u[level], v[level], r))
        iso = np.datetime_as_string(times[k], unit="s") + "Z"
        name = iso[:16].replace("-", "").replace(":", "") + ".png"
        write_png(folder / name, np.concatenate(blocks, axis=0))
        frames.append({"time": iso, "file": f"{folder_name}/{name}", "ranges": ranges})
        print(f"    {n}/{len(keep)} {iso}", end="\r", flush=True)
    print()
    step_hours = float(np.median(np.diff(hours[keep]))) if len(keep) > 1 else float(step)
    size_kb = sum(p.stat().st_size for p in folder.glob("*.png")) / 1024
    print(f"[profile] wrote {len(frames)} frames ({width}×{height}×{len(depths)} levels, {size_kb:,.0f} KB)")
    return {
        "folder": folder_name,
        "datasetId": DEPTH_DATASET,
        "variables": list(pair),
        "depths": [round(d, 3) for d in depths],
        "stepHours": round(step_hours, 3),
        "encoding": "rgb8 stacked by level (level 0 = top); R=u, G=v, B=mask; value=(q-1)/254*2*range-range, range per level per frame",
        "grid": grid,
        "frames": frames,
    }


# --------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=float, default=5, help="forecast length to download (default 5)")
    parser.add_argument("--past-hours", type=int, default=0, help="also include this many hours before now")
    parser.add_argument("--step", type=int, default=1, help="hours between surface frames (default 1)")
    parser.add_argument("--surface", choices=sorted(SURFACE_VARIABLES), default="total",
                        help="total = ocean + tide + wave drift (default); ocean = ocean circulation only")
    parser.add_argument("--depths", type=float, nargs="*", default=[],
                        help="extra animation levels in metres (6-hourly 3D dataset), e.g. --depths 50 100 200")
    parser.add_argument("--no-profile", action="store_true", help="skip the full-depth profile set")
    parser.add_argument("--profile-days", type=float, help="days of depth profiles (default: same as --days)")
    parser.add_argument("--profile-step", type=int, default=12,
                        help="hours between depth profiles, multiple of 6 (default 12)")
    parser.add_argument("--profile-max-depth", type=float, default=4000,
                        help="deepest level in metres for profiles (default 4000, below the Gulf's deepest point)")
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        default=DEFAULT_BBOX, help="area in degrees (default: Gulf of America)")
    parser.add_argument("--ca-bundle", help="PEM file with your company root certificate (instead of truststore)")
    parser.add_argument("--username", help="Copernicus Marine username (default: saved login)")
    parser.add_argument("--password", help="Copernicus Marine password (default: saved login)")
    args = parser.parse_args()
    if args.step < 1:
        parser.error("--step must be at least 1")
    if args.profile_step < 6 or args.profile_step % 6:
        parser.error("--profile-step must be a multiple of 6")

    setup_tls(args.ca_bundle)
    try:
        import copernicusmarine as cm
        import numpy  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"Missing package ({exc.name}). Install with:  pip install copernicusmarine truststore")

    credentials = {k: v for k, v in (
        ("username", args.username or os.environ.get("BLUEWATER_COPERNICUS_USERNAME")),
        ("password", args.password or os.environ.get("BLUEWATER_COPERNICUS_PASSWORD"))) if v}
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    start = now - dt.timedelta(hours=args.past_hours)
    end = now + dt.timedelta(days=args.days)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    print(f"Window: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC · bbox {tuple(args.bbox)}")
    OUT.mkdir(parents=True, exist_ok=True)

    sets = []
    ds, pair, depth = open_currents(cm, SURFACE_DATASET, args.bbox, start, end,
                                    SURFACE_VARIABLES[args.surface], None, credentials)
    sets.append(build_set(ds, pair, "surface", SET_LABELS.get(pair[0], "Surface"), SURFACE_DATASET,
                          args.step, depth, run_id))

    for target in args.depths:
        ds, pair, depth = open_currents(cm, DEPTH_DATASET, args.bbox, start, end, [("uo", "vo")], target, credentials)
        set_id = f"d{round(target)}"
        label = f"{depth:.0f} m depth" if depth is not None else f"{target:.0f} m depth"
        sets.append(build_set(ds, pair, set_id, label, DEPTH_DATASET, max(args.step, 6), depth, run_id))

    profile = None
    if not args.no_profile:
        # Start on a 6-hour boundary at or before now so the profile covers the current time.
        p_start = start.replace(hour=start.hour - start.hour % 6)
        p_end = now + dt.timedelta(days=args.profile_days if args.profile_days is not None else args.days)
        ds, pair, _ = open_currents(cm, DEPTH_DATASET, args.bbox, p_start, p_end, [("uo", "vo")], None,
                                    credentials, max_depth=args.profile_max_depth)
        profile = build_profile(ds, pair, args.profile_step, run_id)

    generated = dt.datetime.now(dt.timezone.utc)
    manifest = {
        "generated": generated.isoformat(timespec="seconds"),
        "version": run_id,
        "product": PRODUCT,
        "doi": DOI,
        "attribution": "Generated using E.U. Copernicus Marine Service Information",
        "bbox": list(args.bbox),
        "sets": sets,
    }
    if profile:
        manifest["profile"] = profile
    write_text_atomic(OUT / "manifest.json", json.dumps(manifest, indent=1))
    print(f"manifest written: {OUT / 'manifest.json'}")

    keep = {s["folder"] for s in sets}
    if profile:
        keep.add(profile["folder"])
    clean_old_outputs(keep)


if __name__ == "__main__":
    main()
