# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask-based chat application powered by Google Gemini AI with integrated web search capabilities. The app supports multi-user authentication, conversation management, admin controls, and background task processing with RQ (Redis Queue).

## Development Commands

### Running the Application

**Development server:**
```bash
python app.py
```
Starts Flask dev server on `http://0.0.0.0:5000` with debug mode enabled.

**Production server (gunicorn):**
```bash
gunicorn -c gunicorn.conf.py wsgi:app
```
Configured via `gunicorn.conf.py`.

### Database Management

**Initialize database:**
Database is automatically created on first run at `instance/database.db`.

**Run migrations:**
```bash
flask db migrate -m "migration message"
flask db upgrade
```

**Create admin user:**
```bash
python create_admin.py
```

### Dependencies

**Install requirements:**
```bash
pip install -r requirements.txt
```

## Architecture

### Application Factory Pattern

The app uses Flask's application factory pattern in `app/__init__.py`:
- `create_app()` initializes Flask, extensions, and blueprints
- All extensions (SQLAlchemy, Flask-Login, CSRF, Limiter, Talisman) are configured here
- **Redis fallback logic**: Automatically falls back to `memory://` storage if Redis is unavailable

### Key Components

**1. AI Client Layer (`services/gemini_client.py`)**
- `GeminiClient`: Manages Google Gemini API interactions
- **Model aliasing**: Automatically maps deprecated model names (gemini-1.5-*) to current versions (gemini-2.5-*)
- **Fallback mechanism**: Primary model → Fallback model → Error
- Supports chat conversations and search result summarization
- Uses `v1beta` API endpoint (configurable via `GOOGLE_API_VERSION`)

**2. Search Integration (`services/search.py`)**
- `SearchClient`: Abstraction layer for web search providers
- Supports Google Custom Search Engine (CSE) and SerpAPI
- **Recency filtering**: `recency_days` parameter for time-based searches
- Built-in retry logic with exponential backoff
- Normalizes results across different providers

**3. Background Tasks (`services/tasks.py`)**
- Uses RQ (Redis Queue) for async processing
- `generate_summary_and_title()`: Auto-generates conversation summaries and titles after each message
- Queue initialization happens conditionally based on Redis availability

**4. Freshness Logic for Time-Sensitive Queries**
Located in `app/__init__.py` at `/api/chat` endpoint (lines 415-453):
- Detects weather/news keywords in user messages
- Forces search path with site-specific biases (e.g., `site:tenki.jp` for weather)
- Injects current date context to prevent stale information
- Uses `recency_days=1` to filter to last 24 hours

**5. Security Hardening**
- CSRF protection via Flask-WTF (all POST/PATCH/DELETE require tokens)
- Rate limiting with Flask-Limiter (100 requests/minute default)
- Talisman for security headers (Referrer-Policy, HTTPS enforcement)
- XSS prevention via Bleach sanitization in `render_markdown_safe()`
- Session cookies: httponly, secure, SameSite=Lax

**6. Data Models (`app/models.py`)**
- `User`: Authentication with bcrypt hashed passwords, admin flag
- `Conversation`: Stores chat sessions with title, summary, pinned status
- `Message`: Individual messages linked to conversations
- `Announcement`: Admin-created announcements shown to users

### Request Flow

1. **User sends message** → `/api/chat` (POST)
2. **Weather/News detection** → If matched, triggers search path with freshness guards
3. **Search path** → `SearchClient.search()` → `GeminiClient.summarize_with_citations()`
4. **Normal chat path** → `GeminiClient.chat()` with conversation history (last 50 messages)
5. **Save response** → Database via SQLAlchemy
6. **Enqueue background task** → RQ job to generate summary/title (if Redis available)
7. **Return JSON** → Frontend renders response

### Frontend Architecture (`static/js/chat.js`, `templates/chat.html`)

**Single-page chat interface:**
- Sidebar: Conversation list with search/filter
- Main area: Message thread with markdown rendering
- Composer: Textarea with model selector and web search toggle

**Key frontend patterns:**
- CSRF token from meta tag included in all API calls
- localStorage for sidebar collapsed state
- Keyboard shortcuts: Enter to send, Shift+Enter for newline

## Environment Variables

Required:
- `GEMINI_API_KEY`: Google Gemini API key
- `SECRET_KEY`: Flask session secret

Optional:
- `REDIS_URL` or `VALKEY_URL`: For rate limiting and background tasks (auto-fallback to memory)
- `DEFAULT_GEMINI_MODEL`: Default model (defaults to `gemini-1.5-flash`, aliased to `gemini-2.5-flash`)
- `FALLBACK_GEMINI_MODEL`: Fallback model (defaults to `gemini-1.5-pro`, aliased to `gemini-2.5-pro`)
- `GOOGLE_API_VERSION`: API endpoint version (defaults to `v1beta`)
- `SEARCH_PROVIDER`: `google_cse` (default) or `serpapi`
- `GOOGLE_API_KEY`, `GOOGLE_CSE_ID`: For Google Custom Search
- `SERPAPI_API_KEY`: For SerpAPI

## Important Implementation Notes

**Model Name Normalization:**
All Gemini model names are normalized through `_MODEL_ALIASES` in `services/gemini_client.py`. When adding new models, update this mapping to maintain backward compatibility.

**Redis Conditional Logic:**
The app checks Redis availability at startup via `choose_redis_url_or_memory()` in `app/__init__.py:89-105`. If Redis is unavailable:
- Limiter uses in-memory storage
- RQ queue is disabled (`app.extensions["rq_queue"] = None`)
- Background tasks won't run but app remains functional

**Markdown Sanitization:**
All AI-generated content is rendered via `render_markdown_safe()` before display. Never bypass this function as it prevents XSS attacks. Allowed tags are strictly controlled in `_ALLOWED_TAGS`.

**Admin vs User Flow:**
Admin users have `is_admin=True` in the database. Admins can:
- Access `/admin_dashboard` to view all users, conversations, announcements
- Delete users and conversations
- Create/toggle announcements

**Search Result Freshness:**
When implementing new time-sensitive features, follow the pattern in `/api/chat` (lines 422-437):
1. Detect intent keywords
2. Add current date to query (both Japanese and ISO format)
3. Use `recency_days=1` for searches
4. Inject date guard in prompt to AI ("今日は YYYY-MM-DD です。今日の情報のみ採用してください。")

## Testing & Debugging

**Check available Gemini models:**
```bash
python check_models.py
```

**View logs:**
Application uses Python's `logging` module. Key loggers:
- `gemini_chat_app` (main app logger)
- Default Flask logger for requests

**Common issues:**
- **"NotFound" errors from Gemini**: Model name not available or API version mismatch. Check `_MODEL_ALIASES` and `GOOGLE_API_VERSION`.
- **RQ jobs not running**: Verify Redis is running and `REDIS_URL` is set correctly.
- **CSRF validation failed**: Ensure `csrf_token()` is present in templates and `X-CSRFToken` header is set in fetch requests.
