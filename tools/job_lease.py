"""Renewable GCS lease; duplicate scheduled executions cannot publish concurrently."""
import logging
import os
import threading
import time
import uuid

LOG = logging.getLogger("bluewater")


class DatasetLease:
    def __init__(self, objects, kind):
        self.objects, self.key = objects, f"locks/{kind}.json"
        self.owner = os.environ.get("CLOUD_RUN_EXECUTION", "local-" + uuid.uuid4().hex)
        self.generation = None
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.mutex = threading.Lock()
        self.thread = None

    def payload(self):
        return {"owner": self.owner, "expires": time.time() + 180}

    def __enter__(self):
        from google.api_core.exceptions import PreconditionFailed
        for attempt in range(25):
            previous, generation = self.objects.pointer(self.key)
            if previous and previous["expires"] > time.time():
                if previous["owner"] != self.owner:
                    print("A refresh for this dataset is already running; skipping duplicate execution.", flush=True)
                    return self
                # A retried task waits for the previous attempt's lease to expire.
                time.sleep(10)
                continue
            try:
                self.generation = self.objects.activate(self.key, self.payload(), generation)
                self.thread = threading.Thread(target=self.renew, daemon=True)
                self.thread.start()
                return self
            except PreconditionFailed:
                time.sleep(1)
        raise RuntimeError("Could not acquire the dataset refresh lease.")

    def renew(self):
        while not self.stop.wait(45):
            try:
                with self.mutex:
                    self.generation = self.objects.activate(self.key, self.payload(), self.generation)
            except Exception:
                self.lost.set()
                LOG.exception("Refresh lease renewal failed; publication is disabled")
                return

    def check(self):
        if self.lost.is_set() or self.generation is None:
            raise RuntimeError("The refresh lease was lost; refusing to publish.")
        current, generation = self.objects.pointer(self.key)
        with self.mutex:
            if not current or current["owner"] != self.owner or current["expires"] <= time.time():
                raise RuntimeError("The refresh lease expired; refusing to publish.")

    def __exit__(self, *args):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=60)
        if self.generation is not None:
            from google.api_core.exceptions import NotFound, PreconditionFailed
            try:
                with self.mutex:
                    self.objects.blob(self.key).delete(if_generation_match=self.generation, timeout=30)
            except (NotFound, PreconditionFailed):
                pass
            except Exception:
                LOG.exception("Could not release refresh lease; it will expire automatically")
