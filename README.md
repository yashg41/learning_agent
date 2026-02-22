# PyMentor — Python Learning Agent with Three-Tiered Memory

A long-term Python learning agent that remembers everything across sessions. Built with Claude Agent SDK, ChromaDB, and FastAPI.

## Architecture

The agent uses three memory tiers that work together:

| Tier | Name | Storage | Purpose |
|------|------|---------|---------|
| 1 | **Working Memory** | SDK context window | Current conversation flow. Auto-compacts when full. |
| 2 | **Episodic Memory** | ChromaDB (vector DB) | Searchable past conversation summaries. Enables "remember when we discussed decorators?" recall. |
| 3 | **Semantic Memory** | JSON files | Structured knowledge graph: concepts, mastery levels, prerequisites, quiz history. |

## Quick Start

```bash
cd learning_app

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set your API key
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Run
python run.py
```

Open **http://localhost:8001** in your browser.

### Pages

| URL | Description |
|-----|-------------|
| `/` | Main chat interface with knowledge dashboard and session picker |
| `/graph` | Interactive knowledge graph — force-directed visualization of 35 Python concepts with mastery overlay |
| `/chroma` | ChromaDB vector database viewer and visualizer |

## Project Structure

```
learning_app/
├── run.py                      # Entry point (uvicorn, port 8001)
├── requirements.txt            # Dependencies
├── .env                        # ANTHROPIC_API_KEY, MODEL_NAME
├── backend/
│   ├── config.py               # Settings (Pydantic BaseSettings)
│   ├── main.py                 # FastAPI app
│   ├── agent.py                # SDK wrapper — wires all 3 memory tiers
│   ├── episodic.py             # Tier 2: ChromaDB conversation store + image handling
│   ├── knowledge.py            # Tier 3: JSON knowledge graph + curriculum (35 concepts)
│   ├── tools.py                # 9 MCP tools
│   ├── prompts.py              # System prompt template
│   ├── memory.py               # Conversation turn persistence
│   ├── replay.py               # Session recovery
│   └── api/
│       └── routes.py           # API endpoints
├── frontend/
│   ├── index.html              # 3-column UI (chat + knowledge dashboard + session picker)
│   ├── graph.html              # Interactive knowledge graph (force-directed, Canvas)
│   ├── chroma.html             # ChromaDB vector database viewer
│   ├── app.js                  # SSE streaming + markdown rendering + session management
│   └── styles.css              # Dark theme with markdown support
└── data/                       # Persistent data (created at runtime)
    ├── chroma/                 # ChromaDB vector embeddings
    ├── uploads/                # Saved images (captions stored in ChromaDB)
    └── users/<email>/          # Per-user data
        ├── user.json           # Session list + active session
        ├── knowledge.json      # Concept tracker (shared across sessions)
        ├── quiz_history.json   # Quiz results (shared across sessions)
        └── sessions/
            └── <session_id>/   # Per-session data
                ├── conversation.json   # Raw conversation turns
                ├── metadata.json       # Model, turn count
                ├── summary.json        # SDK compacted summary
                └── turns/              # Individual turn files
```

---

## API Endpoints

Base URL: `http://localhost:8001/api`

### Chat

| Method | Path | Description | Request Body |
|--------|------|-------------|--------------|
| `POST` | `/api/chat/stream` | Send a message and stream back events as SSE | `{ "message": "string", "email": "string", "session_id": "string?" }` |
| `POST` | `/api/chat` | Non-streaming chat (waits for full response) | `{ "message": "string", "email": "string", "session_id": "string?" }` |

If `session_id` is omitted, the user's active session is used (or a new one is created).

**SSE Event Types** (from `/api/chat/stream`):

| Event Type | Fields | Description |
|------------|--------|-------------|
| `session_init` | `session_id` | New or resumed session ID |
| `assistant_message` | `content` | Streamed text from the agent |
| `tool_call` | `tool_name`, `tool_input`, `tool_use_id` | Agent is calling an MCP tool |
| `tool_result` | `tool_use_id`, `content` | Result from a tool execution |
| `result` | `content`, `subtype` | Final agent response |
| `error` | `content` | Error message |
| `done` | — | Stream complete |

### Knowledge (Semantic Memory)

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| `GET` | `/api/knowledge/{email}` | Get a user's knowledge graph | `{ profile, concepts, learning_path }` |
| `GET` | `/api/quiz/{email}` | Get a user's quiz history | `{ quizzes, stats }` |

### Episodic Memory

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| `GET` | `/api/episodes/{email}` | Get recent conversation summaries | `{ episodes, count }` |

### ChromaDB Visualization

| Method | Path | Description | Request/Params |
|--------|------|-------------|----------------|
| `GET` | `/api/chroma/stats` | Collection metadata (name, doc count, model, dimensions) | — |
| `GET` | `/api/chroma/documents` | All documents with embeddings projected to 2D via PCA | `?email=` (optional filter), `?limit=100` |
| `POST` | `/api/chroma/search` | Semantic similarity search across all episodes | `{ "query": "string", "email": "string?", "n_results": 10 }` |

**`/api/chroma/documents` response:**
```json
{
  "documents": [{ "id": "...", "document": "...", "email": "...", "timestamp": "...", "topics": [...] }],
  "points_2d": [{ "x": 0.1234, "y": -0.5678 }],
  "count": 11
}
```

**`/api/chroma/search` response:**
```json
{
  "results": [{ "document": "...", "email": "...", "topics": [...], "similarity": 0.7312 }],
  "query": "object oriented programming"
}
```

### Sessions

| Method | Path | Description | Request Body |
|--------|------|-------------|--------------|
| `GET` | `/api/sessions/{email}` | List all sessions for a user | — |
| `POST` | `/api/sessions/{email}` | Create a new empty session | `{ "name": "string?" }` |
| `PATCH` | `/api/sessions/{email}/{session_id}` | Rename a session | `{ "name": "string" }` |
| `POST` | `/api/sessions/{email}/{session_id}/activate` | Switch active session | — |

### Knowledge Graph

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| `GET` | `/api/graph/{email}` | Get curriculum graph with user mastery overlaid | `{ nodes, edges, categories, profile }` |

**`/api/graph/{email}` response:**
```json
{
  "nodes": [{ "id": "lists", "name": "Lists", "category": "data_structures", "color": "#9ece6a", "mastery": "practiced", "review_count": 3 }],
  "edges": [{ "source": "variables", "target": "lists" }],
  "categories": [{ "id": "fundamentals", "name": "Fundamentals", "color": "#7aa2f7" }],
  "profile": { "level": "beginner", "strengths": [], "struggle_areas": [] }
}
```

### Users & Info

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| `GET` | `/api/memory/users` | List all users with session info | `{ users: [...] }` |
| `GET` | `/api/memory/users/{email}` | Get a user's sessions | `{ email, sessions: [...] }` |
| `GET` | `/api/tools` | List all MCP tool names | `{ tools: [...] }` |

---

## MCP Tools

9 tools available to the agent via the `learning-tools` MCP server. Tool names follow the `mcp__learning-tools__<name>` convention.

### Tier 2: Episodic Memory (ChromaDB)

| # | Tool Name | Purpose | When Called |
|---|-----------|---------|-------------|
| 1 | `search_past_conversations` | Semantic search over past discussion summaries | User references a past conversation ("remember when we...") |
| 2 | `save_conversation_summary` | Store a conversation summary with topic tags | After teaching a concept or significant discussion |
| 3 | `save_image_memory` | Save an image to disk, store only caption + path in memory | User shares a screenshot, diagram, or code image |

**`search_past_conversations`**
```json
{
  "query": "string (required) — natural language search query",
  "n_results": "integer (optional, default: 5, max: 20)"
}
```

**`save_conversation_summary`**
```json
{
  "summary": "string (required) — 2-4 sentence summary",
  "topics": ["string"] // required — topic tags e.g. ["lists", "indexing"]
}
```

**`save_image_memory`**
```json
{
  "image_base64": "string (required) — base64-encoded image",
  "caption": "string (required) — AI description of the image",
  "content_type": "string (optional, default: image/png)"
}
```

### Tier 3: Semantic Memory (JSON Knowledge Graph)

| # | Tool Name | Purpose | When Called |
|---|-----------|---------|-------------|
| 4 | `get_learning_progress` | Read full knowledge state, quiz stats, suggestions | Agent needs detailed view beyond system prompt |
| 5 | `update_concept` | Add or update a concept's mastery level | After teaching or when learner demonstrates understanding |
| 6 | `suggest_next_topics` | Get prerequisite-aware topic suggestions from curriculum graph | When deciding what to teach next |
| 7 | `get_quiz_topics` | Get concepts ranked by quiz priority (stalest, lowest mastery first) | User asks for a quiz |
| 8 | `record_quiz_result` | Save quiz results and auto-promote mastery levels | After user completes a quiz |
| 9 | `update_learner_profile` | Update learner's level, strengths, struggle areas, preferences | Agent notices patterns in learner's abilities |

**`get_learning_progress`** — no parameters

**`update_concept`**
```json
{
  "concept_id": "string (required) — snake_case ID e.g. 'list_comprehensions'",
  "name": "string (required) — human-readable e.g. 'List Comprehensions'",
  "category": "string (required) — fundamentals|control_flow|data_structures|functions|oop|error_handling|file_io|modules|advanced",
  "mastery": "string (required) — introduced|practiced|mastered",
  "prerequisites": ["string"],  // optional — concept IDs
  "related": ["string"],        // optional — concept IDs
  "notes": "string"             // optional — what was covered
}
```

**`suggest_next_topics`**
```json
{
  "count": "integer (optional, default: 3)"
}
```

**`get_quiz_topics`**
```json
{
  "count": "integer (optional, default: 5)",
  "category": "string (optional) — filter by category"
}
```

**`record_quiz_result`**
```json
{
  "quiz_id": "string (required) — unique ID e.g. 'q_20260222_143000'",
  "topics": ["string (required) — concept IDs tested"],
  "questions": [
    {
      "question": "string",
      "user_answer": "string",
      "correct": "boolean",
      "concept": "string — concept ID"
    }
  ],
  "score": "integer (required)",
  "total": "integer (required)"
}
```

**`update_learner_profile`**
```json
{
  "level": "string (optional) — beginner|intermediate|advanced",
  "strengths": ["string"],       // optional — concept IDs
  "struggle_areas": ["string"],  // optional — concept IDs
  "preferences": "string"        // optional — learning style notes
}
```

---

## ChromaDB Viewer (`/chroma`)

A built-in visualization page for inspecting the episodic memory vector database. Access at `http://localhost:8001/chroma`.

**Features:**
- **Stats bar** — Collection name, document count, embedding dimensions (384d), model (`all-MiniLM-L6-v2`), distance metric (cosine)
- **Embedding scatter plot** — PCA projection of 384-dimensional embeddings to 2D, color-coded by user, with hover tooltips
- **Document browser** — All stored episodes with IDs, summaries, emails, timestamps, and topic tags
- **Semantic search** — Type a natural language query to find similar episodes, ranked by cosine similarity with match percentages
- **User filter** — Dropdown to filter documents by email
- **Interactive linking** — Hovering scatter points highlights the corresponding document card, and vice versa

The 2D projection uses PCA implemented with pure numpy (no sklearn dependency). Embeddings are generated by ChromaDB's default `all-MiniLM-L6-v2` model (22MB, runs locally).

---

## Knowledge Graph Viewer (`/graph`)

An interactive force-directed graph of the 35-concept Python curriculum. Access at `http://localhost:8001/graph`.

**Features:**
- **Force-directed layout** — Nodes repel each other, edges pull connected concepts together, auto-stabilizes
- **Mastery overlay** — Node size and style reflect the user's mastery level (not started / introduced / practiced / mastered)
- **Category coloring** — 9 categories each with a distinct color (fundamentals, control flow, data structures, functions, OOP, error handling, file I/O, modules, advanced)
- **Prerequisite edges** — Arrows show which concepts must be learned before others
- **Interactive** — Drag nodes, scroll to zoom, pan the canvas, hover for detailed tooltips
- **Sidebar** — Email input, category legend, mastery legend, and stats summary

Built with pure Canvas API (no external graph libraries).

---

## Multi-Session Support

Each user can have multiple independent conversation sessions. Knowledge (concepts, mastery, quizzes) is shared across all sessions, but conversation history is per-session.

- Sessions auto-created on first message
- "New Session" button (+) in the left panel to start a fresh conversation
- Click any session in the list to switch to it
- Sessions auto-named from the first message (truncated to 40 chars)
- Active session highlighted with accent border

---

## Frontend

Three-column layout with session management:

**Left panel:**
- Email input
- Session picker — list of sessions with active indicator, "+" button for new session
- Navigation links — Knowledge Graph and Memory Explorer open in new tabs

**Center panel (chat):**
- Full markdown rendering: code blocks, inline code, headers, bold/italic, lists, horizontal rules
- Tool cards with clean names (MCP prefix stripped), input summaries, and expandable JSON detail
- SSE streaming for real-time responses

**Right panel (knowledge dashboard)** — four tabs:
- **Progress** — Mastery level header + concept list grouped by introduced/practiced/mastered
- **History** — Episodic conversation summaries with topic tags
- **Quizzes** — Quiz scores with accuracy stats
- **Events** — Raw SSE event stream log

---

## Mastery Levels

| Level | Meaning | Promotion Trigger |
|-------|---------|-------------------|
| `introduced` | Concept was explained for the first time | Agent teaches a new concept |
| `practiced` | Learner wrote code or answered questions using it | Correct quiz answer on `introduced` concept, or agent observes practice |
| `mastered` | Learner demonstrated deep understanding | Correct quiz answer on `practiced` concept |

---

## Curriculum Graph

35 Python concepts with prerequisite chains. The `suggest_next_topics` tool traverses this graph to find concepts whose prerequisites are all met but which haven't been covered yet.

```
fundamentals:     variables, data_types, operators, string_formatting, input_output
control_flow:     if_else, for_loops, while_loops, match_statement
data_structures:  lists, tuples, dictionaries, sets, list_comprehensions
functions:        functions_basic, functions_args, scope, lambda_functions, closures, decorators
oop:              classes_basic, inheritance, dunder_methods
error_handling:   error_handling, custom_exceptions
file_io:          file_reading, file_writing, context_managers
modules:          modules_imports, packages, virtual_environments
advanced:         generators, iterators, async_await, type_hints
```

---

## Image Handling

Images are **never stored as base64 in the vector database**. Instead:

1. Image file saved to `data/uploads/<email>/`
2. Agent generates a text caption describing the image
3. Only the caption + file path are stored in ChromaDB

This reduces a 1MB image to ~30 text tokens in long-term memory while keeping vector searches fast.

---

## Configuration

Environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required. Your Anthropic API key. |
| `MODEL_NAME` | `claude-sonnet-4-20250514` | Claude model to use |

Settings in `backend/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `DATA_DIR` | `./data` | Root directory for all persistent data |
| `CHROMA_DIR` | `./data/chroma` | ChromaDB storage |
| `API_HOST` | `127.0.0.1` | Server host |
| `API_PORT` | `8001` | Server port |
| `DEBUG` | `true` | Enable hot reload |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Dependencies

```
claude-agent-sdk==0.1.34    # Agent loop, session management, MCP tools
chromadb                     # Vector database (in-process, uses all-MiniLM-L6-v2 embeddings)
fastapi                      # Web framework
uvicorn[standard]            # ASGI server
pydantic-settings            # Configuration management
python-dotenv                # .env file loading
numpy                        # PCA for embedding visualization (also a chromadb dependency)
```
