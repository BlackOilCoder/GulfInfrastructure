"""Production HTTP adapter; raster math remains in tools/bathymetry.py."""
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from tools.bathymetry import Bathymetry
from tools.config import ROOT, data_directory, structures_file

LOG = logging.getLogger("bluewater")
MAX_BODY = 1024 * 1024


def create_app():
    data_root = data_directory()
    backend = os.environ.get("BLUEWATER_STORAGE_BACKEND", "local")
    cloud = None
    if backend == "gcs":
        from tools.storage import GCSObjects, CloudDatasets
        from tools.cloud_bathymetry import CloudBathymetry
        cloud = CloudDatasets(GCSObjects(os.environ.get("BLUEWATER_DATA_BUCKET")))
        engine = CloudBathymetry(GCSObjects(os.environ.get("BLUEWATER_BATHYMETRY_BUCKET")),
                                 GCSObjects(os.environ.get("BLUEWATER_TILE_BUCKET")))
    elif backend == "local":
        engine = Bathymetry()
    else:
        raise ValueError("BLUEWATER_STORAGE_BACKEND must be local or gcs.")
    concurrency = int(os.environ.get("BLUEWATER_RASTER_CONCURRENCY", "4"))
    if not 1 <= concurrency <= 8:
        raise ValueError("BLUEWATER_RASTER_CONCURRENCY must be between 1 and 8.")
    slots = threading.BoundedSemaphore(concurrency)

    @asynccontextmanager
    async def lifespan(application):
        # Match the existing bounded raster work; keep threads for ordinary file responses.
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 12
        LOG.info("Bathymetry status: %s", engine.status())
        yield

    application = FastAPI(title="Bluewater", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)

    @application.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code,
                            headers={"Cache-Control": "no-store"})

    @application.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if os.environ.get("BLUEWATER_PRIVATE_CACHE") == "1":
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def state_or_fail(version=None):
        state = engine.status()
        if not state["ready"]:
            raise HTTPException(503, state["message"])
        if version and version != state["version"]:
            raise HTTPException(409, "The bathymetry version changed. Refresh the page and try again.")
        return state

    def raster_operation(operation):
        if not slots.acquire(timeout=10):
            raise HTTPException(503, "The raster service is busy. Try again shortly.")
        try:
            return operation()
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except Exception as error:
            LOG.exception("Bathymetry operation failed")
            raise HTTPException(503, "The bathymetry raster could not be read.") from error
        finally:
            slots.release()

    @application.get("/healthz")
    def health():
        return JSONResponse({"status": "ok", "version": os.environ.get("BLUEWATER_RELEASE", "local")},
                            headers={"Cache-Control": "no-store"})

    from tools.infrastructure import TalosSubset
    talos_subset = TalosSubset(data_root, cloud)

    @application.get("/api/infrastructure/talos")
    def talos_infrastructure():
        try:
            return Response(talos_subset.get(), media_type="application/json",
                            headers={"Cache-Control": "private, no-cache"})
        except Exception as error:
            LOG.exception("Talos subset could not be loaded")
            raise HTTPException(503, "Talos asset snapshots are temporarily unavailable.") from error

    @application.get("/readyz")
    def ready():
        try:
            data_ready = cloud.exists() if cloud else (data_root / "manifest.json").is_file()
        except Exception:
            LOG.exception("Dataset readiness check failed")
            data_ready = False
        checks = {"application": (ROOT / "index.html").is_file(),
                  "data": data_ready,
                  "bathymetry": engine.status()["ready"]}
        return JSONResponse({"ready": all(checks.values()), "checks": checks},
                            status_code=200 if all(checks.values()) else 503,
                            headers={"Cache-Control": "no-store"})

    @application.get("/api/bathymetry/status")
    def bathymetry_status():
        return JSONResponse(engine.status(), headers={"Cache-Control": "no-store"})

    @application.post("/api/bathymetry/sample")
    async def sample(request: Request):
        body = bytearray()
        # Streaming limit also covers requests with no Content-Length header.
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_BODY:
                        raise HTTPException(413, "Send a JSON request smaller than 1 MB.")
                    body.extend(chunk)
        except TimeoutError as error:
            raise HTTPException(408, "The request body took too long to arrive.") from error
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Send a JSON object containing points and an optional method.")
        except (ValueError, UnicodeDecodeError) as error:
            raise HTTPException(400, str(error)) from error

        def execute():
            state_or_fail(payload.get("version"))
            return raster_operation(lambda: engine.sample(payload.get("points"), payload.get("method", "nearest")))

        result = await run_in_threadpool(execute)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @application.api_route("/api/bathymetry/hillshade/{z}/{x}/{y}.png", methods=["GET", "HEAD"])
    def tile(z: int, x: int, y: int, request: Request, v: str | None = None):
        state_or_fail(v)
        image = raster_operation(lambda: engine.tile(z, x, y, version=v))
        headers = {"Cache-Control": "public, max-age=31536000, immutable" if v else "no-cache",
                   "Content-Length": str(len(image))}
        return Response(b"" if request.method == "HEAD" else image, media_type="image/png", headers=headers)

    def serve_file(path, request, versioned=False):
        if not path.is_file():
            raise HTTPException(404, "File not found.")
        headers = {"Cache-Control": "public, max-age=31536000, immutable" if versioned else "no-cache"}
        media_types = {".json": "application/json", ".js": "text/javascript", ".html": "text/html", ".png": "image/png"}
        media_type = media_types.get(path.suffix, "application/octet-stream")
        # Preserve the existing gzip snapshots, avoiding recompression per request.
        accepts = request.headers.get("accept-encoding", "").lower().split(",")
        gzip_ok = any(part.split(";", 1)[0].strip() == "gzip" and
                      not any(p.strip() in {"q=0", "q=0.0", "q=0.000"} for p in part.split(";")[1:])
                      for part in accepts)
        compressed = path.with_name(path.name + ".gz")
        if path.suffix in {".js", ".json"}:
            headers["Vary"] = "Accept-Encoding"
            if gzip_ok and compressed.is_file() and compressed.stat().st_mtime >= path.stat().st_mtime:
                path = compressed
                headers["Content-Encoding"] = "gzip"
        return FileResponse(path, media_type=media_type, headers=headers)

    @application.api_route("/", methods=["GET", "HEAD"])
    @application.api_route("/index.html", methods=["GET", "HEAD"])
    def index(request: Request):
        return serve_file(ROOT / "index.html", request)

    @application.api_route("/platform-structures.js", methods=["GET", "HEAD"])
    def structures(request: Request):
        if cloud:
            return serve_cloud("platform-structures.js", request)
        return serve_file(structures_file(), request)

    def serve_cloud(filename, request):
        from google.api_core.exceptions import NotFound
        try:
            inline, entry, media_type, immutable = cloud.resolve(filename, request.query_params.get("v"))
            headers = {"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"}
            if inline is not None:
                headers["Content-Length"] = str(len(inline))
                return Response(b"" if request.method == "HEAD" else inline, media_type=media_type, headers=headers)
            headers["Vary"] = "Accept-Encoding"
            if "gzip" in request.headers.get("accept-encoding", "").lower() and entry.get("gzip"):
                entry = entry["gzip"]
                headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(entry["size"])
            if request.method == "HEAD":
                return Response(media_type=media_type, headers=headers)
            blob = cloud.objects.blob(entry["object"])

            def chunks():
                with blob.open("rb", chunk_size=256*1024) as stream:
                    while chunk := stream.read(256*1024):
                        yield chunk

            return StreamingResponse(chunks(), media_type=media_type, headers=headers)
        except (NotFound, FileNotFoundError) as error:
            raise HTTPException(404, "Dataset not found.") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except Exception as error:
            LOG.exception("Cloud dataset request failed")
            raise HTTPException(503, "Cloud datasets are temporarily unavailable.") from error

    @application.api_route("/data/{filename:path}", methods=["GET", "HEAD"])
    def dataset(filename: str, request: Request):
        if cloud:
            if (any(part.startswith(".") or part.lower() in {"cache", "sources"} for part in Path(filename).parts) or
                    Path(filename).suffix.lower() not in {".json", ".png"}):
                raise HTTPException(404, "File not found.")
            return serve_cloud(filename, request)
        path = (data_root / filename).resolve()
        # Only published files, never caches, hidden files, scripts, or directory listings.
        if (not path.is_relative_to(data_root) or
                any(part.startswith(".") or part.lower() in {"cache", "sources"} for part in Path(filename).parts) or
                path.suffix.lower() not in {".json", ".png"}):
            raise HTTPException(404, "File not found.")
        return serve_file(path, request, versioned=bool(request.query_params.get("v")))

    return application


app = create_app()
