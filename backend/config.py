import os

from pydantic import model_validator
from pydantic_settings import BaseSettings

_APP_ROOT = os.path.dirname(os.path.dirname(__file__))
_ENV_FILE = os.path.join(_APP_ROOT, ".env")

# One port per tutor. 8002 is deliberately skipped: Dropzone binds it
# (dropzone/config.py), and a collision there would have each app's startup
# evict the other's Dropzone in a loop.
_APP_PORTS = {"pymentor": 8001, "sanskrit": 8003}


class Settings(BaseSettings):
    ANTHROPIC_API_KEY: str = ""
    # Sonnet 5.5. The tutor's job is explanation and diagnosis — noticing what a
    # learner actually got wrong and why — which is exactly where a stronger
    # model earns its cost. Override with MODEL_NAME in .env.
    MODEL_NAME: str = "claude-sonnet-5-5"

    # Which tutor this process is. Set by run.py from its command-line
    # argument, before backend.config is first imported.
    #
    # This is the only knob that matters: DATA_DIR, CHROMA_DIR and API_PORT are
    # all derived from it below, so the two tutors get separate stores and
    # separate ports without any storage module having to know an app exists.
    # Every store joins onto DATA_DIR, so scoping that scopes all of them.
    APP: str = "pymentor"

    # Empty/zero mean "derive from APP" — see _derive_app_paths.
    #
    # Sentinels rather than a @property or @computed_field, because those would
    # make the value read-only and an explicit DATA_DIR=/mnt/big in .env could
    # no longer override it. A set value is left alone; only an unset one is
    # filled in.
    DATA_DIR: str = ""
    CHROMA_DIR: str = ""
    API_PORT: int = 0

    # Data that is NOT per-app: shared read-only assets, keyed to the repo
    # rather than the tutor. vidyut's linguistic bundle is ~77MB and PyMentor
    # never touches it, so duplicating it per app would be pure waste.
    SHARED_DATA_DIR: str = os.path.join(_APP_ROOT, "data")

    # Where vidyut's linguistic data bundle lives. Empty means "the default
    # under SHARED_DATA_DIR"; set it to point at a pre-baked copy instead of
    # downloading at runtime. Its absence is never fatal — the Sanskrit tools
    # report what they cannot check, the same way a missing PyMuPDF only
    # disables PDF import. See backend/sanskrit_setup.py.
    VIDYUT_DATA_DIR: str = ""

    # Hosts the server may fetch a book PDF from, comma-separated. A leading
    # dot is not needed — subdomains of each entry are included.
    #
    # Starts CLOSED, and deliberately so: open_book lets the TUTOR pass a URL
    # it found by searching, so this is a server-side request with a
    # model-chosen destination. Every other SSRF check in bookresolve.py still
    # applies regardless of this list; set it empty to allow any public host.
    BOOK_FETCH_ALLOWED_HOSTS: str = "archive.org,googleapis.com,books.google.com"

    API_HOST: str = "127.0.0.1"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # How the CLI reaches the learning tools.
    #
    # "http"  — a loopback MCP endpoint this app mounts at /mcp-tools.
    # "sdk"   — the in-process server the SDK reaches back down its stdin pipe.
    #
    # http is the default because sdk transport has a hard 60-second ceiling:
    # the SDK holds stdin open only until the turn's first result, capped at
    # CLAUDE_CODE_STREAM_CLOSE_TIMEOUT, then closes it — after which every tool
    # call fails with "Stream closed". Measured on this project's logs: max age
    # of a successful call 59.3s, min age of a failure 60.2s, across 560 calls
    # and 29 tools with zero overlap. An http server never enters the SDK's
    # sdk_mcp_servers dict, so that branch is skipped entirely.
    #
    # Kept switchable so a problem with the HTTP path is one setting away from
    # the old behaviour, not a code change.
    MCP_TRANSPORT: str = "http"

    @model_validator(mode="after")
    def _derive_app_paths(self):
        """Fill in whatever APP implies, and reject an app we do not have.

        Runs after every field is populated, so it can see APP while deciding
        DATA_DIR, and see the final DATA_DIR while deciding CHROMA_DIR. That
        ordering is the point: CHROMA_DIR used to be hardcoded to
        <repo>/data/chroma, so overriding DATA_DIR alone left the two tutors
        silently sharing one vector store — the leak least likely to be
        noticed, because episodic memory is injected into every system prompt.

        Validating APP here rather than only in the API means a typo fails at
        startup instead of quietly serving a live process against data/sanskirt.
        """
        # Imported lazily: backend.curriculum is the single source of truth for
        # which apps exist, and a module-level import would make config and
        # curriculum import each other.
        from backend.curriculum import APP_TRACKS

        if self.APP not in APP_TRACKS:
            raise ValueError(
                f"Unknown app {self.APP!r}; expected one of {sorted(APP_TRACKS)}"
            )
        if not self.DATA_DIR:
            self.DATA_DIR = os.path.join(_APP_ROOT, "data", self.APP)
        if not self.CHROMA_DIR:
            self.CHROMA_DIR = os.path.join(self.DATA_DIR, "chroma")
        if not self.API_PORT:
            self.API_PORT = _APP_PORTS[self.APP]
        return self

    class Config:
        env_file = _ENV_FILE
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
