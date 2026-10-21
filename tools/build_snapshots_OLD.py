#!/usr/bin/env python3
"""
Bluewater snapshot builder.

Downloads the BOEM/BSEE GOA_Layers feature services (platforms, wells, pipelines)
into compact static JSON files that the map loads locally instead of querying the
live services. Also converts platform-structures.js into data/platform-structures.json.

Run from the app folder (the one containing index.html):

    python tools/build_snapshots.py                 # refresh everything
    python tools/build_snapshots.py pipelines       # refresh one dataset
    python tools/build_snapshots.py --structures-only
    python tools/build_snapshots.py contours1000 contours500   # depth contours only

The 100 ft depth contours are intentionally not snapshotted (Gulf-wide they are very large);
the map always reads them live, and only when zoomed in.

HTTPS on corporate networks
---------------------------
Networks with TLS inspection re-sign HTTPS traffic with a company root certificate
that Python's own certificate bundle does not know about. This script handles it by:
  1. using the `truststore` package (Windows certificate store) if it is installed,
  2. otherwise loading the Windows ROOT/CA certificate stores explicitly,
  3. plus any bundle given with --ca-bundle (or SSL_CERT_FILE / REQUESTS_CA_BUNDLE),
  4. and, on Windows, falling back automatically to PowerShell's HTTP client
     (which validates with Windows itself) if Python still cannot verify the site.
Use --transport powershell to skip straight to step 4.

Outputs (in ./data):
    platforms.json, wells.json, pipelines.json, contours1000.json, contours500.json,
    platform-structures.json
    *.json.gz            pre-compressed copies (used by tools/serve.py)
    manifest.json        dataset versions (sha256) the browser uses for cache checks

Only the Python standard library is required.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "https://gis.boem.gov/server/rest/services/BOEM_BSEE/GOA_Layers/FeatureServer"
CONTOUR_SERVICE = "https://gis.boem.gov/server/rest/services/BOEM_BSEE/GOA_Deepwater_Bathymetry_Contours/FeatureServer"

# chunk = features per request (contour lines are long, so they use smaller batches).
DATASETS = {
    "platforms": {"service": SERVICE, "layer": 0, "decimals": 6, "chunk": 500},
    "wells": {"service": SERVICE, "layer": 1, "decimals": 6, "chunk": 500},
    "pipelines": {"service": SERVICE, "layer": 2, "decimals": 6, "chunk": 500},
    # BOEM depth contours in feet. 5 decimals ~ 1 m, plenty for display at these intervals.
}

# Pipeline product codes the app never displays; dropped at build time to save size.
IGNORED_PIPELINE_CODES = {"ACID", "AIR", "LGER", "LPRO", "METH", "SPRE", "SULF", "TEST", "TOW"}

# Esri REST field types -> ArcGIS Maps SDK for JavaScript field types.
FIELD_TYPES = {
    "esriFieldTypeOID": "oid",
    "esriFieldTypeString": "string",
    "esriFieldTypeInteger": "integer",
    "esriFieldTypeSmallInteger": "small-integer",
    "esriFieldTypeBigInteger": "big-integer",
    "esriFieldTypeDouble": "double",
    "esriFieldTypeSingle": "single",
    "esriFieldTypeDate": "date",
    "esriFieldTypeDateOnly": "date-only",
    "esriFieldTypeTimeOnly": "time-only",
    "esriFieldTypeTimestampOffset": "timestamp-offset",
    "esriFieldTypeGlobalID": "global-id",
    "esriFieldTypeGUID": "guid",
}
SKIP_FIELD = re.compile(r"^(shape__|shape\.|st_length|st_area)", re.I)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "Bluewater-snapshot-builder/1.1"


# --------------------------------------------------------------------------- HTTPS transport
class Transport:
    """POSTs form data and returns the response bytes, via Python or PowerShell."""

    def __init__(self, mode="auto", ca_bundle=None):
        self.mode = mode
        self.ca_bundle = ca_bundle or os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        self.use_powershell = mode == "powershell"
        self.context = None if self.use_powershell else self._build_context()

    def _build_context(self):
        try:
            import truststore  # optional: pip install truststore
            print("TLS: using Windows/OS certificate store via truststore")
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            pass
        ctx = ssl.create_default_context()
        # Python 3.13+ enables strict X.509 checks that many inspection proxies' certs fail.
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        if sys.platform == "win32":
            loaded = 0
            for store in ("ROOT", "CA"):
                try:
                    for cert, encoding, trust in ssl.enum_certificates(store):
                        if encoding != "x509_asn":
                            continue
                        if trust is not True and "1.3.6.1.5.5.7.3.1" not in trust:  # server auth
                            continue
                        try:
                            ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
                            loaded += 1
                        except ssl.SSLError:
                            pass
                except OSError:
                    pass
            print(f"TLS: loaded {loaded} certificates from the Windows ROOT/CA stores")
        if self.ca_bundle:
            ctx.load_verify_locations(cafile=self.ca_bundle)
            print(f"TLS: added CA bundle {self.ca_bundle}")
        return ctx

    def post(self, url, params):
        body = urllib.parse.urlencode(params).encode()
        if self.use_powershell:
            return self._post_powershell(url, body)
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120, context=self.context) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError) and self._can_fallback():
                print("TLS: Python could not verify the certificate (likely corporate TLS inspection);\n"
                      "     switching to PowerShell, which uses Windows certificate validation.")
                self.use_powershell = True
                return self._post_powershell(url, body)
            raise

    def _can_fallback(self):
        return self.mode == "auto" and sys.platform == "win32" and self._powershell_exe() is not None

    @staticmethod
    def _powershell_exe():
        return shutil.which("pwsh") or shutil.which("powershell")

    def _post_powershell(self, url, body):
        exe = self._powershell_exe()
        if not exe:
            raise RuntimeError("PowerShell not found; cannot use the PowerShell transport.")
        with tempfile.TemporaryDirectory() as tmp:
            body_file = pathlib.Path(tmp, "body.txt")
            out_file = pathlib.Path(tmp, "out.json")
            body_file.write_bytes(body)
            script = (
                "$ErrorActionPreference='Stop';"
                "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                "$ProgressPreference='SilentlyContinue';"
                f"Invoke-WebRequest -UseBasicParsing -Method Post -Uri '{url}' "
                f"-UserAgent '{UA}' -ContentType 'application/x-www-form-urlencoded' "
                f"-InFile '{body_file}' -OutFile '{out_file}' -TimeoutSec 120"
            )
            result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True)
            if result.returncode != 0 or not out_file.exists():
                raise RuntimeError(f"PowerShell request failed: {result.stderr.strip() or result.stdout.strip()}")
            return out_file.read_bytes()


TRANSPORT = None


def request_json(url, params, retries=4):
    for attempt in range(1, retries + 1):
        try:
            payload = json.loads(TRANSPORT.post(url, {**params, "f": "json"}).decode("utf-8-sig"))
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise SystemExit(
                    f"\nCertificate verification failed for {url}\n"
                    "Fix options (any one):\n"
                    "  pip install truststore                     (uses the Windows certificate store)\n"
                    "  python tools/build_snapshots.py --transport powershell\n"
                    "  python tools/build_snapshots.py --ca-bundle path\\to\\company-root.pem")
            last = exc
        except (TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last = exc
        if attempt == retries:
            raise last
        wait = 2 ** attempt
        print(f"    retry {attempt}/{retries - 1} in {wait}s ({last})", file=sys.stderr)
        time.sleep(wait)


# --------------------------------------------------------------------------- dataset build
def round_coords(value, decimals):
    if isinstance(value, list):
        return [round_coords(v, decimals) for v in value]
    return round(value, decimals)


def dedupe_path(path):
    out = []
    for pt in path:
        if not out or pt[:2] != out[-1][:2]:
            out.append(pt[:2])
    return out


def build_dataset(name, cfg, chunk=None):
    chunk = chunk or cfg.get("chunk", 500)
    url = f"{cfg.get('service', SERVICE)}/{cfg['layer']}"
    print(f"[{name}] reading layer metadata")
    meta = request_json(url, {})
    oid_field = meta.get("objectIdField") or next(
        (f["name"] for f in meta["fields"] if f["type"] == "esriFieldTypeOID"), "OBJECTID")
    fields = []
    for f in meta["fields"]:
        sdk_type = FIELD_TYPES.get(f["type"])
        if not sdk_type or (SKIP_FIELD.match(f["name"]) and f["name"] != oid_field):
            continue
        field = {"name": f["name"], "alias": f.get("alias") or f["name"], "type": sdk_type}
        if f.get("length") and sdk_type == "string":
            field["length"] = f["length"]
        fields.append(field)
    names = [f["name"] for f in fields]

    where = "1=1"
    if name == "pipelines":
        where = "PROD_CODE IS NOT NULL AND PROD_CODE NOT IN ({})".format(
            ",".join(f"'{c}'" for c in sorted(IGNORED_PIPELINE_CODES)))

    print(f"[{name}] listing object IDs")
    ids = request_json(f"{url}/query", {"where": where, "returnIdsOnly": "true"}).get("objectIds") or []
    ids.sort()
    print(f"[{name}] {len(ids):,} features; downloading in batches of {chunk}")

    geometry_type = "point" if meta.get("geometryType") == "esriGeometryPoint" else "polyline"
    rows, geoms, skipped = [], [], 0
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        result = request_json(f"{url}/query", {
            "objectIds": ",".join(map(str, batch)),
            "outFields": ",".join(names),
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": str(cfg["decimals"]),
        })
        for feat in result.get("features", []):
            g = feat.get("geometry") or {}
            if geometry_type == "point":
                if g.get("x") is None or g.get("y") is None or g.get("x") != g.get("x"):
                    skipped += 1
                    continue
                geom = [round(g["x"], cfg["decimals"]), round(g["y"], cfg["decimals"])]
            else:
                paths = [dedupe_path(round_coords(p, cfg["decimals"])) for p in g.get("paths") or []]
                paths = [p for p in paths if len(p) >= 2]
                if not paths:
                    skipped += 1
                    continue
                geom = paths
            attrs = feat.get("attributes") or {}
            row = []
            for n in names:
                v = attrs.get(n)
                if isinstance(v, str):
                    v = v.strip() or None
                row.append(v)
            rows.append(row)
            geoms.append(geom)
        done = min(start + chunk, len(ids))
        print(f"    {done:,}/{len(ids):,}", end="\r", flush=True)
    print()
    if skipped:
        print(f"[{name}] skipped {skipped:,} features without usable geometry")

    # Drop columns that are empty for every record (keeps the object-ID field).
    keep = [i for i, n in enumerate(names) if n == oid_field or any(r[i] is not None for r in rows)]
    fields = [fields[i] for i in keep]
    rows = [[r[i] for i in keep] for r in rows]

    return {
        "name": name,
        "source": url,
        "captured": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "geometryType": geometry_type,
        "spatialReference": {"wkid": 4326},
        "objectIdField": oid_field,
        "displayField": meta.get("displayField"),
        "fields": fields,
        "rows": rows,
        "geoms": geoms,
    }


def build_structures():
    src = ROOT / "platform-structures.js"
    if not src.exists():
        print("[platformStructures] platform-structures.js not found; skipped")
        return None
    text = src.read_text(encoding="utf-8")
    captured = re.search(r"captured\s+(\d{4}-\d{2}-\d{2})", text)
    fields = re.search(r"window\.BSEE_PLATFORM_FIELDS\s*=\s*(\[.*?\]);", text, re.S)
    rows = re.search(r"window\.BSEE_PLATFORM_STRUCTURES\s*=\s*(\[.*\]);", text, re.S)
    if not (fields and rows):
        raise SystemExit("Could not parse platform-structures.js")
    data = {
        "name": "platformStructures",
        "source": "https://www.data.bsee.gov/Platform/PlatformStructures/Default.aspx",
        "captured": captured.group(1) if captured else None,
        "fields": json.loads(fields.group(1)),
        "rows": json.loads(rows.group(1)),
    }
    print(f"[platformStructures] {len(data['rows']):,} structure records")
    return data


def write_dataset(key, data, manifest):
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    filename = f"{'platform-structures' if key == 'platformStructures' else key}.json"
    (DATA / filename).write_bytes(raw)
    (DATA / (filename + ".gz")).write_bytes(gzip.compress(raw, 9))
    manifest["datasets"][key] = {
        "file": filename,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "gzipBytes": (DATA / (filename + ".gz")).stat().st_size,
        "count": len(data["rows"]),
        "captured": data.get("captured"),
    }
    m = manifest["datasets"][key]
    print(f"[{key}] wrote {filename}: {m['bytes'] / 1e6:.1f} MB ({m['gzipBytes'] / 1e6:.1f} MB gzipped)")


def main():
    global TRANSPORT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("datasets", nargs="*", help=f"datasets to refresh: {', '.join(DATASETS)} (default: all)")
    parser.add_argument("--structures-only", action="store_true", help="only convert platform-structures.js")
    parser.add_argument("--transport", choices=["auto", "python", "powershell"], default="auto",
                        help="HTTP client: auto (Python, PowerShell fallback on Windows), python, or powershell")
    parser.add_argument("--ca-bundle", help="extra PEM file with your company's root certificate")
    args = parser.parse_args()
    unknown = [d for d in args.datasets if d not in DATASETS]
    if unknown:
        parser.error(f"unknown dataset(s): {', '.join(unknown)}")

    DATA.mkdir(exist_ok=True)
    manifest_path = DATA / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"datasets": {}}
    manifest.setdefault("datasets", {})

    structures = build_structures()
    if structures:
        write_dataset("platformStructures", structures, manifest)

    if not args.structures_only:
        TRANSPORT = Transport(args.transport, args.ca_bundle)
        try:
            for name in args.datasets or DATASETS:
                write_dataset(name, build_dataset(name, DATASETS[name]), manifest)
        finally:
            manifest["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"manifest updated: {manifest_path}")
        return

    manifest["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"manifest updated: {manifest_path}")


if __name__ == "__main__":
    main()
