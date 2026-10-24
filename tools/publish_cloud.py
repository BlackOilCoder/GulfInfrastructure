"""Stage and publish immutable GCS datasets. Run --help before publishing anything."""
import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import copy
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from bathymetry import Bathymetry, default_directory, package_path
from config import ROOT, data_directory, structures_file
from storage import CloudDatasets, GCSObjects, KINDS, object_name


def release_id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_file(root, name):
    object_name(name)
    result = (root / name).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError(f"Missing or invalid published file: {name}")
    return result


def references(value):
    if isinstance(value, dict):
        if isinstance(value.get("file"), str):
            yield value["file"]
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def inventory(kind, root):
    index_name = "manifest.json" if kind in {"infra", "currents"} else "index.json"
    index = json.loads(local_file(root, index_name).read_text(encoding="utf-8"))
    names = {index_name}
    if kind == "infra":
        # Legacy monolithic contour snapshots were replaced by the independent
        # contour tile index. Never advertise those removed files in a cloud release.
        for legacy in ("contours1000", "contours500"):
            index["datasets"].pop(legacy, None)
        if not {"platforms", "pipelines", "wells", "platformStructures", "talosAssets"} <= index["datasets"].keys():
            raise ValueError("Infrastructure publication must include all map datasets and Talos metadata.")
        for entry in index["datasets"].values():
            path = local_file(root, entry["file"])
            if digest(path) != entry["sha256"]:
                raise ValueError(f"Snapshot checksum mismatch: {entry['file']}")
            names.add(entry["file"])
    elif kind == "production":
        for group in ("wells", "platforms"):
            count = index[group]["shards"]
            if not isinstance(count, int) or not 1 <= count <= 4096:
                raise ValueError("Invalid production shard count.")
            names.update(f"{group}/{number}.json" for number in range(count))
    elif kind == "currents":
        if not index.get("sets") or not all(group.get("frames") for group in index["sets"]):
            raise ValueError("The currents manifest must contain nonempty frame sets.")
        names.update(references(index))
    elif kind == "contours":
        if not index.get("tiers"):
            raise ValueError("Contour tiers are missing.")
        for tier in index["tiers"]:
            names.update(f"{tier['id']}/{tier['z']}/{key}.json" for key in tier["tiles"])
    for name in sorted(names):
        path = local_file(root, name)
        if name.endswith(".json"):
            json.loads(path.read_text(encoding="utf-8"))
        elif name.endswith(".png"):
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
        else:
            raise ValueError(f"Unexpected public dataset format: {name}")
    return index, names


def upload_file(objects, key, path, content_type):
    blob = objects.blob(key)
    blob.chunk_size = 8*1024*1024
    sha = digest(path)
    blob.metadata = {"sha256": sha}
    blob.cache_control = "private, max-age=31536000, immutable"
    if path.stat().st_size > 100*1024*1024:
        print(f"Uploading {path.name}: {path.stat().st_size/1e6:.0f} MB", flush=True)
    blob.upload_from_filename(str(path), content_type=content_type, if_generation_match=0, timeout=600)
    return {"object": key, "size": path.stat().st_size, "sha256": sha, "contentType": content_type}


def publish_data(objects, kind, root, expected, structures=None, before_activate=None):
    index, names = inventory(kind, root)
    version = release_id()
    prefix = f"releases/{kind}/{version}"
    files = {}
    def upload(name):
        index_name = "manifest.json" if kind in {"infra", "currents"} else "index.json"
        if name == index_name:
            payload = json.dumps(index).encode()
            key = f"{prefix}/files/{name}"
            objects.put_new(key, payload, "application/json")
            return name, {"object": key, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                          "contentType": "application/json"}
        path = local_file(root, name)
        media_type = "image/png" if name.endswith(".png") else "application/json"
        entry = upload_file(objects, f"{prefix}/files/{name}", path, media_type)
        compressed = path.with_name(path.name + ".gz")
        if compressed.is_file():
            with gzip.open(compressed, "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise ValueError(f"Stale compressed snapshot: {name}")
            entry["gzip"] = upload_file(objects, f"{prefix}/files/{name}.gz", compressed, "application/gzip")
        return name, entry
    with ThreadPoolExecutor(max_workers=8) as pool:
        for completed, (name, entry) in enumerate(pool.map(upload, sorted(names)), 1):
            files[name] = entry
            if completed % 100 == 0 or completed == len(names):
                print(f"{kind}: uploaded {completed}/{len(names)} files", flush=True)
    if kind == "infra" and structures and structures.is_file():
        files["platform-structures.js"] = upload_file(objects, f"{prefix}/files/platform-structures.js", structures, "text/javascript")
    catalog = {"schemaVersion": 1, "kind": kind, "release": version,
               "generated": dt.datetime.now(dt.timezone.utc).isoformat(), "index": index, "files": files}
    objects.put_new(f"{prefix}/catalog.json", json.dumps(catalog).encode(), "application/json")
    if before_activate:
        before_activate()
    # An overlapping refresh cannot overwrite a release published since this run began.
    objects.activate(f"active/{kind}.json", {"schemaVersion": 1, "release": version}, expected)
    print(f"Published {kind}: {version} ({len(files)} files)")


def publish_bathymetry(root, rasters, tiles):
    _, expected = rasters.pointer("active.json")
    original = Bathymetry(root).manifest()
    data = copy.deepcopy(original)
    version = release_id()
    data["upstreamVersion"], data["version"] = original["version"], version
    uploaded = {}
    for item, field in [(source, field) for source in data["sources"] for field in ("depth", "hillshade")] + [(data["datumGrid"], "file")]:
        name = item[field]
        if name not in uploaded:
            key = f"packages/{version}/{object_name(name)}"
            upload_file(rasters, key, local_file(root, name), "image/tiff")
            uploaded[name] = key
        item[field] = uploaded[name]
    data["datumGrid"]["sha256"] = digest(local_file(root, original["datumGrid"]["file"]))
    db_path = package_path(root, original["tiles"]["file"])
    if db_path.is_file():
        with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as database:
            cursor = database.execute("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles")
            completed = 0
            def upload_tile(tile):
                zoom, col, tms_row, payload = tile
                tiles.put_new(f"hillshade/{version}/{zoom}/{col}/{2**zoom-1-tms_row}.png", payload, "image/png")
            with ThreadPoolExecutor(max_workers=8) as pool:
                while batch := cursor.fetchmany(100):
                    list(pool.map(upload_tile, batch))
                    completed += len(batch)
                    print(f"Hillshade tiles uploaded: {completed}", flush=True)
    data["tiles"].pop("file", None)  # Cloud runtime never opens a shared SQLite file.
    rasters.put_new(f"packages/{version}/manifest.json", json.dumps(data).encode(), "application/json")
    rasters.activate("active.json", {"schemaVersion": 1, "release": version}, expected)
    print(f"Published bathymetry: {version}")


def build_and_publish(objects, kind, builder_args, work_dir, lease=None):
    _, expected = objects.pointer(f"active/{kind}.json")
    catalog = CloudDatasets(objects).release(kind)
    # Hydrate the complete previous release so partial updates preserve other data.
    with tempfile.TemporaryDirectory(prefix="bluewater-publish-", dir=work_dir) as temporary:
        root = Path(temporary)
        data = root / "data"
        output = data if kind == "infra" else data / kind
        output.mkdir(parents=True)
        structures = root / "platform-structures.js"
        for name, entry in catalog["files"].items():
            target = structures if name == "platform-structures.js" else output / object_name(name)
            if not target.resolve().is_relative_to(root.resolve()):
                raise ValueError("Invalid staged dataset path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            objects.blob(entry["object"]).download_to_filename(str(target), timeout=600)
            if digest(target) != entry["sha256"]:
                raise ValueError(f"Downloaded dataset checksum mismatch: {name}")
            if entry.get("gzip"):
                compressed = target.with_name(target.name + ".gz")
                objects.blob(entry["gzip"]["object"]).download_to_filename(str(compressed), timeout=600)
                if digest(compressed) != entry["gzip"]["sha256"]:
                    raise ValueError(f"Downloaded gzip checksum mismatch: {name}")
        environment = dict(os.environ, BLUEWATER_DATA_DIR=str(data), BLUEWATER_STRUCTURES_FILE=str(structures))
        scripts = {"infra": "build_snapshots.py", "production": "build_production.py",
                   "currents": "build_currents.py", "contours": "build_contours.py"}
        subprocess.run([sys.executable, "-u", str(ROOT / "tools" / scripts[kind]), *builder_args],
                       env=environment, check=True)
        publish_data(objects, kind, output, expected, structures, lease.check if lease else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    seed = actions.add_parser("seed-data", help="Publish an existing complete local dataset")
    seed.add_argument("--kind", required=True, choices=sorted(KINDS))
    seed.add_argument("--directory", type=Path, help="Dataset folder (not its parent)")
    seed.add_argument("--structures-file", type=Path, default=structures_file())
    bathy = actions.add_parser("seed-bathymetry", help="Upload rasters and export existing MBTiles to PNG objects")
    bathy.add_argument("--directory", type=Path, default=default_directory())
    build = actions.add_parser("build", help="Build in isolation, validate, upload, then activate")
    build.add_argument("--kind", required=True, choices=sorted(KINDS))
    build.add_argument("--work-dir", type=Path, default=None, help="Existing scratch directory; large builds need adequate disk")
    build.add_argument("builder_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "seed-bathymetry":
        publish_bathymetry(args.directory.resolve(), GCSObjects(os.environ.get("BLUEWATER_BATHYMETRY_BUCKET")),
                          GCSObjects(os.environ.get("BLUEWATER_TILE_BUCKET")))
    else:
        objects = GCSObjects(os.environ.get("BLUEWATER_DATA_BUCKET"))
        if args.action == "seed-data":
            root = args.directory or (data_directory() if args.kind == "infra" else data_directory() / args.kind)
            _, expected = objects.pointer(f"active/{args.kind}.json")
            publish_data(objects, args.kind, root.resolve(), expected, args.structures_file)
        else:
            builder_args = args.builder_args
            if builder_args[:1] == ["--"]:
                builder_args = builder_args[1:]
            if "--help" in builder_args or "-h" in builder_args:
                parser.error("Use the builder directly for help; cloud builds always publish completed data.")
            from job_lease import DatasetLease
            with DatasetLease(objects, args.kind) as lease:
                if lease.generation is not None:
                    build_and_publish(objects, args.kind, builder_args, args.work_dir, lease)


if __name__ == "__main__":
    main()
