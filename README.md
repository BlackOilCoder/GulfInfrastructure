# Bluewater — local snapshot setup

## Folder layout
    index.html
    platform-structures.js     (legacy source; still used as a fallback)
    data/                      (generated snapshots + manifest.json)
    tools/build_snapshots.py   (refreshes data/)
    tools/serve.py             (local server with gzip + cache headers)

## First run / refresh data
    python tools/build_snapshots.py              # platforms, wells, pipelines + structures
    python tools/build_snapshots.py pipelines    # refresh just one dataset
    python tools/build_snapshots.py --structures-only

When a new platform-structures.js is dropped in, run with --structures-only.

## Serve
    python tools/serve.py          # http://localhost:8000/

## How loading works
1. The browser fetches data/manifest.json (small, always revalidated).
2. For each dataset, if IndexedDB already holds the same sha256 version it is used directly;
   otherwise the versioned JSON file is downloaded and cached.
3. If the snapshot cannot be fetched, the last cached copy is used (offline); if there is
   none, that layer falls back to the live BOEM feature service.
4. Wells load only when the Wells layer is switched on or a search runs.
5. Platform Structures load in the background after the map has drawn.

Clear the cache: browser DevTools > Application > IndexedDB > bluewater-snapshots > Delete.
