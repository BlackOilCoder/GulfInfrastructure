#!/usr/bin/env python3
"""
Bluewater depth-contour tile builder.

Downloads the BOEM deepwater depth contours once, simplifies them for each map-scale tier,
cuts them into small Web Mercator tiles and writes them under data/contours/. The map then
loads only the tiles in view, so memory use stays small at any zoom level.

Run from the app folder (the one containing index.html):
    python tools/build_contours.py
    python tools/build_contours.py --transport powershell     # if Python cannot verify HTTPS
    python tools/build_contours.py --ca-bundle path\\to\\company-root.pem

The contours are static, so this only needs re-running if BOEM republishes them.
Only the Python standard library is used.

Tiers (scale ranges match index.html):
    t1000   1,000 ft contours   zoomed out past 1:1,500,000
    t500      500 ft contours   1:1,500,000 - 1:400,000
    t100m     100 ft contours   1:400,000 - 1:100,000   (moderately simplified)
    t100      100 ft contours   closer than 1:100,000   (near full detail)

Tile format (JSON, one file per tile): {"d":[depth_ft,...], "l":[[x0,y0,dx1,dy1,...],...]}
Coordinates are integers 0..4096 across the tile (y downwards), delta-encoded after the
first point. Tiles are kept fairly large to limit the file count (the folder may sit in OneDrive).
"""
import argparse
import datetime as dt
import gzip
import json
import math
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array

SERVICE = "https://gis.boem.gov/server/rest/services/BOEM_BSEE/GOA_Deepwater_Bathymetry_Contours/FeatureServer"
ORIGIN = 20037508.342789244          # Web Mercator half-world (m)
WORLD = 2 * ORIGIN
EXTENT = 4096                        # integer units across one tile

# tolerance = Douglas-Peucker tolerance in metres (about half a screen pixel at the tier's
# most zoomed-in scale). z = tile zoom level (tile width ~ 40,075 km / 2^z at the equator).
TIERS = [
    {"id": "t1000", "layer": 0, "interval": 1000, "indexEvery": 5000, "labelAll": False,
     "z": 6, "tolerance": 200.0, "minScale": 0, "maxScale": 1500000},
    {"id": "t500", "layer": 1, "interval": 500, "indexEvery": 2500, "labelAll": False,
     "z": 8, "tolerance": 50.0, "minScale": 1500000, "maxScale": 400000},
    {"id": "t100m", "layer": 2, "interval": 100, "indexEvery": 500, "labelAll": False,
     "z": 10, "tolerance": 12.0, "minScale": 400000, "maxScale": 100000},
    {"id": "t100", "layer": 2, "interval": 100, "indexEvery": 500, "labelAll": True,
     "z": 11, "tolerance": 3.0, "minScale": 100000, "maxScale": 0},
]

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "contours"
UA = "Bluewater-contour-builder/1.0"


# --------------------------------------------------------------------------- HTTPS transport
class Transport:
    """POSTs form data; handles corporate TLS inspection like build_snapshots.py."""

    def __init__(self, mode="auto", ca_bundle=None):
        self.mode = mode
        self.ca_bundle = ca_bundle or os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        self.use_powershell = mode == "powershell"
        self.context = None if self.use_powershell else self._context()

    def _context(self):
        try:
            import truststore
            print("TLS: using Windows/OS certificate store via truststore")
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            pass
        ctx = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        if sys.platform == "win32":
            for store in ("ROOT", "CA"):
                try:
                    for cert, encoding, trust in ssl.enum_certificates(store):
                        if encoding == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in trust):
                            try:
                                ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
                            except ssl.SSLError:
                                pass
                except OSError:
                    pass
        if self.ca_bundle:
            ctx.load_verify_locations(cafile=self.ca_bundle)
        return ctx

    def post(self, url, params):
        body = urllib.parse.urlencode(params).encode()
        if self.use_powershell:
            return self._powershell(url, body)
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300, context=self.context) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            if (isinstance(exc.reason, ssl.SSLCertVerificationError) and self.mode == "auto"
                    and sys.platform == "win32" and self._exe()):
                print("TLS: Python could not verify the certificate; switching to PowerShell.")
                self.use_powershell = True
                return self._powershell(url, body)
            raise

    @staticmethod
    def _exe():
        return shutil.which("pwsh") or shutil.which("powershell")

    def _powershell(self, url, body):
        exe = self._exe()
        if not exe:
            raise RuntimeError("PowerShell not found")
        with tempfile.TemporaryDirectory() as tmp:
            body_file, out_file = pathlib.Path(tmp, "body.txt"), pathlib.Path(tmp, "out.json")
            body_file.write_bytes(body)
            script = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                      "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                      f"Invoke-WebRequest -UseBasicParsing -Method Post -Uri '{url}' -UserAgent '{UA}' "
                      f"-ContentType 'application/x-www-form-urlencoded' -InFile '{body_file}' "
                      f"-OutFile '{out_file}' -TimeoutSec 300")
            result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True)
            if result.returncode != 0 or not out_file.exists():
                raise RuntimeError(f"PowerShell request failed: {result.stderr.strip() or result.stdout.strip()}")
            return out_file.read_bytes()


TRANSPORT = None


def request_json(url, params, retries=4):
    last = None
    for attempt in range(1, retries + 1):
        try:
            payload = json.loads(TRANSPORT.post(url, {**params, "f": "json"}).decode("utf-8-sig"))
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise SystemExit("Certificate verification failed. Try:  pip install truststore   or   "
                                 "--transport powershell   or   --ca-bundle company-root.pem")
            last = exc
        except (TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last = exc
        if attempt == retries:
            raise last
        print(f"    retry {attempt}/{retries - 1} in {2 ** attempt}s ({last})", file=sys.stderr)
        time.sleep(2 ** attempt)


# --------------------------------------------------------------------------- geometry
def simplify(pts, tol):
    """Douglas-Peucker on a list of (x, y) tuples."""
    n = len(pts)
    if n < 3 or tol <= 0:
        return pts
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if l2 == 0:
                ex, ey = px - ax, py - ay
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / l2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                ex, ey = ax + t * dx - px, ay + t * dy - py
            d = ex * ex + ey * ey
            if d > best:
                best, bi = d, i
        if best > tol2:
            keep[bi] = 1
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def split_by_tiles(pts, z):
    """Cuts a polyline at tile edges. Yields ((tx, ty), [points]) pieces."""
    size = WORLD / (2 ** z)

    def tile_of(x, y):
        return int((x + ORIGIN) // size), int((ORIGIN - y) // size)

    cur_tile = tile_of(*pts[0])
    cur = [pts[0]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        t1 = tile_of(x1, y1)
        if t1 == cur_tile:
            cur.append((x1, y1))
            continue
        ts = []
        for g0, g1 in (((x0 + ORIGIN) / size, (x1 + ORIGIN) / size), ((ORIGIN - y0) / size, (ORIGIN - y1) / size)):
            if g1 != g0:
                lo, hi = sorted((g0, g1))
                for k in range(math.floor(lo) + 1, math.floor(hi) + 1):
                    t = (k - g0) / (g1 - g0)
                    if 0 < t < 1:
                        ts.append(t)
        ts.sort()
        if not ts:  # endpoint lies exactly on a tile edge
            if len(cur) >= 2:
                yield cur_tile, cur
            cur_tile, cur = t1, [(x0, y0), (x1, y1)]
            continue
        bounds = ts + [1.0]
        for i, t in enumerate(ts):
            cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            cur.append((cx, cy))
            if len(cur) >= 2:
                yield cur_tile, cur
            tm = (t + bounds[i + 1]) / 2
            cur_tile = tile_of(x0 + (x1 - x0) * tm, y0 + (y1 - y0) * tm)
            cur = [(cx, cy)]
        cur.append((x1, y1))
    if len(cur) >= 2:
        yield cur_tile, cur


def encode_piece(points, tile, z):
    size = WORLD / (2 ** z)
    min_x = tile[0] * size - ORIGIN
    max_y = ORIGIN - tile[1] * size
    k = EXTENT / size
    out = array("i")
    px = py = None
    count = 0
    for x, y in points:
        qx, qy = round((x - min_x) * k), round((max_y - y) * k)
        if px is None:
            out.extend((qx, qy))
        elif qx != px or qy != py:
            out.extend((qx - px, qy - py))
        else:
            continue
        px, py = qx, qy
        count += 1
    return out if count >= 2 else None


def to_mercator(lon, lat):
    lat = max(min(lat, 85.0511), -85.0511)
    return lon * math.pi / 180 * 6378137.0, math.log(math.tan(math.pi / 4 + lat * math.pi / 360)) * 6378137.0


# --------------------------------------------------------------------------- build
def find_depth_field(fields):
    numeric = [f for f in fields if f.get("type") in ("esriFieldTypeDouble", "esriFieldTypeSingle",
                                                      "esriFieldTypeInteger", "esriFieldTypeSmallInteger",
                                                      "esriFieldTypeBigInteger")]
    for test in (lambda f: f["name"].lower() in ("contour", "depth", "elev", "elevation"),
                 lambda f: any(w in (f["name"] + " " + (f.get("alias") or "")).lower() for w in ("contour", "depth", "elev")),
                 lambda f: not f["name"].lower().startswith(("objectid", "fid", "shape", "id"))):
        match = next((f for f in numeric if test(f)), None)
        if match:
            return match["name"]
    return None


def build_layer(layer_id, tiers, chunk):
    url = f"{SERVICE}/{layer_id}"
    meta = request_json(url, {})
    depth_field = find_depth_field(meta.get("fields") or [])
    if not depth_field:
        raise SystemExit(f"Layer {layer_id}: no numeric depth field in {[f['name'] for f in meta.get('fields', [])]}")
    ids = sorted(request_json(f"{url}/query", {"where": "1=1", "returnIdsOnly": "true"}).get("objectIds") or [])
    offset = min(t["tolerance"] for t in tiers)
    print(f"[layer {layer_id}] {meta.get('name', '')}: {len(ids):,} lines, depth field '{depth_field}', "
          f"tiers {', '.join(t['id'] for t in tiers)}")

    tiles = {t["id"]: {} for t in tiers}
    stats = {t["id"]: [0, 0] for t in tiers}   # pieces, vertices
    in_vertices = 0
    started = time.time()
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        result = request_json(f"{url}/query", {
            "objectIds": ",".join(map(str, batch)),
            "outFields": depth_field,
            "returnGeometry": "true",
            "outSR": "102100",
            "maxAllowableOffset": str(offset),   # server-side generalisation shrinks the download
            "geometryPrecision": "1",
        })
        wkid = (result.get("spatialReference") or {}).get("latestWkid") or (result.get("spatialReference") or {}).get("wkid")
        geographic = wkid == 4326
        for feat in result.get("features", []):
            value = (feat.get("attributes") or {}).get(depth_field)
            paths = (feat.get("geometry") or {}).get("paths") or []
            if value is None or not paths:
                continue
            depth = int(round(abs(float(value))))
            for path in paths:
                pts = [to_mercator(p[0], p[1]) if geographic else (p[0], p[1]) for p in path if len(p) >= 2]
                if len(pts) < 2:
                    continue
                in_vertices += len(pts)
                for tier in tiers:
                    simple = simplify(pts, tier["tolerance"])
                    for tile, piece in split_by_tiles(simple, tier["z"]):
                        encoded = encode_piece(piece, tile, tier["z"])
                        if encoded is None:
                            continue
                        tiles[tier["id"]].setdefault(tile, []).append((depth, encoded))
                        stats[tier["id"]][0] += 1
                        stats[tier["id"]][1] += len(encoded) // 2
        done = min(start + chunk, len(ids))
        rate = done / max(time.time() - started, 1e-6)
        print(f"    {done:,}/{len(ids):,} lines · {in_vertices:,} vertices · ~{(len(ids) - done) / max(rate, 1e-6):,.0f}s left",
              end="\r", flush=True)
    print()
    for tier in tiers:
        pieces, verts = stats[tier["id"]]
        print(f"    {tier['id']}: {len(tiles[tier['id']]):,} tiles, {pieces:,} pieces, {verts:,} vertices")
    return tiles


def write_tier(tier, tiles, staging):
    folder = staging / tier["id"] / str(tier["z"])
    raw_total = gz_total = 0
    keys = []
    for (tx, ty), lines in sorted(tiles.items()):
        lines.sort(key=lambda item: item[0])
        payload = json.dumps({"d": [d for d, _ in lines], "l": [list(a) for _, a in lines]},
                             separators=(",", ":")).encode()
        path = folder / str(tx) / f"{ty}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        raw_total += len(payload)
        gz_total += len(gzip.compress(payload, 6))
        keys.append(f"{tx}/{ty}")
    print(f"[{tier['id']}] {len(keys):,} tiles · {raw_total / 1e6:,.1f} MB (~{gz_total / 1e6:,.1f} MB if served compressed)")
    return keys


def main():
    global TRANSPORT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["auto", "python", "powershell"], default="auto")
    parser.add_argument("--ca-bundle", help="extra PEM file with your company's root certificate")
    parser.add_argument("--chunk", type=int, default=25, help="contour lines per request (default 25)")
    args = parser.parse_args()
    TRANSPORT = Transport(args.transport, args.ca_bundle)

    staging = OUT.parent / ".contours.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    tier_meta = []
    for layer_id in sorted({t["layer"] for t in TIERS}):
        tiers = [t for t in TIERS if t["layer"] == layer_id]
        tiles = build_layer(layer_id, tiers, args.chunk)
        for tier in tiers:
            keys = write_tier(tier, tiles.pop(tier["id"]), staging)
            tier_meta.append({k: tier[k] for k in ("id", "interval", "indexEvery", "labelAll", "z", "minScale", "maxScale")}
                             | {"extent": EXTENT, "tiles": keys})

    generated = dt.datetime.now(dt.timezone.utc)
    index = {
        "generated": generated.isoformat(timespec="seconds"),
        "version": generated.strftime("%Y%m%dT%H%M%S"),
        "source": SERVICE,
        "units": "feet",
        "tiers": sorted(tier_meta, key=lambda t: [x["id"] for x in TIERS].index(t["id"])),
    }
    (staging / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    if OUT.exists():
        shutil.rmtree(OUT)
    staging.rename(OUT)
    print(f"done: {OUT / 'index.json'}")


if __name__ == "__main__":
    main()
