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
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(frontend_path, "index.html"))


@app.get("/chroma")
async def chroma_viewer():
    return FileResponse(os.path.join(frontend_path, "chroma.html"))


@app.get("/graph")
async def knowledge_graph():
    return FileResponse(os.path.join(frontend_path, "graph.html"))
