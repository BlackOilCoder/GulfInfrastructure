"""Small server-selected Talos snapshot; do not send the full Gulf to mobile clients."""
import json
import threading


class TalosSubset:
    def __init__(self, data_root, cloud=None):
        self.root, self.cloud = data_root, cloud
        self.lock = threading.Lock()
        self.cached_version = None
        self.cached_payload = None

    def get(self):
        with self.lock:
            if self.cloud:
                catalog = self.cloud.release("infra")
                manifest = catalog["index"]
                version = catalog["release"]

                def read(name):
                    entry = catalog["files"][name]
                    return json.loads(self.cloud.objects.read(entry["object"], maximum=64 * 1024 * 1024))
            else:
                manifest = json.loads((self.root / "manifest.json").read_bytes())
                version = tuple((key, entry.get("sha256")) for key, entry in sorted(manifest["datasets"].items()))

                def read(name):
                    path = (self.root / name).resolve()
                    if not path.is_relative_to(self.root.resolve()):
                        raise ValueError("Invalid snapshot path")
                    return json.loads(path.read_bytes())

            if version == self.cached_version:
                return self.cached_payload
            assets = read(manifest["datasets"]["talosAssets"]["file"])
            result = {"assets": assets, "datasets": {}}
            for kind, field in (("platforms", "COMPLEX_ID_NUM"), ("pipelines", "SEGMENT_NUM"), ("wells", "API_NUMBER")):
                data = read(manifest["datasets"][kind]["file"])
                column = next(i for i, item in enumerate(data["fields"]) if item["name"] == field)
                wanted = {str(value).strip() for value in assets["keys"][kind]}
                rows, geoms = [], []
                for row, geom in zip(data["rows"], data["geoms"]):
                    if str(row[column]).strip() in wanted:
                        rows.append(row)
                        geoms.append(geom)
                result["datasets"][kind] = {**data, "rows": rows, "geoms": geoms}
                del data
            payload = json.dumps(result, separators=(",", ":")).encode()
            self.cached_version, self.cached_payload = version, payload
            return payload
