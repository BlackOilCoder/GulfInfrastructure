"""Private GCS objects and immutable release catalogues. No credentials in URLs."""
import copy
import json
import re
import threading

RELEASE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
KINDS = {"infra", "production", "currents", "contours"}


def object_name(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value or
            value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("Invalid storage object path.")
    return value


class GCSObjects:
    def __init__(self, bucket_name, client=None):
        from google.cloud import storage
        if not bucket_name or "/" in bucket_name:
            raise ValueError("Configure a bucket name, without gs:// or a prefix.")
        self.client = client or storage.Client()
        self.bucket = self.client.bucket(bucket_name)
        self._credentials_lock = threading.Lock()

    def blob(self, key):
        return self.bucket.blob(object_name(key))

    def read(self, key, maximum=None):
        blob = self.blob(key)
        blob.reload(timeout=30)
        if maximum is not None and blob.size > maximum:
            raise ValueError("Storage metadata exceeds its permitted size.")
        return blob.download_as_bytes(if_generation_match=blob.generation, timeout=60)

    def json(self, key):
        return json.loads(self.read(key, maximum=16 * 1024 * 1024))

    def pointer(self, key):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        for attempt in range(3):
            blob = self.blob(key)
            try:
                blob.reload(timeout=30)
                return json.loads(blob.download_as_bytes(if_generation_match=blob.generation, timeout=30)), int(blob.generation)
            except NotFound:
                return None, 0
            except PreconditionFailed:
                if attempt == 2:
                    raise

    def activate(self, key, payload, generation):
        blob = self.blob(key)
        blob.upload_from_string(json.dumps(payload), content_type="application/json",
                                if_generation_match=generation, timeout=60)
        return int(blob.generation)

    def put_new(self, key, payload, content_type="application/octet-stream"):
        self.blob(key).upload_from_string(payload, content_type=content_type,
                                         if_generation_match=0, timeout=60)

    def token(self):
        from google.auth.transport.requests import Request
        with self._credentials_lock:
            credentials = self.client._credentials
            if not credentials.valid:
                credentials.refresh(Request())
            return credentials.token


class CloudDatasets:
    """Map existing browser URLs to an immutable release, including old sessions."""
    def __init__(self, objects):
        self.objects = objects
        self._releases = {}
        self._lock = threading.RLock()

    def release(self, kind, version=None):
        if kind not in KINDS:
            raise ValueError("Unknown dataset kind.")
        if version is None:
            pointer, _ = self.objects.pointer(f"active/{kind}.json")
            if pointer is None:
                raise FileNotFoundError(f"No {kind} release has been published.")
            version = pointer["release"]
        if not RELEASE.fullmatch(version):
            raise ValueError("Invalid dataset release.")
        key = (kind, version)
        with self._lock:
            if key not in self._releases:
                value = self.objects.json(f"releases/{kind}/{version}/catalog.json")
                if value.get("schemaVersion") != 1 or value.get("release") != version or value.get("kind") != kind:
                    raise ValueError("Invalid dataset catalogue.")
                self._releases[key] = value
                if len(self._releases) > 24:
                    self._releases.pop(next(iter(self._releases)))
            return self._releases[key]

    def exists(self):
        try:
            return "manifest.json" in self.release("infra")["files"]
        except FileNotFoundError:
            return False

    def resolve(self, filename, version=None):
        object_name(filename)
        parts = filename.split("/")
        if parts[0] == "releases":
            if len(parts) < 4:
                raise ValueError("Invalid dataset URL.")
            kind, release = parts[1:3]
            logical = "/".join(parts[3:])
            catalog = self.release(kind, release)
            immutable = True
        else:
            kind = parts[0] if parts[0] in KINDS - {"infra"} else "infra"
            logical = "/".join(parts[1:]) if kind != "infra" else filename
            is_index = logical == ("manifest.json" if kind in {"infra", "currents"} else "index.json")
            catalog = self.release(kind, None if is_index or kind == "infra" else version)
            immutable = bool(version) and not is_index
            if is_index:
                index = copy.deepcopy(catalog["index"])
                if kind == "infra":
                    for entry in index["datasets"].values():
                        entry["file"] = f"releases/infra/{catalog['release']}/{entry['file']}"
                else:
                    index["version"] = catalog["release"]
                return json.dumps(index).encode(), None, "application/json", False
        try:
            entry = catalog["files"][logical]
        except KeyError as error:
            raise FileNotFoundError(filename) from error
        expected = f"releases/{kind}/{catalog['release']}/files/"
        key = object_name(entry["object"])
        if not key.startswith(expected):
            raise ValueError("Dataset object is outside its release.")
        if entry.get("gzip") and not object_name(entry["gzip"]["object"]).startswith(expected):
            raise ValueError("Compressed dataset object is outside its release.")
        return None, entry, entry["contentType"], immutable
