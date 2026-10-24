#!/usr/bin/env python3
"""
Bluewater production-data builder (BSEE / BOEM public production data, 1996 onward).

Builds the local data behind the map's "Production Data" popup button:

  * Wells (by API number) from the OGOR-A well production files, one zip per year:
        https://www.data.boem.gov/Production/Files/ogora<YEAR>delimit.zip  (current year: ogoradelimit.zip)
    Completions are added up per well and month; rates = monthly volume / days in month.

  * Platforms (by Complex ID) from the "Production by Platform" raw data file on
        https://www.data.bsee.gov/Main/RawData.aspx
    which is the data behind BOEM's Production by Platform query (rates per complex/month).

Wells and platforms are kept separate; nothing links one to the other.

Run from the app folder (the one containing index.html):
    python tools/build_production.py                 # normal refresh (re-downloads recent years)
    python tools/build_production.py --full          # re-download every year
    python tools/build_production.py --wells-only
    python tools/build_production.py --platforms-only
    python tools/build_production.py --platform-url <url of the Production by Platform zip>
    python tools/build_production.py --transport powershell     # if Python cannot verify HTTPS

BSEE updates these files about twice a month, so a weekly or monthly schedule is plenty
(tools/update_production.bat works with Windows Task Scheduler).

Output (data/production/):
    index.json                         versions, coverage, shard counts
    wells/<n>.json (+ .json.gz)        n = int(12-digit API) % WELL_SHARDS
    platforms/<n>.json (+ .json.gz)    n = int(Complex ID) % PLATFORM_SHARDS
    cache/                             downloaded zips (reused between runs)

Only the Python standard library is used.
"""
import argparse
import calendar
import csv
import datetime as dt
import gzip
import io
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
import zipfile
from config import data_directory, production_cache_directory

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = data_directory() / "production"
CACHE = production_cache_directory()
UA = "Bluewater-production-builder/1.0"

FIRST_YEAR = 1996
BASE_MONTH = (1996, 1)             # month index 0
WELL_SHARDS = 512
PLATFORM_SHARDS = 64
BOE_MCF = 5.62                     # 5.62 Mcf gas = 1 bbl oil equivalent (30 CFR 203.73)

# Past years are published as ogora<YEAR>delimit.zip; the current (partial) year as ogoradelimit.zip.
# Both BOEM and BSEE host the files; candidates are tried in order until one is a valid zip.
OGORA_HOSTS = ["https://www.data.boem.gov/Production/Files/", "https://www.data.bsee.gov/Production/Files/"]
OGORA_URL = "https://www.data.boem.gov/Production/Files/ogora{year}delimit.zip"


def ogora_candidates(year, this_year):
    names = ["ogoradelimit.zip", f"ogora{year}delimit.zip"] if year == this_year else [f"ogora{year}delimit.zip"]
    return [host + name for name in names for host in OGORA_HOSTS]
RAW_DATA_PAGES = ["https://www.data.boem.gov/Main/RawData.aspx", "https://www.data.bsee.gov/Main/RawData.aspx"]

# OGOR-A record layout (BSEE field definitions), used when the delimited file has no header row.
OGORA_COLUMNS = ["LEASE_NUMBER", "COMPLETION_NAME", "PRODUCTION_DATE", "DAYS_ON_PROD", "PRODUCT_CODE",
                 "MON_O_PROD_VOL", "MON_G_PROD_VOL", "MON_WTR_PROD_VOL", "API_WELL_NUMBER", "WELL_STAT_CD",
                 "AREA_BLOCK", "OPERATOR_NUM", "SORT_NAME", "FIELD_NAME_CODE", "INJECTION_VOLUME",
                 "PROD_INTERVAL_CD", "FIRST_PROD_DATE"]
# Fixed-width positions (1-based start, length) for the same fields, as a last-resort parser.
OGORA_FIXED = {"PRODUCTION_DATE": (14, 6), "DAYS_ON_PROD": (20, 2), "MON_O_PROD_VOL": (23, 9),
               "MON_G_PROD_VOL": (32, 9), "MON_WTR_PROD_VOL": (41, 9), "API_WELL_NUMBER": (50, 12),
               "SORT_NAME": (77, 45)}


# --------------------------------------------------------------------------- HTTPS transport
class Transport:
    """Downloads over HTTPS; handles corporate TLS inspection like build_snapshots.py."""

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

    def download(self, url, dest):
        """GET url into dest (binary). Returns dest."""
        dest = pathlib.Path(dest)
        tmp = dest.with_name(dest.name + ".part")
        if self.use_powershell:
            self._powershell(url, tmp)
        else:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=600, context=self.context) as resp, open(tmp, "wb") as fh:
                    shutil.copyfileobj(resp, fh, 1 << 20)
            except urllib.error.URLError as exc:
                if (isinstance(exc.reason, ssl.SSLCertVerificationError) and self.mode == "auto"
                        and sys.platform == "win32" and self._exe()):
                    print("TLS: Python could not verify the certificate; switching to PowerShell.")
                    self.use_powershell = True
                    self._powershell(url, tmp)
                else:
                    raise
        os.replace(tmp, dest)
        return dest

    def get_text(self, url):
        with tempfile.TemporaryDirectory() as folder:
            path = self.download(url, pathlib.Path(folder) / "page.html")
            return path.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _exe():
        return shutil.which("pwsh") or shutil.which("powershell")

    def _powershell(self, url, out_file):
        exe = self._exe()
        if not exe:
            raise RuntimeError("PowerShell not found")
        script = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                  f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' -UserAgent '{UA}' "
                  f"-OutFile '{out_file}' -TimeoutSec 600")
        result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True)
        if result.returncode != 0 or not pathlib.Path(out_file).exists():
            raise RuntimeError(f"PowerShell download failed: {result.stderr.strip() or result.stdout.strip()}")


def is_valid_zip(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None and bool(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def fetch_zip(transport, urls, dest):
    """Tries each URL until one returns a valid zip. Bad downloads never replace a good cached copy."""
    dest = pathlib.Path(dest)
    trial = dest.with_name(dest.name + ".download")
    problems = []
    for url in urls:
        try:
            fetch(transport, url, trial)
        except urllib.error.HTTPError as exc:
            problems.append(f"{url}: HTTP {exc.code}")
            continue
        except Exception as exc:                      # noqa: BLE001
            problems.append(f"{url}: {exc}")
            continue
        if is_valid_zip(trial):
            os.replace(trial, dest)
            return url
        problems.append(f"{url}: not a zip (probably an error page)")
    try:
        trial.unlink()
    except OSError:
        pass
    raise RuntimeError("; ".join(problems))


def fetch(transport, url, dest, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return transport.download(url, dest)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise SystemExit("Certificate verification failed. Try:  pip install truststore   or   "
                                 "--transport powershell   or   --ca-bundle company-root.pem")
            last = exc
        except (TimeoutError, RuntimeError, OSError) as exc:
            last = exc
        if attempt < retries:
            print(f"    retry {attempt}/{retries - 1} in {2 ** attempt}s ({last})", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise last


# --------------------------------------------------------------------------- helpers
def month_index(yyyymm):
    y, m = divmod(int(yyyymm), 100)
    if not (1 <= m <= 12 and FIRST_YEAR <= y <= 2100):
        return None
    return (y - BASE_MONTH[0]) * 12 + (m - BASE_MONTH[1])


def index_to_ym(index):
    y, m = divmod(index, 12)
    return BASE_MONTH[0] + y, m + 1


def days_in(index):
    y, m = index_to_ym(index)
    return calendar.monthrange(y, m)[1]


def num(value):
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_api(value):
    """12-digit API key: digits only; 10 digits gets '00' sidetrack, 14 digits is trimmed."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits += "00"
    if len(digits) >= 12:
        return digits[:12]
    return None


def parse_month(value):
    """Accepts YYYYMM, YYYY-MM, MM/YYYY, MM/DD/YYYY, YYYY-MM-DD → YYYYMM int or None."""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})(\d{2})", text)
    if m:
        return int(text)
    m = re.match(r"(\d{4})[-/](\d{1,2})", text)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2))
    m = re.match(r"(\d{1,2})/(?:\d{1,2}/)?(\d{4})", text)
    if m:
        return int(m.group(2)) * 100 + int(m.group(1))
    return None


def sniff_rows(text_stream, sample):
    """Returns (rows iterator, header list or None)."""
    first = sample.splitlines()[0] if sample else ""
    delimiter = max([",", "\t", "|", ";"], key=first.count)
    if first.count(delimiter) == 0:
        return None, None
    reader = csv.reader(text_stream, delimiter=delimiter, quotechar='"')
    header = None
    cells = [c.strip().upper() for c in next(csv.reader([first], delimiter=delimiter, quotechar='"'))]
    if any(re.search(r"[A-Z]{3,}", c) for c in cells) and not any(re.fullmatch(r"\d{6}", c) for c in cells):
        header = cells
        next(reader, None)
    return reader, header


def open_zip_text(path):
    """Yields (member name, text stream, sample) for each data file in a zip."""
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist()
                   if not n.endswith("/") and not n.lower().endswith((".pdf", ".htm", ".html", ".xml"))]
        for name in members:
            with archive.open(name) as raw:
                sample = raw.read(4096).decode("latin-1")
            with archive.open(name) as raw:
                yield name, io.TextIOWrapper(raw, encoding="latin-1", newline=""), sample


def write_json(path, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(raw)
    path.with_name(path.name + ".gz").write_bytes(gzip.compress(raw, 6))
    return len(raw)


# --------------------------------------------------------------------------- wells (OGOR-A)
def ogora_records(path):
    """Yields (api12, yyyymm, oil_bbl, gas_mcf, water_bbl, days_on_prod, operator) from one zip."""
    for name, stream, sample in open_zip_text(path):
        reader, header = sniff_rows(stream, sample)
        if reader is None:          # no delimiter: try fixed width
            for line in stream:
                if len(line) < 61:
                    continue
                get = lambda f: line[OGORA_FIXED[f][0] - 1: OGORA_FIXED[f][0] - 1 + OGORA_FIXED[f][1]]
                yield (normalize_api(get("API_WELL_NUMBER")), parse_month(get("PRODUCTION_DATE")),
                       num(get("MON_O_PROD_VOL")), num(get("MON_G_PROD_VOL")), num(get("MON_WTR_PROD_VOL")),
                       num(get("DAYS_ON_PROD")), get("SORT_NAME").strip())
            continue
        cols = header or OGORA_COLUMNS
        pos = {c: i for i, c in enumerate(cols)}

        def find(*names):
            for n in names:
                if n in pos:
                    return pos[n]
            for n in names:
                for c, i in pos.items():
                    if n in c:
                        return i
            return None
        i_api, i_date = find("API_WELL_NUMBER", "API"), find("PRODUCTION_DATE", "PROD_DATE")
        i_oil, i_gas = find("MON_O_PROD_VOL", "OIL"), find("MON_G_PROD_VOL", "GAS")
        i_wtr, i_days = find("MON_WTR_PROD_VOL", "WTR", "WATER"), find("DAYS_ON_PROD", "DAYS")
        i_op = find("SORT_NAME", "OPERATOR_NAME")
        if None in (i_api, i_date, i_oil, i_gas, i_wtr):
            raise SystemExit(f"{path.name}/{name}: unrecognised columns {cols[:20]}")
        width = max(i_api, i_date, i_oil, i_gas, i_wtr)
        for row in reader:
            if len(row) <= width:
                continue
            yield (normalize_api(row[i_api]), parse_month(row[i_date]), num(row[i_oil]), num(row[i_gas]),
                   num(row[i_wtr]), num(row[i_days]) if i_days is not None and i_days < len(row) else 0,
                   row[i_op].strip() if i_op is not None and i_op < len(row) else "")


def build_wells(transport, full, keep_years, incremental=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    this_year = dt.date.today().year
    first_refresh = max(FIRST_YEAR, this_year - keep_years + 1) if incremental else FIRST_YEAR
    cutoff = (first_refresh - BASE_MONTH[0]) * 12
    if incremental:
        for shard in range(WELL_SHARDS):
            if not (OUT / "wells" / f"{shard}.json").is_file():
                raise SystemExit("Incremental refresh requires a complete existing well dataset.")
    shard_dir = OUT / ".wells.tmp"
    if shard_dir.exists():
        shutil.rmtree(shard_dir)
    shard_dir.mkdir(parents=True)
    # Year-by-year into per-shard CSV spill files keeps memory low.
    spills = {}
    years_used, rows_read, rows_kept = [], 0, 0
    for year in range(first_refresh, this_year + 1):
        zip_path = CACHE / f"ogora{year}delimit.zip"
        if zip_path.exists() and not is_valid_zip(zip_path):
            zip_path.unlink()                       # left over from an earlier failed run
        refresh = full or not zip_path.exists() or year >= this_year - keep_years + 1
        if refresh:
            print(f"[wells] downloading OGOR-A {year} …", flush=True)
            try:
                used = fetch_zip(transport, ogora_candidates(year, this_year), zip_path)
                print(f"[wells]   from {used}")
            except RuntimeError as exc:
                if incremental:
                    raise SystemExit(f"Recent-year refresh failed; preserving the published release: {year}: {exc}")
                if zip_path.exists():
                    print(f"[wells] {year} download failed; using cached copy ({exc})")
                elif year == this_year:
                    print(f"[wells] {year} file not available yet; skipped ({exc})")
                    continue
                else:
                    raise SystemExit(f"Could not download OGOR-A {year}: {exc}")
        if not zip_path.exists():
            continue
        before = rows_kept
        for api, ym, oil, gas, wtr, days, operator in ogora_records(zip_path):
            rows_read += 1
            mi = month_index(ym) if ym else None
            if not api or mi is None or mi < 0:
                continue
            shard = int(api) % WELL_SHARDS
            fh = spills.get(shard)
            if fh is None:
                fh = spills[shard] = open(shard_dir / f"{shard}.csv", "a", newline="", encoding="utf-8")
            fh.write(f"{api},{mi},{oil:g},{gas:g},{wtr:g},{days:g},{operator.replace(',', ' ')}\n")
            rows_kept += 1
        years_used.append(year)
        print(f"[wells] {year}: {rows_kept - before:,} completion-months")
        for fh in spills.values():
            fh.flush()
    for fh in spills.values():
        fh.close()

    final = OUT / ".wells.new"
    if final.exists():
        shutil.rmtree(final)
    final.mkdir()
    wells_total, latest, size = 0, -1, 0
    for shard in range(WELL_SHARDS):
        spill = shard_dir / f"{shard}.csv"
        wells = {}
        if spill.exists():
            with open(spill, encoding="utf-8") as fh:
                for line in fh:
                    api, mi, oil, gas, wtr, days, operator = line.rstrip("\n").split(",", 6)
                    mi = int(mi)
                    rec = wells.setdefault(api, {"op": "", "m": {}})
                    acc = rec["m"].setdefault(mi, [0.0, 0.0, 0.0, 0.0, 0])
                    acc[0] += float(oil)
                    acc[1] += float(gas)
                    acc[2] += float(wtr)
                    acc[3] = max(acc[3], float(days))
                    acc[4] += 1
                    if operator:
                        rec["op"] = operator          # most recent year wins (files read in order)
        out = {}
        if incremental:
            previous = json.loads((OUT / "wells" / f"{shard}.json").read_text(encoding="utf-8"))
            for api, rec in previous.items():
                historical = [row for row in rec["r"] if row[0] < cutoff]
                if historical:
                    out[api] = {"op": rec.get("op", ""), "r": historical}
        for api, rec in wells.items():
            rows = list(out.get(api, {}).get("r", []))
            for mi in sorted(rec["m"]):
                oil, gas, wtr, days, completions = rec["m"][mi]
                rows.append([mi, round(oil), round(gas), round(wtr), int(days), completions])
                latest = max(latest, mi)
            out[api] = {"op": rec["op"], "r": rows}
        for rec in out.values():
            if rec["r"]:
                latest = max(latest, rec["r"][-1][0])
        wells_total += len(out)
        size += write_json(final / f"{shard}.json", out)
    shutil.rmtree(shard_dir, ignore_errors=True)
    print(f"[wells] {wells_total:,} wells from {rows_read:,} rows · {size / 1e6:,.1f} MB")
    return final, {"count": wells_total, "shards": WELL_SHARDS, "years": [FIRST_YEAR if incremental else min(years_used), max(years_used)] if years_used else None,
                   "latestMonth": "%04d-%02d" % index_to_ym(latest) if latest >= 0 else None,
                   "source": OGORA_HOSTS[0] + "ogora<year>delimit.zip / ogoradelimit.zip"}


# --------------------------------------------------------------------------- platforms
def find_platform_url(transport):
    for page in RAW_DATA_PAGES:
        try:
            html = transport.get_text(page)
        except Exception as exc:                       # noqa: BLE001
            print(f"[platforms] could not read {page}: {exc}")
            continue
        # The row text "Production by Platform" is followed by its "Delimit" link.
        for match in re.finditer(r"Production\s+by\s+Platform", html, re.I):
            tail = html[match.end(): match.end() + 3000]
            if re.match(r"\s*Online Query", tail, re.I):
                continue
            link = re.search(r'href\s*=\s*["\']([^"\']+?\.(?:zip|txt|csv))["\']', tail, re.I)
            if link:
                return urllib.parse.urljoin(page, link.group(1).replace("&amp;", "&"))
    return None


def platform_records(path):
    """Yields dicts with complex, ym, oil, gas, water, boe, wells, name, operator."""
    if zipfile.is_zipfile(path):
        sources = open_zip_text(path)
    else:
        text = path.read_text(encoding="latin-1")
        sources = [(path.name, io.StringIO(text), text[:4096])]
    for name, stream, sample in sources:
        reader, header = sniff_rows(stream, sample)
        if reader is None or header is None:
            raise SystemExit(f"{name}: the Production by Platform file has no header row; "
                             "please send the first few lines so the parser can be adjusted.")

        def find(*patterns, exclude=None):
            for pattern in patterns:
                for i, col in enumerate(header):
                    if re.search(pattern, col) and not (exclude and re.search(exclude, col)):
                        return i
            return None
        # Exact BOEM names first (BOPD, MCFPD, BOEPD, BWPD, PF_OPERATOR, PRODUCTION_DATE), then looser fallbacks.
        idx = {
            "complex": find(r"^COMPLEX_ID_NUM$", r"COMPLEX"),
            "date": find(r"^PRODUCTION_DATE$", r"^PROD.*(MONTH|DATE|MTH)$", r"^(MONTH|DATE)$", r"YEAR.?MONTH"),
            "boe": find(r"^BOEPD$", r"(BOE|EQUIV)"),
            "oil": find(r"^BOPD$", r"OIL", exclude=r"(BOE|EQUIV)"),
            "gas": find(r"^MCFPD$", r"GAS", exclude=r"(BOE|EQUIV)"),
            "water": find(r"^BWPD$", r"(WATER|WTR)"),
            "wells": find(r"^PRODUCING_WELLS$", r"WELL"),
            "name": find(r"^STRUCTURE_NAME$", r"STRUC.*NAME"),
            "operator": find(r"^PF_OPERATOR$", r"^OPERATOR(_NAME)?$", r"(OPERATOR|BUS_ASC|COMPANY).*NAME", r"SORT_NAME"),
        }
        missing = [k for k in ("complex", "date", "oil", "gas", "water") if idx[k] is None]
        if missing:
            raise SystemExit(f"{name}: could not find columns {missing} in header {header}")
        is_volume = {k: idx[k] is not None and re.search(r"VOL", header[idx[k]]) is not None
                     for k in ("oil", "gas", "water", "boe")}
        print(f"[platforms] {name}: columns " + ", ".join(f"{k}={header[i]}" for k, i in idx.items() if i is not None))
        for row in reader:
            if len(row) <= max(i for i in idx.values() if i is not None):
                continue
            complex_id = re.sub(r"\D", "", row[idx["complex"]])
            ym = parse_month(row[idx["date"]])
            if not complex_id or not ym:
                continue
            values = {k: num(row[idx[k]]) if idx[k] is not None else None for k in ("oil", "gas", "water", "boe")}
            mi = month_index(ym)
            if mi is None or mi < 0:
                continue
            if any(is_volume.values()):
                days = days_in(mi)
                values = {k: (v / days if is_volume[k] and v is not None else v) for k, v in values.items()}
            yield {"complex": str(int(complex_id)), "mi": mi, **values,
                   "wells": num(row[idx["wells"]]) if idx["wells"] is not None else None,
                   "name": row[idx["name"]].strip() if idx["name"] is not None else "",
                   "operator": row[idx["operator"]].strip() if idx["operator"] is not None else ""}


def build_platforms(transport, url):
    CACHE.mkdir(parents=True, exist_ok=True)
    if not url:
        print("[platforms] locating the Production by Platform raw data file …")
        url = find_platform_url(transport)
        if not url:
            raise SystemExit("Could not find the Production by Platform download on the BSEE Raw Data page. "
                             "Open https://www.data.bsee.gov/Main/RawData.aspx, copy the 'Delimit' link for "
                             "Production by Platform and re-run with --platform-url <link>.")
    suffix = pathlib.Path(urllib.parse.urlparse(url).path).suffix or ".zip"
    path = CACHE / f"production-by-platform{suffix}"
    print(f"[platforms] downloading {url} …")
    fetch(transport, url, path)

    # Rows can repeat per structure within a complex; identical month/rate rows are counted once.
    complexes = {}
    for rec in platform_records(path):
        c = complexes.setdefault(rec["complex"], {"names": set(), "op": "", "m": {}, "seen": set()})
        key = (rec["mi"], round(rec["oil"] or 0, 3), round(rec["gas"] or 0, 3), round(rec["water"] or 0, 3))
        if rec["name"]:
            c["names"].add(rec["name"])
        if rec["operator"]:
            c["op"] = rec["operator"]
        if key in c["seen"]:
            continue
        c["seen"].add(key)
        acc = c["m"].setdefault(rec["mi"], [0.0, 0.0, 0.0, 0.0, 0.0, False])
        acc[0] += rec["oil"] or 0
        acc[1] += rec["gas"] or 0
        acc[2] += rec["water"] or 0
        if rec["boe"] is not None:
            acc[3] += rec["boe"]
            acc[5] = True
        acc[4] += rec["wells"] or 0

    final = OUT / ".platforms.new"
    if final.exists():
        shutil.rmtree(final)
    final.mkdir()
    shards = {i: {} for i in range(PLATFORM_SHARDS)}
    latest = -1
    for complex_id, c in complexes.items():
        rows = []
        for mi in sorted(c["m"]):
            oil, gas, water, boe, wells, has_boe = c["m"][mi]
            rows.append([mi, round(oil, 1), round(gas, 1), round(water, 1),
                         round(boe if has_boe else oil + gas / BOE_MCF, 1), int(round(wells))])
            latest = max(latest, mi)
        shards[int(complex_id) % PLATFORM_SHARDS][complex_id] = {"names": sorted(c["names"]), "op": c["op"], "r": rows}
    size = sum(write_json(final / f"{i}.json", data) for i, data in shards.items())
    print(f"[platforms] {len(complexes):,} complexes · {size / 1e6:,.1f} MB")
    return final, {"count": len(complexes), "shards": PLATFORM_SHARDS, "source": url,
                   "latestMonth": "%04d-%02d" % index_to_ym(latest) if latest >= 0 else None}


def swap_in(new_dir, name):
    """Replaces OUT/name with new_dir; tolerates OneDrive locks by leaving the old folder aside."""
    target = OUT / name
    if target.exists():
        old = OUT / f".{name}.old-{int(time.time())}"
        try:
            target.rename(old)
        except OSError:
            # Could not move the old folder (locked): copy files over instead.
            for f in new_dir.iterdir():
                shutil.copy2(f, target / f.name)
            shutil.rmtree(new_dir, ignore_errors=True)
            return
        shutil.rmtree(old, ignore_errors=True)
    new_dir.rename(target)


# --------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="re-download every year of well data")
    parser.add_argument("--incremental", action="store_true", help="preserve published history and replace the recent-years window")
    parser.add_argument("--recent-years", type=int, default=3,
                        help="years of well data re-downloaded on a normal run (default 3)")
    parser.add_argument("--wells-only", action="store_true")
    parser.add_argument("--platforms-only", action="store_true")
    parser.add_argument("--platform-url", help="direct link to the Production by Platform raw data file")
    parser.add_argument("--transport", choices=["auto", "python", "powershell"], default="auto")
    parser.add_argument("--ca-bundle", help="extra PEM file with your company's root certificate")
    args = parser.parse_args()
    if args.wells_only and args.platforms_only:
        parser.error("choose at most one of --wells-only / --platforms-only")
    if args.incremental and args.full:
        parser.error("--incremental and --full cannot be combined")

    OUT.mkdir(parents=True, exist_ok=True)
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    transport = Transport(args.transport, args.ca_bundle)

    if not args.platforms_only:
        new_dir, meta = build_wells(transport, args.full, max(1, args.recent_years), args.incremental)
        swap_in(new_dir, "wells")
        index["wells"] = meta
    if not args.wells_only:
        new_dir, meta = build_platforms(transport, args.platform_url)
        swap_in(new_dir, "platforms")
        index["platforms"] = meta

    generated = dt.datetime.now(dt.timezone.utc)
    index.update({"generated": generated.isoformat(timespec="seconds"),
                  "version": generated.strftime("%Y%m%dT%H%M%S"),
                  "baseMonth": "%04d-%02d" % BASE_MONTH, "boeMcf": BOE_MCF})
    tmp = index_path.with_name("index.json.new")
    tmp.write_text(json.dumps(index, indent=1))
    os.replace(tmp, index_path)
    print(f"done: {index_path}")


if __name__ == "__main__":
    main()
