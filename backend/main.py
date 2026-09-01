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

    # And for book fetches: a download interrupted by a restart cannot resume,
    # and its chip would spin in the reader for the life of the tab.
    try:
        from backend.bookjobs import sweep_stale as sweep_book_jobs

        swept = sweep_book_jobs()
        if swept:
            logger.info(f"Resolved {swept} stale book job(s)")
    except Exception as e:
        logger.warning(f"Book job sweep failed: {e}")

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


# This process serves exactly ONE tutor, and its UI lives at the root.
#
# The alternative — both tutors in one process, told apart by a /sanskrit/
# prefix — is what this replaced. It shared a data root, a Chroma store and an
# active-session pointer between them, and the only thing keeping one learner's
# two tutors apart was having typed two different emails.
#
# APP_DIR is the front end for this app. Mounting it at /static rather than
# mounting all of frontend/ has a pleasant consequence: tokens.css exists in
# both roots, so the shared graph and memory pages ask for /static/tokens.css
# and get the right theme automatically, with no path sniffing.
APP_DIR = (
    os.path.join(frontend_path, "sanskrit")
    if settings.APP == "sanskrit"
    else frontend_path
)

if os.path.exists(APP_DIR):
    app.mount("/static", NoCacheStaticFiles(directory=APP_DIR), name="static")


@app.get("/")
async def index():
    """This app's chat UI.

    The two tutors share a backend but not a front end: the panes differ (no
    code runner or derivations in Sanskrit) as does the whole visual register.
    """
    return FileResponse(os.path.join(APP_DIR, "index.html"))


@app.get("/graph")
async def knowledge_graph():
    """The knowledge graph — one renderer, scoped by which process serves it.

    graph.html and chroma.html stay shared, unforked files. They learn which
    app they belong to from /api/app.js, which the server generates: the
    process IS the app, so the server is the only thing that unambiguously
    knows, and a bare /graph bookmark can never be ambiguous.
    """
    return FileResponse(os.path.join(frontend_path, "graph.html"))


@app.get("/memory")
@app.get("/chroma")   # the older name; kept so existing bookmarks still work
async def memory_explorer():
    return FileResponse(os.path.join(frontend_path, "chroma.html"))
