#!/usr/bin/env python3
"""
Bluewater ocean-currents builder (Copernicus Marine Service / CMEMS).

Downloads Gulf of America current forecasts from GLOBAL_ANALYSISFORECAST_PHY_001_024 and
writes them as small PNG frames that index.html animates as Windy-style particles.

One-time setup
    pip install copernicusmarine truststore
    copernicusmarine login        (free account: https://data.marine.copernicus.eu/register)

Run from the app folder (the one containing index.html):
    python tools/build_currents.py                       # surface, hourly, next 5 days
    python tools/build_currents.py --days 7 --step 3     # 7 days, every 3 hours
    python tools/build_currents.py --depths 50 100 200   # add sub-surface levels (6-hourly)
    python tools/build_currents.py --surface ocean       # ocean currents only (no tide / wave drift)

Run it once or twice a day (tools/update_currents.bat works with Windows Task Scheduler).

Output
    data/currents/manifest.json
    data/currents/<set>/<YYYYMMDDTHHMM>.png

Frame encoding (8-bit RGB PNG, row 0 = north):
    R = eastward velocity, G = northward velocity, B = 255 sea / 0 land or no data
    value_m_s = (q - 1) / 254 * 2 * range - range     (range is stored per set in the manifest)
"""
import argparse
import datetime as dt
import json
import math
import os
import pathlib
import shutil
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "currents"

PRODUCT = "GLOBAL_ANALYSISFORECAST_PHY_001_024"
DOI = "10.48670/moi-00016"
SURFACE_DATASET = "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i"   # hourly surface, incl. tides + wave drift
DEPTH_DATASET = "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i"    # 6-hourly, all depth levels
DEFAULT_BBOX = (-98.0, 18.0, -80.0, 31.0)                      # west, south, east, north

SURFACE_VARIABLES = {
    "total": [("utotal", "vtotal"), ("uo", "vo")],
    "ocean": [("uo", "vo")],
}
SET_LABELS = {
    "utotal": "Surface · total (ocean + tide + wave drift)",
    "uo": "Surface · ocean currents",
}


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


# --------------------------------------------------------------------------- data access
def open_currents(cm, dataset_id, bbox, start, end, candidates, depth, credentials):
    kwargs = dict(dataset_id=dataset_id,
                  minimum_longitude=bbox[0], maximum_longitude=bbox[2],
                  minimum_latitude=bbox[1], maximum_latitude=bbox[3],
                  start_datetime=start.strftime("%Y-%m-%dT%H:%M:%S"),
                  end_datetime=end.strftime("%Y-%m-%dT%H:%M:%S"),
                  **credentials)
    if depth is not None:
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

    actual_depth = None
    if "depth" in ds.dims:
        ds = ds.isel(depth=0) if depth is None else ds.sel(depth=depth, method="nearest")
    if "depth" in ds.coords and ds["depth"].size == 1:
        actual_depth = float(ds["depth"].values)
    return ds, pair, actual_depth


def build_set(ds, pair, set_id, label, dataset_id, step, actual_depth):
    import numpy as np
    t_name = coord_name(ds, "time")
    lat_name = coord_name(ds, "latitude", "lat")
    lon_name = coord_name(ds, "longitude", "lon")
    ds = ds.sortby(lat_name, ascending=False).sortby(lon_name)

    times = ds[t_name].values
    if len(times) == 0:
        raise SystemExit(f"[{set_id}] no time steps in the requested window (the forecast may not reach that far)")
    hours = (times - times[0]) / np.timedelta64(1, "h")
    keep = [i for i, h in enumerate(hours) if abs(h / step - round(h / step)) < 1e-6]
    ds = ds.isel({t_name: keep})
    print(f"[{set_id}] downloading {len(keep)} time steps of {pair[0]}/{pair[1]} …")
    ds = ds.load()

    u_all = ds[pair[0]].transpose(t_name, lat_name, lon_name).values.astype("float32")
    v_all = ds[pair[1]].transpose(t_name, lat_name, lon_name).values.astype("float32")
    lons = ds[lon_name].values.astype(float)
    lats = ds[lat_name].values.astype(float)
    times = ds[t_name].values
    height, width = len(lats), len(lons)
    if width < 2 or height < 2:
        raise SystemExit(f"[{set_id}] grid is too small ({width}×{height}); check --bbox")

    with np.errstate(all="ignore"):
        peak = float(np.nanmax([np.nanmax(np.abs(u_all)), np.nanmax(np.abs(v_all))]))
    if not math.isfinite(peak):
        raise SystemExit(f"[{set_id}] no valid current values in the requested area")
    value_range = max(0.5, math.ceil(peak * 1.02 * 10) / 10)

    staging = OUT / f".{set_id}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    frames = []
    for k, t in enumerate(times):
        iso = np.datetime_as_string(t, unit="s") + "Z"
        name = iso[:16].replace("-", "").replace(":", "") + ".png"
        write_png(staging / name, encode_frame(u_all[k], v_all[k], value_range))
        frames.append({"time": iso, "file": f"{set_id}/{name}"})
    final = OUT / set_id
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)

    step_hours = float(np.median(np.diff(hours[keep]))) if len(keep) > 1 else float(step)
    size_kb = sum(p.stat().st_size for p in final.glob("*.png")) / 1024
    print(f"[{set_id}] wrote {len(frames)} frames ({width}×{height}, ±{value_range} m/s, {size_kb:,.0f} KB)")
    return {
        "id": set_id,
        "label": label,
        "datasetId": dataset_id,
        "variables": list(pair),
        "depth": actual_depth,
        "stepHours": round(step_hours, 3),
        "range": value_range,
        "encoding": "rgb8: R=u, G=v, B=mask; value=(q-1)/254*2*range-range",
        "grid": {
            "width": width,
            "height": height,
            "lon0": round(float(lons[0]), 6),
            "lat0": round(float(lats[0]), 6),
            "dLon": round(float((lons[-1] - lons[0]) / (width - 1)), 8),
            "dLat": round(float((lats[0] - lats[-1]) / (height - 1)), 8),
        },
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
                        help="extra levels in metres from the 6-hourly 3D dataset, e.g. --depths 50 100 200")
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        default=DEFAULT_BBOX, help="area in degrees (default: Gulf of America)")
    parser.add_argument("--ca-bundle", help="PEM file with your company root certificate (instead of truststore)")
    parser.add_argument("--username", help="Copernicus Marine username (default: saved login)")
    parser.add_argument("--password", help="Copernicus Marine password (default: saved login)")
    args = parser.parse_args()
    if args.step < 1:
        parser.error("--step must be at least 1")

    setup_tls(args.ca_bundle)
    try:
        import copernicusmarine as cm
        import numpy  # noqa: F401  (required by the toolbox; imported here for a clear error)
    except ImportError as exc:
        raise SystemExit(f"Missing package ({exc.name}). Install with:  pip install copernicusmarine truststore")

    credentials = {k: v for k, v in (("username", args.username), ("password", args.password)) if v}
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    start = now - dt.timedelta(hours=args.past_hours)
    end = now + dt.timedelta(days=args.days)
    print(f"Window: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC · bbox {tuple(args.bbox)}")
    OUT.mkdir(parents=True, exist_ok=True)

    sets = []
    ds, pair, depth = open_currents(cm, SURFACE_DATASET, args.bbox, start, end,
                                    SURFACE_VARIABLES[args.surface], None, credentials)
    sets.append(build_set(ds, pair, "surface", SET_LABELS.get(pair[0], "Surface"), SURFACE_DATASET, args.step, depth))

    for target in args.depths:
        ds, pair, depth = open_currents(cm, DEPTH_DATASET, args.bbox, start, end, [("uo", "vo")], target, credentials)
        set_id = f"d{round(target)}"
        label = f"{depth:.0f} m depth" if depth is not None else f"{target:.0f} m depth"
        sets.append(build_set(ds, pair, set_id, label, DEPTH_DATASET, max(args.step, 6), depth))

    # Remove frame folders from previous runs that are no longer referenced.
    keep_ids = {s["id"] for s in sets}
    for folder in OUT.iterdir():
        if folder.is_dir() and folder.name not in keep_ids:
            shutil.rmtree(folder, ignore_errors=True)

    generated = dt.datetime.now(dt.timezone.utc)
    manifest = {
        "generated": generated.isoformat(timespec="seconds"),
        "version": generated.strftime("%Y%m%dT%H%M%S"),
        "product": PRODUCT,
        "doi": DOI,
        "attribution": "Generated using E.U. Copernicus Marine Service Information",
        "bbox": list(args.bbox),
        "sets": sets,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"manifest written: {OUT / 'manifest.json'}")


if __name__ == "__main__":
    main()
