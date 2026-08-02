"""Entrypoint for Dropzone. Mirrors run.py, but never with reload=True —
a file-watch restart would drop SSE connections and wipe the buffer mid-session.
"""

import uvicorn

from dropzone.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "dropzone.main:app",
        host=settings.DROPZONE_HOST,
        port=settings.DROPZONE_PORT,
        reload=False,
        log_level="warning",
    )
