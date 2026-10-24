#!/usr/bin/env python3
"""Install BOEM East/West depth rasters, derived hillshade and an MBTiles disk cache.

Run through tools/install_bathymetry.bat. Data defaults to LOCALAPPDATA/Bluewater/bathymetry.
--directory overrides storage; --refresh downloads a new source copy; --source NAME=TIFF
accepts an existing signed-elevation GeoTIFF in feet (repeat for multiple rasters).
The active manifest is replaced only after the new package is complete.
"""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import importlib.resources
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import time
import urllib.request
import zipfile

from bathymetry import Bathymetry, WORLD, default_directory, initialize_tiles, configure_projection, save_local_configuration

SOURCES = {
    "east": "https://www.boem.gov/sites/default/files/Bathymetry/BOEM_Bathymetry_East_ft_tiff.zip",
    "west": "https://www.boem.gov/sites/default/files/Bathymetry/BOEM_Bathymetry_West_ft_tiff.zip",
}
PAGE = "https://www.boem.gov/oil-gas-energy/mapping-and-data/map-gallery/northern-goa-deepwater-bathymetry-grid-3d-seismic"
DATUM_GRID = "https://cdn.proj.org/us_noaa_conus.tif"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ssl_context():
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


def install_datum_grid(root):
    # The BOEM source is NAD27. Install NOAA's published CONUS correction grid once.
    path = root / "proj" / "us_noaa_conus.tif"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(".part")
        print("Installing NOAA NAD27 coordinate correction grid…", flush=True)
        with urllib.request.urlopen(DATUM_GRID, context=ssl_context(), timeout=120) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out)
        if temporary.read_bytes()[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00"):
            raise ValueError("The coordinate correction grid is not a TIFF.")
        os.replace(temporary, path)
    # The two wheels can bundle different PROJ database versions. Keep each database
    # with its matching library, and install the same NOAA grid alongside both.
    import pyproj
    for name, original in (("pyproj", Path(pyproj.datadir.get_data_dir())),
                           ("rasterio", Path(str(importlib.resources.files("rasterio") / "proj_data")))):
        target = root / "proj" / name
        target.mkdir(exist_ok=True)
        for filename in ("proj.db", "proj.ini"):
            source = original / filename
            if source.exists() and source.resolve() != (target / filename).resolve():
                shutil.copy2(source, target / filename)
        shutil.copy2(path, target / path.name)
    configure_projection(root)
    return {"file": "proj/us_noaa_conus.tif", "url": DATUM_GRID, "sha256": sha256(path)}


def download(url, path, refresh=False):
    if path.exists() and not refresh:
        return
    context = ssl_context()
    partial = path.with_suffix(path.suffix + ".part")
    for attempt in range(4):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "Bluewater-local-bathymetry/1.0", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), context=context, timeout=120) as response:
                append = offset > 0 and response.status == 206
                if not append:
                    offset = 0
                total = int(response.headers.get("Content-Length", "0")) + offset
                progress = -1
                with partial.open("ab" if append else "wb") as out:
                    for block in iter(lambda: response.read(4*1024*1024), b""):
                        out.write(block)
                        offset += len(block)
                        percent = int(offset*100/total) if total else 0
                        if percent//10 != progress:
                            print(f"Download {path.name}: {offset/1e6:,.0f} / {total/1e6:,.0f} MB ({percent}%)", flush=True)
                            progress = percent//10
                if total and offset != total:
                    raise OSError("The download ended before the advertised file size.")
            with zipfile.ZipFile(partial) as archive:
                bad = archive.testzip()
                if bad:
                    raise OSError(f"ZIP integrity check failed: {bad}")
            os.replace(partial, path)
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def source_file(name, url, cache, refresh):
    archive_path = cache / f"boem-{name}-ft.zip"
    download(url, archive_path, refresh)
    digest = sha256(archive_path)
    target = cache / f"{name}-{digest[:12]}.tif"
    if not target.exists():
        print(f"Extracting {name} grid…", flush=True)
        with zipfile.ZipFile(archive_path) as archive:
            members = [info for info in archive.infolist() if info.filename.lower().endswith((".tif", ".tiff"))]
            if not members:
                raise ValueError(f"No GeoTIFF found in {archive_path.name}.")
            member = max(members, key=lambda item: item.file_size)
            temporary = target.with_suffix(".extracting")
            with archive.open(member) as source, temporary.open("wb") as out:
                shutil.copyfileobj(source, out, 4*1024*1024)
            os.replace(temporary, target)
    return target, {"url": url, "archiveSha256": digest}


def cog_copy(source, target):
    from rasterio.shutil import copy
    temporary = target.with_name(target.stem + ".building.tif")
    copy(source, temporary, driver="COG", COMPRESS="DEFLATE", PREDICTOR="YES", BLOCKSIZE=512,
         NUM_THREADS="4", BIGTIFF="IF_SAFER", OVERVIEW_RESAMPLING="AVERAGE")
    os.replace(temporary, target)


def shade_raster(depth_path, output, units="feet"):
    """Shade at native resolution, with a one-cell halo and no cliffs around NoData."""
    import numpy as np
    import pyproj
    import rasterio
    from rasterio.windows import Window
    raw = output.with_name(output.stem + ".raw.tif")
    with rasterio.open(depth_path) as src:
        if src.transform.b or src.transform.d:
            raise ValueError("Rotated source grids must first be converted to a north-up GeoTIFF.")
        crs = pyproj.CRS.from_wkt(src.crs.to_wkt())
        geod = crs.get_geod() if crs.is_geographic else None
        unit_factor = crs.axis_info[0].unit_conversion_factor if not geod else None
        profile = {"driver": "GTiff", "height": src.height, "width": src.width, "count": 1,
                   "dtype": "uint8", "crs": src.crs, "transform": src.transform, "nodata": 0,
                   "tiled": True, "blockxsize": 512, "blockysize": 512, "compress": "deflate", "BIGTIFF": "IF_SAFER"}
        total = math.ceil(src.height/1024)*math.ceil(src.width/1024)
        done = 0
        print(f"Shading {src.width:,} × {src.height:,} cells in {total:,} blocks…", flush=True)
        with rasterio.open(raw, "w", **profile) as dest:
            for row in range(0, src.height, 1024):
                for col in range(0, src.width, 1024):
                    h, w = min(1024, src.height-row), min(1024, src.width-col)
                    r0, c0 = max(0, row-1), max(0, col-1)
                    r1, c1 = min(src.height, row+h+1), min(src.width, col+w+1)
                    values = src.read(1, window=Window(c0, r0, c1-c0, r1-r0), masked=True).astype("float64")
                    valid = ~np.ma.getmaskarray(values) & np.isfinite(values.data)
                    elevation = (values.filled(0)*src.scales[0]+src.offsets[0]) * (.3048 if units == "feet" else 1)
                    # Replication at external raster edges permits a centered derivative everywhere.
                    pad = ((int(r0==row), int(r1==row+h)), (int(c0==col), int(c1==col+w)))
                    elevation, valid = np.pad(elevation, pad, mode="edge"), np.pad(valid, pad, mode="edge")
                    if geod:
                        lat = src.transform.f+(row+np.arange(h)+.5)*src.transform.e
                        lon = np.full(h, src.transform.c+(col+.5)*src.transform.a)
                        _, _, dx = geod.inv(lon, lat, lon+src.transform.a, lat)
                        _, _, dy = geod.inv(lon, lat, lon, lat+src.transform.e)
                        dx, dy = np.abs(dx)[:, None], np.abs(dy)[:, None]
                    else:
                        dx, dy = abs(src.transform.a)*unit_factor, abs(src.transform.e)*unit_factor
                    gx = (elevation[1:-1, 2:]-elevation[1:-1, :-2])/(2*dx)*5
                    gy = (elevation[2:, 1:-1]-elevation[:-2, 1:-1])/(2*dy)*5
                    gx = np.where(valid[1:-1, 2:] & valid[1:-1, :-2], gx, 0)
                    gy = np.where(valid[2:, 1:-1] & valid[:-2, 1:-1], gy, 0)
                    # Light from north at 45 degrees; only display is exaggerated (5x).
                    light = np.clip((gy+1)/np.sqrt(2*(gx*gx+gy*gy+1)), 0, 1)
                    shade = np.where(valid[1:-1, 1:-1], 1+np.rint(light*254), 0).astype("uint8")
                    dest.write(shade, 1, window=Window(col, row, w, h))
                    done += 1
                    if done == total or done % max(1, total//10) == 0:
                        print(f"Hillshade {done:,}/{total:,} blocks", flush=True)
    cog_copy(raw, output)
    raw.unlink()


def prepare_source(name, path, provenance, root, folder):
    import rasterio
    from rasterio.warp import transform_bounds
    out = folder / f"depth-{name}.tif"
    shade = folder / f"shade-{name}.tif"
    print(f"Preparing numeric COG: {name}…", flush=True)
    with rasterio.open(path) as src:
        if src.count != 1 or not src.crs:
            raise ValueError(f"{name} must be a single-band georeferenced depth grid.")
        print(f"Source {name}: {src.width:,}×{src.height:,}, {src.crs}, resolution {src.res}, NoData {src.nodata}", flush=True)
    cog_copy(path, out)
    shade_raster(out, shade)
    with rasterio.open(out) as ds:
        return {"id": name, "depth": out.relative_to(root).as_posix(), "hillshade": shade.relative_to(root).as_posix(),
                "units": "feet", "valueConvention": "elevation", "crs": ds.crs.to_string(),
                "boundsWgs84": list(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=41)),
                "boundsMercator": list(transform_bounds(ds.crs, "EPSG:3857", *ds.bounds, densify_pts=41)),
                "width": ds.width, "height": ds.height, "resolution": list(ds.res),
                "sourceSha256": sha256(path), **provenance}


def update_bounds(sources, root):
    import pyproj
    import rasterio
    updated = []
    for source in sources:
        item = dict(source)
        with rasterio.open(root / source["depth"]) as ds:
            for field, target in (("boundsWgs84", "EPSG:4326"), ("boundsMercator", "EPSG:3857")):
                transformer = pyproj.Transformer.from_crs(ds.crs, target, always_xy=True, allow_ballpark=False)
                item[field] = list(transformer.transform_bounds(*ds.bounds, densify_pts=41))
        updated.append(item)
    return updated


def prebuild(engine, manifest, maximum):
    if maximum < 0:
        return
    tiles = set()
    for zoom in range(4, maximum+1):
        span = 2*WORLD/2**zoom
        for source in manifest["sources"]:
            left, bottom, right, top = source["boundsMercator"]
            for col in range(max(0, math.floor((left+WORLD)/span)), min(2**zoom-1, math.floor((right+WORLD)/span))+1):
                for row in range(max(0, math.floor((WORLD-top)/span)), min(2**zoom-1, math.floor((WORLD-bottom)/span))+1):
                    tiles.add((zoom, col, row))
    print(f"Prebuilding {len(tiles):,} regional hillshade tiles through zoom {maximum}…", flush=True)
    for i, tile in enumerate(sorted(tiles), 1):
        engine.tile(*tile)
        if i % 100 == 0 or i == len(tiles):
            print(f"Hillshade tiles {i:,}/{len(tiles):,}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--directory", type=Path, default=default_directory())
    parser.add_argument("--refresh", action="store_true", help="download new copies of the BOEM source archives")
    parser.add_argument("--rebuild-tiles", action="store_true", help="reuse installed numeric and shade rasters, rebuilding the tile cache")
    parser.add_argument("--source", action="append", metavar="NAME=TIFF", help="use local signed-elevation GeoTIFF(s) in feet instead of downloading")
    parser.add_argument("--max-zoom", type=int, default=14)
    parser.add_argument("--prebuild-max-zoom", type=int, default=11, help="-1 skips tile prefill; detailed tiles are cached when requested")
    args = parser.parse_args()
    if not 0 <= args.max_zoom <= 16 or not -1 <= args.prebuild_max_zoom <= args.max_zoom:
        parser.error("Use max zoom 0–16 and prebuild zoom -1 through max zoom.")
    root = args.directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "build.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f"Another build may be running ({lock}). If it has stopped, remove that lock before retrying.")
    os.write(lock_fd, str(os.getpid()).encode())
    os.close(lock_fd)
    try:
        datum_grid = install_datum_grid(root)
        active_path = root / "manifest.json"
        active = json.loads(active_path.read_text(encoding="utf-8")) if active_path.exists() else None
        if active and not args.refresh and not args.source and not args.rebuild_tiles:
            if active.get("datumGrid") == datum_grid and active["tiles"]["maxZoom"] == args.max_zoom:
                save_local_configuration(root)
                print(f"Local bathymetry is already installed ({active['version']}). Use --refresh to check new BOEM source files, or --rebuild-tiles to rebuild the tile cache.", flush=True)
                return
            args.rebuild_tiles = True
        if args.rebuild_tiles and not active:
            parser.error("Install bathymetry before rebuilding only its tile cache.")
        cache = root / "sources"
        cache.mkdir(exist_ok=True)
        inputs = []
        if args.rebuild_tiles:
            pass
        elif args.source:
            for value in args.source:
                name, separator, filename = value.partition("=")
                if not separator or not name.isascii() or not name.replace("-", "").isalnum():
                    parser.error("Sources must be NAME=TIFF, with a simple alphanumeric name.")
                inputs.append((name, Path(filename).expanduser().resolve(), {"fileName": Path(filename).name}))
        else:
            for name, url in SOURCES.items():
                source, provenance = source_file(name, url, cache, args.refresh)
                inputs.append((name, source, provenance))
        if len({item[0] for item in inputs}) != len(inputs):
            parser.error("Source names must be unique.")
        version = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        folder = root / "versions" / version
        folder.mkdir(parents=True)
        sources = update_bounds(active["sources"], root) if args.rebuild_tiles else update_bounds([prepare_source(name, path, provenance, root, folder) for name, path, provenance in inputs], root)
        tile_path = folder / "hillshade.mbtiles"
        initialize_tiles(tile_path)
        manifest = {"schemaVersion": 1, "version": version, "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "sourcePage": PAGE, "resolutionFt": 40, "sources": sources, "datumGrid": datum_grid,
                    "tiles": {"file": tile_path.relative_to(root).as_posix(), "maxZoom": args.max_zoom,
                              "prebuiltMaxZoom": args.prebuild_max_zoom, "azimuth": 0, "altitude": 45, "verticalExaggeration": 5}}
        with closing(sqlite3.connect(tile_path)) as db:
            db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", [("name", "BOEM hillshade"), ("format", "png"),
                           ("type", "overlay"), ("version", "1.3"), ("minzoom", "0"), ("maxzoom", str(args.max_zoom)),
                           ("attribution", "BOEM northern Gulf deepwater bathymetry")])
            db.commit()
        prebuild(Bathymetry(root, manifest_data=manifest), manifest, args.prebuild_max_zoom)
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temporary = root / "manifest.new.json"
        temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(temporary, root / "manifest.json")
        save_local_configuration(root)
        print(f"Local bathymetry installed: {root}\nVersion: {version}\nRestart Bluewater through tools/start_bluewater.bat.", flush=True)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
