"""FastAPI app for the Python Learning Agent."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
import logging

from backend.config import settings

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    users_dir = os.path.join(settings.DATA_DIR, "users")
    os.makedirs(users_dir, exist_ok=True)
    os.makedirs(settings.CHROMA_DIR, exist_ok=True)
    logger.info("Python Learning Agent starting...")
    logger.info(f"Model: {settings.MODEL_NAME}")
    logger.info(f"Server: http://{settings.API_HOST}:{settings.API_PORT}")
    logger.info(f"Data: {os.path.abspath(settings.DATA_DIR)}")

    # Put archived SDK transcripts back where the CLI expects them. On this
    # machine that is usually a no-op; on a fresh one it is what makes a copied
    # data/ directory resume instead of starting every session from scratch.
    try:
        from backend.transcripts import restore_all

        restored = restore_all()
        if restored:
            logger.info(f"Restored {restored} SDK transcript(s) from data/")
    except Exception as e:
        logger.warning(f"Transcript restore failed: {e}")

    # A demo build that was in flight when the process died cannot resume, and
    # its job record would otherwise keep the UI chip spinning forever. Resolve
    # those to failed so the learner sees what happened.
    try:
        from backend.demojobs import sweep_stale

        sweep_stale()
    except Exception as e:
        logger.warning(f"Demo job sweep failed: {e}")

    # Same for background code runs: an orphaned one would otherwise keep
    # telling the model "still running, check again later" indefinitely.
    try:
        from backend.codejobs import sweep_stale as sweep_code_jobs

        swept = sweep_code_jobs()
        if swept:
            logger.info(f"Resolved {swept} stale code job(s)")
    except Exception as e:
        logger.warning(f"Code job sweep failed: {e}")

    logger.info("Ready!")
    yield
    logger.info("Shutting down.")


app = FastAPI(title="Python Learning Agent", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and include routes
from backend.api.routes import router  # noqa: E402
app.include_router(router)

# Serve frontend static files
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


class NoCacheStaticFiles(StaticFiles):
    """Static files that must be revalidated before reuse.

    The pages pin ?v= on directly-linked assets, but ES modules import each
    other with bare paths that carry no version. With no Cache-Control the
    browser invents a freshness lifetime and can serve a stale shared module
    indefinitely while the parent page updates — a fixed file that "didn't
    take". This does not disable caching: StaticFiles still sends an etag, so
    revalidation costs one conditional request and usually returns 304.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if os.path.exists(frontend_path):
    app.mount("/static", NoCacheStaticFiles(directory=frontend_path), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(frontend_path, "index.html"))


@app.get("/chroma")
async def chroma_viewer():
    return FileResponse(os.path.join(frontend_path, "chroma.html"))


@app.get("/graph")
async def knowledge_graph():
    return FileResponse(os.path.join(frontend_path, "graph.html"))
