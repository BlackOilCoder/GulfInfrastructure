# Mobile infrastructure loading

The default Talos layer previously instantiated all three full Gulf snapshot
FeatureLayers, retained their search indexes and snapshot objects, and then built
the Talos layers from copies. This included 54,578 wells and 16,838 pipeline
segments even with their full-layer switches off. That creates a large startup
memory peak, consistent with Safari killing/reloading a page under memory pressure.
The reported iPhone termination has not been reproduced on a physical iPhone.

The web adapter now exposes authenticated `/api/infrastructure/talos`, selecting
matching rows and complete geometries on the server from the active infrastructure
release. The browser builds only the Talos subset and does not instantiate hidden
full layers. Server selection is serialized and caches one encoded response until
the release changes. No new published datasets or scheduled jobs are required.
Existing popup fields, full pipeline geometry and platform labels are retained.

On iPhone, iPad and Android, full infrastructure layers use the existing BOEM
FeatureServer fallback, allowing the SDK to request visible features rather than
instantiating all Gulf features in browser memory. Those full layers and their
searches therefore require live agency connectivity and may reflect newer records
than the snapshot-backed Talos layer. Desktop full layers retain snapshot behavior.

For the older native HTTP server without this endpoint, the browser selects Talos
rows before constructing graphics and skips IndexedDB caching of those full input
files. Native mode still downloads each full snapshot sequentially, so the cloud
adapter's subset endpoint provides the smaller mobile transfer as well.

This change addresses the identified startup allocation path. Physical Safari
validation is still needed, including repeat openings, panning and layers. It does
not establish that every possible WebGL/device-memory failure is resolved.
