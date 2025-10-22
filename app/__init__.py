from dotenv import load_dotenv
load_dotenv()
# app/__init__.py
import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any

import markdown as md
import bleach
import redis as redis_lib
from rq import Queue

from flask import (
    Flask, Blueprint, render_template, request, jsonify, abort,
    current_app, redirect, url_for
)
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, login_required, current_user
from flask_talisman import Talisman
from flask_migrate import Migrate
from flask_bcrypt import Bcrypt

from .models import db, User, Conversation, Message, Announcement, ResearchJob

logger = logging.getLogger("gemini_chat_app")

# モックモードの判定
USE_MOCK = os.getenv("USE_MOCK_GEMINI", "false").lower() == "true"
if USE_MOCK:
    from services.gemini_client_mock import GeminiClient, GeminiFallbackError
    logger.warning("🔧 Using MOCK Gemini client (development mode)")
else:
    # HTTP版を使用（Python SDKのタイムアウト問題を回避）
    from services.gemini_client_http import GeminiClient, GeminiFallbackError
    logger.info("Using HTTP-based Gemini client")

from services.search import SearchClient, SearchError

# ===============================
# Markdown/XSS Safe Renderer
# ===============================
_ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union({
    "p", "br", "pre", "code", "blockquote",
    "ul", "ol", "li", "strong", "em",
    "h1", "h2", "h3", "h4",
    "table", "thead", "tbody", "tr", "th", "td"
})
_ALLOWED_ATTRS = {"a": ["href", "title", "rel", "target"]}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def _linkify_callback(attrs, new=False):
    href_key = (None, "href")
    if href_key not in attrs:
        return attrs
    attrs[(None, "rel")] = "nofollow noopener noreferrer"
    attrs[(None, "target")] = "_blank"
    return attrs


def render_markdown_safe(text: str) -> str:
    if not text:
        return ""
    html = md.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"]
    )
    try:
        html = bleach.linkify(
            html, callbacks=[_linkify_callback], skip_tags=["code", "pre"], parse_email=True
        )
    except Exception as e:
        logger.warning(f"bleach.linkify failed, fallback without linkify: {e}")
    clean = bleach.clean(
        html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS, strip=True
    )
    return clean


def clean_and_shorten_title(text: str, max_length: int = 18) -> str:
    if not text:
        return "会話"
    title = re.sub(r"[\r\n\t]+", " ", str(text)).strip()
    for ch in ['「', '」', '"', "'", '。', '、', '：', ':', '|', '/', '\\', '　']:
        title = title.replace(ch, "")
    title = re.sub(r"\s+", " ", title)
    if len(title) > max_length:
        title = title[:max_length - 1] + "…"
    return title.strip() or "会話"


# ===============================
# Helper: Summarizer selection
# ===============================
from typing import List, Dict, Any


def _summarize_with_citations(gc, query: str, results: List[Dict[str, Any]], requested_model: str = "") -> Dict[str, Any]:
    """Prefer enriched summarizer if available, fallback to default.

    This allows HTTP client to use enriched_content-aware prompt without
    changing the Python client which already handles enrichment internally.
    """
    try:
        if hasattr(gc, "summarize_with_citations_enriched"):
            return gc.summarize_with_citations_enriched(query, results, requested_model)
        return gc.summarize_with_citations(query, results, requested_model)
    except Exception:
        # Last resort: fall back to default method
        return gc.summarize_with_citations(query, results, requested_model)


# ===============================
# Helpers: Redis availability
# ===============================
def choose_redis_url_or_memory() -> tuple[str, bool]:
    """
    REDIS_URL が使えればそれを返す。接続不可なら ('memory://', False) を返す。
    Render のスタートアップ時に Redis が遅延起動することがあるため、
    長めのタイムアウト（5秒）を設定。
    """
    redis_url = os.getenv("REDIS_URL") or os.getenv("VALKEY_URL")
    if not redis_url:
        logger.info("REDIS_URL not set -> using memory storage for limiter and disabling RQ.")
        return "memory://", False

    try:
        # Render での遅延初期化に対応: 5秒のタイムアウト
        conn = redis_lib.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
        conn.ping()  # quick health check
        logger.info("Redis is available -> using Redis for limiter/RQ.")
        return redis_url, True
    except Exception as e:
        logger.warning(f"Redis check failed -> fallback to memory. reason={e}")
        return "memory://", False


# ===============================
# Flask Application Factory
# ===============================
def create_app() -> Flask:
    root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
        instance_path=str(root / "instance"),
        instance_relative_config=True,
    )
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    # Database configuration: PostgreSQL (production) or SQLite (development)
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # Render provides DATABASE_URL, but we need to handle postgres:// -> postgresql://
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        sqlalchemy_database_uri = database_url
    else:
        # Development: use SQLite
        db_path = Path(app.instance_path) / "database.db"
        sqlalchemy_database_uri = f"sqlite:///{db_path}"

    # SECRET_KEY validation
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        # Development fallback
        if os.getenv("FLASK_ENV") == "development" or not database_url:
            secret_key = "dev-secret-key-change-in-production"
            logger.warning("⚠️  Using development SECRET_KEY. Set SECRET_KEY environment variable in production!")
        else:
            raise RuntimeError("SECRET_KEY environment variable must be set in production!")

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=sqlalchemy_database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
        SESSION_COOKIE_HTTPONLY=os.getenv("SESSION_COOKIE_HTTPONLY", "true").lower() == "true",
        SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
        PERMANENT_SESSION_LIFETIME=int(os.getenv("PERMANENT_SESSION_LIFETIME", 604800)),
    )

    # セキュリティヘッダ（Referrer 対策・HTTPS 推奨）
    # ローカル開発環境では force_https を無効化
    force_https = os.getenv("FORCE_HTTPS", "false").lower() == "true"
    Talisman(
        app,
        content_security_policy=None,
        referrer_policy="strict-origin-when-cross-origin",
        force_https=force_https,
    )

    # CSRF
    CSRFProtect(app)

    # Bcrypt
    bcrypt = Bcrypt(app)
    app.extensions['bcrypt'] = bcrypt

    # Limiter: Redisが無ければmemory://へ自動フォールバック
    limiter_storage_uri, redis_ok = choose_redis_url_or_memory()
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=["100/minute"],
        storage_uri=limiter_storage_uri,
    )
    limiter.init_app(app)

    # RQ（Redisキュー）: RedisがOKのときだけ有効化
    if redis_ok:
        try:
            # Render での遅延初期化に対応: 5秒のタイムアウト
            rq_conn = redis_lib.from_url(
                os.getenv("REDIS_URL") or os.getenv("VALKEY_URL"),
                socket_connect_timeout=5,
                socket_timeout=5
            )
            rq_queue = Queue("default", connection=rq_conn, default_timeout=180)
            app.extensions["rq_queue"] = rq_queue
            logger.info("RQ queue initialized successfully")
        except Exception as e:
            logger.warning(f"RQ init failed -> disable queue. reason={e}")
            app.extensions["rq_queue"] = None
    else:
        app.extensions["rq_queue"] = None

    # DB + Migrate
    db.init_app(app)
    Migrate(app, db)

    # ログイン
    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.query.get(int(user_id))

    # 外部APIクライアント（モデル名は存在チェック付きの実装側でマッピング）
    app.extensions["gemini_client"] = GeminiClient(
        primary_model=os.getenv("DEFAULT_GEMINI_MODEL", "gemini-1.5-flash"),
        fallback_model=os.getenv("FALLBACK_GEMINI_MODEL", "gemini-1.5-pro"),
        api_key=os.getenv("GEMINI_API_KEY"),
    )
    app.extensions["search_client"] = SearchClient(
        provider=os.getenv("SEARCH_PROVIDER", "serpapi"),  # "google_cse" から "serpapi" に変更
        env=os.environ
    )

    # Blueprint登録
    from .auth import auth_bp
    app.register_blueprint(auth_bp)

    # データベーステーブルの作成
    # 本番環境ではFlask-Migrateを使用するため、開発環境のみ自動作成
    with app.app_context():
        if not database_url:  # SQLite (development)
            db.create_all()
            logger.info("Database tables created (development mode)")
        else:
            logger.info("Using Flask-Migrate for database management (production mode)")

    # ======================================================
    # Core Blueprint
    # ======================================================
    bp = Blueprint("core", __name__)

    def _json():
        try:
            return request.get_json(force=True)
        except Exception:
            abort(400, description="Invalid JSON")

    def _generate_summary_sync(conversation_id: int):
        """同期的に要約とタイトルを生成（Redisがない場合の代替）"""
        try:
            conv = Conversation.query.get(conversation_id)
            if not conv:
                return

            gc: GeminiClient = current_app.extensions["gemini_client"]
            msgs = Message.query.filter_by(conversation_id=conversation_id).order_by(Message.id.asc()).all()
            # Gemini API: role は "user" または "model" である必要があります
            convo_dump = [{"role": "model" if m.sender == "assistant" else "user", "content": m.content} for m in msgs][-100:]

            analysis = gc.analyze_conversation(convo_dump)
            new_summary = (analysis.get("summary") or "").strip()

            if new_summary:
                conv.summary = new_summary
                # タイトルは自動生成しない（要約をサイドバーに表示するため）
                # ユーザーが手動でタイトルを設定することは可能
                conv.updated_at = datetime.utcnow()
                db.session.commit()
                logger.info(f"✅ Generated summary for conversation {conversation_id}")
        except Exception as e:
            logger.warning(f"Summary generation failed: {e}")

    def _admin_required():
        if not (current_user.is_authenticated and getattr(current_user, "is_admin", False)):
            abort(403, description="admin required")

    # ----------------- Health -----------------
    @bp.route("/healthz")
    def healthz():
        return jsonify(status="ok")

    # ----------------- Pages -----------------
    @bp.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        return redirect(url_for("core.chat"))

    @bp.route("/chat")
    @login_required
    def chat():
        latest_announcement = Announcement.query.filter_by(is_active=True) \
            .order_by(Announcement.timestamp.desc()).first()
        return render_template(
            "chat.html",
            username=current_user.username,
            is_admin=bool(getattr(current_user, "is_admin", False)),
            announcement=latest_announcement
        )

    # ----------------- Admin Dashboard -----------------
    @bp.route("/admin_dashboard")
    @login_required
    def admin_dashboard():
        _admin_required()
        # Get users with conversation and message counts
        from sqlalchemy import func
        users = db.session.query(
            User,
            func.count(Conversation.id.distinct()).label('conversation_count'),
            func.count(Message.id).label('message_count')
        ).outerjoin(Conversation, User.id == Conversation.user_id)\
         .outerjoin(Message, Conversation.id == Message.conversation_id)\
         .group_by(User.id)\
         .order_by(User.id.asc())\
         .all()

        # Unpack query results
        users_with_stats = []
        for user, conv_count, msg_count in users:
            user.conversation_count = conv_count
            user.message_count = msg_count
            users_with_stats.append(user)

        conversations = db.session.query(Conversation).order_by(
            Conversation.is_pinned.desc(), Conversation.id.desc()
        ).limit(100).all()
        announcements = db.session.query(Announcement).order_by(
            Announcement.timestamp.desc().nullslast()
        ).all()
        return render_template(
            "admin_dashboard.html",
            users=users_with_stats, conversations=conversations, announcements=announcements
        )

    @bp.route("/admin/user/<int:user_id>")
    @login_required
    def user_detail(user_id: int):
        _admin_required()
        user = db.session.get(User, user_id)
        if not user:
            abort(404)

        # Get user statistics
        from sqlalchemy import func
        total_conversations = db.session.query(func.count(Conversation.id))\
            .filter(Conversation.user_id == user_id).scalar()
        total_messages = db.session.query(func.count(Message.id))\
            .join(Conversation, Message.conversation_id == Conversation.id)\
            .filter(Conversation.user_id == user_id).scalar()
        pinned_conversations = db.session.query(func.count(Conversation.id))\
            .filter(Conversation.user_id == user_id, Conversation.is_pinned == True).scalar()
        deep_research_jobs = db.session.query(func.count(ResearchJob.id))\
            .filter(ResearchJob.user_id == user_id).scalar()

        stats = {
            'total_conversations': total_conversations or 0,
            'total_messages': total_messages or 0,
            'pinned_conversations': pinned_conversations or 0,
            'deep_research_jobs': deep_research_jobs or 0
        }

        # Get recent conversations with message counts
        conversations = db.session.query(
            Conversation,
            func.count(Message.id).label('message_count')
        ).outerjoin(Message, Conversation.id == Message.conversation_id)\
         .filter(Conversation.user_id == user_id)\
         .group_by(Conversation.id)\
         .order_by(Conversation.updated_at.desc())\
         .limit(10).all()

        # Unpack and add message_count to conversations
        conversations_with_count = []
        for conv, msg_count in conversations:
            conv.message_count = msg_count
            conversations_with_count.append(conv)

        # Get recent research jobs
        research_jobs = db.session.query(ResearchJob)\
            .filter(ResearchJob.user_id == user_id)\
            .order_by(ResearchJob.id.desc())\
            .limit(10).all()

        return render_template(
            "user_detail.html",
            user=user,
            stats=stats,
            conversations=conversations_with_count,
            research_jobs=research_jobs
        )

    @bp.route("/admin/announcement/add", methods=["POST"])
    @login_required
    def add_announcement():
        _admin_required()
        msg = (request.form.get("message") or "").strip()
        is_active = bool(request.form.get("is_active"))
        if not msg:
            abort(400, description="message is required")
        a = Announcement(message=msg, is_active=is_active, timestamp=datetime.utcnow())
        db.session.add(a)
        db.session.commit()

        # ユーザー提供資料（DeepResearch など）を取り込む
        user_sources_raw = data.get("user_sources") or []
        user_sources: List[Dict[str, Any]] = []
        try:
            for src in user_sources_raw:
                if not isinstance(src, dict):
                    continue
                title = (src.get("title") or "ユーザー提供資料").strip()
                content = (src.get("content") or "").strip()
                chapter = (src.get("chapter") or "").strip()
                if not content:
                    continue
                user_sources.append({
                    "title": title,
                    "url": src.get("url") or f"user:{title}",
                    "snippet": content[:500],
                    "enriched_content": content,
                    "source": "user",
                    "chapter": chapter,
                })
        except Exception:
            user_sources = []
        return redirect(url_for("core.admin_dashboard"))

    @bp.route("/admin/announcement/<int:ann_id>/toggle", methods=["POST"])
    @login_required
    def toggle_announcement(ann_id: int):
        _admin_required()
        a = Announcement.query.get_or_404(ann_id)
        a.is_active = not bool(a.is_active)
        db.session.commit()
        return redirect(url_for("core.admin_dashboard"))

    @bp.route("/admin/announcement/<int:ann_id>/delete", methods=["POST"])
    @login_required
    def delete_announcement(ann_id: int):
        _admin_required()
        a = Announcement.query.get_or_404(ann_id)
        db.session.delete(a)
        db.session.commit()
        return redirect(url_for("core.admin_dashboard"))

    @bp.route("/admin/delete_user/<int:user_id>", methods=["POST"])
    @login_required
    def delete_user(user_id: int):
        _admin_required()
        user = User.query.get(user_id)
        if not user:
            abort(404)
        convs = Conversation.query.filter_by(user_id=user.id).all()
        for c in convs:
            Message.query.filter_by(conversation_id=c.id).delete()
            db.session.delete(c)
        db.session.delete(user)
        db.session.commit()
        return redirect(url_for("core.admin_dashboard"))

    @bp.route("/admin/delete_conversation/<int:cid>", methods=["POST"])
    @login_required
    def delete_conversation(cid: int):
        _admin_required()
        conv = Conversation.query.get(cid)
        if not conv:
            abort(404)
        Message.query.filter_by(conversation_id=cid).delete()
        db.session.delete(conv)
        db.session.commit()
        return redirect(url_for("core.admin_dashboard"))

    # ----------------- Conversations API -----------------
    @bp.route("/api/conversations", methods=["GET"])
    @login_required
    def list_conversations():
        q = (request.args.get("q") or "").strip().lower()
        items = Conversation.query.filter_by(user_id=current_user.id).order_by(
            Conversation.is_pinned.desc(), Conversation.id.desc()
        ).all()
        out = []
        for c in items:
            if q:
                hay = ((c.title or "") + " " + (c.summary or "")).lower()
                if q not in hay:
                    continue
            out.append({
                "id": c.id,
                "title": c.title,
                "summary": (c.summary or ""),
                "is_pinned": bool(c.is_pinned),
                "created_at": c.created_at.isoformat() if getattr(c, "created_at", None) else "",
            })
        return jsonify({"ok": True, "items": out})

    @bp.route("/api/conversations", methods=["POST"])
    @login_required
    def create_conversation():
        data = _json()
        title = (data.get("title") or f"新しい会話 {datetime.utcnow().strftime('%H:%M:%S')}").strip()
        conv = Conversation(title=title, user_id=current_user.id, is_pinned=False)
        db.session.add(conv)
        db.session.commit()
        return jsonify({"ok": True, "id": conv.id})

    @bp.route("/api/conversations/<int:cid>", methods=["PATCH"])
    @login_required
    def update_conversation(cid: int):
        conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first()
        if not conv:
            abort(404)
        data = _json()
        if "title" in data:
            t = (data.get("title") or "").strip()
            if t:
                conv.title = t
        if "is_pinned" in data:
            conv.is_pinned = bool(data.get("is_pinned"))
        db.session.commit()
        return jsonify({"ok": True})

    @bp.route("/api/conversations/<int:cid>", methods=["DELETE"])
    @login_required
    def remove_conversation(cid: int):
        conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first()
        if not conv:
            abort(404)
        Message.query.filter_by(conversation_id=cid).delete()
        db.session.delete(conv)
        db.session.commit()
        return jsonify({"ok": True})

    # ----------------- History (summary付き) -----------------
    @bp.route("/api/history/<int:conversation_id>", methods=["GET"])
    @login_required
    def api_history(conversation_id: int):
        conv = Conversation.query.filter_by(id=conversation_id, user_id=current_user.id).first()
        if not conv and not getattr(current_user, "is_admin", False):
            abort(404)
        msgs = Message.query.filter_by(conversation_id=conversation_id).order_by(Message.id.asc()).all()
        data = [{
            "role": m.sender,
            "content": m.content,
            "html": render_markdown_safe(m.content),
            "created_at": m.created_at.isoformat()
        } for m in msgs]
        return jsonify({
            "ok": True,
            "messages": data,
            "summary": conv.summary or ""
        })

    # ----------------- Chat API（鮮度ロジック内蔵） -----------------
    @bp.route("/api/chat", methods=["POST"])
    @login_required
    def api_chat():
        data = _json()
        msg = (data.get("message") or "").strip()
        if not msg:
            abort(400, description="message is required")

        cid = data.get("conversation_id")
        conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first() if cid else None
        if not conv:
            conv = Conversation(
                title=f"新しい会話 {datetime.utcnow().strftime('%H:%M:%S')}",
                user_id=current_user.id, is_pinned=False
            )
            db.session.add(conv)
            db.session.commit()
            cid = conv.id

        # 保存（ユーザ発話）
        db.session.add(Message(content=msg, sender="user", conversation_id=cid))
        db.session.commit()

        # 書籍要約判定 → 書籍専用検索パス
        import re
        q_lower = msg.lower()

        # 書籍要約リクエストの検出
        book_keywords = ["要約", "まとめ", "内容", "について", "目次", "章"]
        has_book_request = any(kw in msg for kw in book_keywords) and any(kw in msg for kw in ["本", "書籍", "著書"])

        # 日本語引用符で囲まれた書籍タイトルを検出
        book_title_match = re.search(r'[「『]([^」』]+)[」』]', msg)

        # 明示的な書籍名パターン（例: "安岡定子 実践・論語塾"）
        # または引用符内のテキスト
        if book_title_match or has_book_request:
            try:
                sc: SearchClient = current_app.extensions["search_client"]
                gc: GeminiClient = current_app.extensions["gemini_client"]

                # 書籍タイトルと著者名の抽出
                author = None
                if book_title_match:
                    book_title = book_title_match.group(1).strip()
                    # 著者名を抽出（"著者名 タイトル"のパターン）
                    author_title_pattern = r'^([^\s]+(?:\s+[^\s]+)?)\s+(.+)$'
                    author_match = re.search(author_title_pattern, book_title)
                    if author_match and len(author_match.group(1)) <= 10:  # 著者名は通常10文字以内
                        potential_author = author_match.group(1)
                        potential_title = author_match.group(2)
                        # 著者名らしい場合のみ分割
                        if any(c in potential_author for c in ['定子', '太郎', '花子', '一郎']) or \
                           potential_author.count(' ') == 0:
                            author = potential_author
                            book_title = potential_title
                else:
                    # キーワードから書籍タイトルを推測
                    # "について" の前の部分を書籍タイトルとして扱う
                    title_pattern = r'(.+?)(?:について|の本|の書籍|を要約|のまとめ)'
                    title_match = re.search(title_pattern, msg)
                    if title_match:
                        full_text = title_match.group(1).strip()
                        # 不要な接頭辞を削除
                        full_text = re.sub(r'^(次の|以下の|この)?(本|書籍|著書)?[:：]?\s*', '', full_text)

                        # 著者名とタイトルを分割（"著者名 タイトル"のパターン）
                        parts = full_text.split(None, 1)  # 最初の空白で分割
                        if len(parts) == 2 and len(parts[0]) <= 10:
                            author = parts[0]
                            book_title = parts[1]
                        else:
                            book_title = full_text
                    else:
                        # フォールバック: メッセージ全体から書籍名を抽出できない場合は通常パスへ
                        raise ValueError("書籍タイトルを抽出できませんでした")

                # 目次が含まれているかチェック
                toc_pattern = r'(序章|第[一二三四五六七八九十\d]+章|終章|第\d+章)'
                has_toc = bool(re.search(toc_pattern, msg))

                if has_toc:
                    # 目次部分を抽出
                    toc_lines = [line.strip() for line in msg.split('\n') if re.search(toc_pattern, line)]
                    table_of_contents = '\n'.join(toc_lines)
                else:
                    table_of_contents = "（目次情報なし）"

                # 書籍専用検索を実行（著者名も渡す）
                logger.info(f"Book search request detected: title='{book_title}', author='{author}'")
                results = sc.search_book_v2(book_title, author=author, top_k=10)
                # ユーザー提供資料があれば先頭にマージ（簡易重複排除）
                try:
                    if user_sources:
                        seen = set((r.get("url") or r.get("link") or r.get("info_link") or "") for r in results)
                        merged = []
                        for r in user_sources:
                            u = r.get("url") or ""
                            if u not in seen:
                                merged.append(r)
                                seen.add(u)
                        merged.extend(results)
                        results = merged
                except Exception:
                    pass

                if not results:
                    # 検索結果が0件の場合
                    reply = f"申し訳ございません。書籍「{book_title}」に関する信頼できる情報源が見つかりませんでした。書籍名を確認の上、再度お試しください。"
                    db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                    db.session.commit()
                else:
                    # 書籍専用要約を実行
                    if has_toc and table_of_contents:
                        summary = gc.summarize_book_with_toc(
                            book_title,
                            table_of_contents,
                            results,
                            (data.get("model") or "").strip()
                        )
                    else:
                        # 目次がない場合は通常の要約
                        query_for_summary = f"書籍「{book_title}」の内容を要約してください。出版社や書評サイトの情報を優先してください。"
                        summary = _summarize_with_citations(gc, query_for_summary, results, (data.get("model") or "").strip())

                    reply = summary.get("answer") or summary.get("summary") or summary.get("text") or "要約を生成できませんでした。"
                    db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                    db.session.commit()

                # バックグラウンドタスクでサマリー・タイトル生成
                q = current_app.extensions.get("rq_queue")
                if q:
                    q.enqueue("services.tasks.generate_summary_and_title", cid, job_timeout=180)
                else:
                    _generate_summary_sync(cid)

                return jsonify({
                    "ok": True, "reply": reply,
                    "reply_html": render_markdown_safe(reply),
                    "conversation_id": cid
                })
            except SearchError as e:
                logger.error(f"Book search path failed due to SearchError: {e}")
                reply = f"書籍検索に失敗しました。検索サービスの設定（APIキーなど）を確認してください。エラー: {e}"
                db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                db.session.commit()
                return jsonify({"ok": True, "reply": reply, "reply_html": render_markdown_safe(reply), "conversation_id": cid})
            except Exception as e:
                logger.error(f"Book search path failed unexpectedly: {e}")
                # 予期せぬエラーでもフォールバックせず、エラーを通知する
                reply = f"書籍検索中に予期せぬエラーが発生しました: {e}"
                db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                db.session.commit()
                return jsonify({"ok": True, "reply": reply, "reply_html": render_markdown_safe(reply), "conversation_id": cid})

        # 天気／ニュース判定 → 検索優先
        is_weather = any(w in msg for w in ["天気", "天候", "予報"]) or "weather" in q_lower or "forecast" in q_lower
        is_news = any(w in msg for w in ["ニュース", "速報"]) or "news" in q_lower or "headline" in q_lower

        if is_weather or is_news:
            try:
                JST = timezone(timedelta(hours=9))
                today = datetime.now(JST)
                jp_full = f"{today.year}年{today.month}月{today.day}日"
                iso1 = today.strftime("%Y-%m-%d")

                sc: SearchClient = current_app.extensions["search_client"]
                gc: GeminiClient = current_app.extensions["gemini_client"]

                site_bias = (
                    "site:tenki.jp OR site:weather.yahoo.co.jp OR site:jma.go.jp OR site:weather.com"
                    if is_weather else
                    "site:news.yahoo.co.jp OR site:www3.nhk.or.jp OR site:asahi.com OR site:mainichi.jp OR site:nikkei.com"
                )
                query1 = f"{msg} {jp_full} {site_bias}"
                results = sc.search(query1, top_k=10, recency_days=1)
                # ユーザー提供資料をマージ（任意）
                try:
                    if user_sources:
                        seen = set((r.get("url") or r.get("link") or r.get("info_link") or "") for r in results)
                        merged = []
                        for r in user_sources:
                            u = r.get("url") or ""
                            if u not in seen:
                                merged.append(r)
                                seen.add(u)
                        merged.extend(results)
                        results = merged
                except Exception:
                    pass

                guard = f"今日は {iso1}（JST）です。今日の情報のみ採用してください。過去日付は除外。"
                composed = guard + "\n\nユーザー入力: " + msg
                summary = _summarize_with_citations(gc, composed, results, (data.get("model") or "").strip())

                reply = summary.get("answer") or summary.get("summary") or summary.get("text") or "情報を取得できませんでした。"
                db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                db.session.commit()

                # バックグラウンドタスクでサマリー・タイトル生成（Redisがなければ同期実行）
                q = current_app.extensions.get("rq_queue")
                if q:
                    q.enqueue("services.tasks.generate_summary_and_title", cid, job_timeout=180)
                else:
                    # Redisがない場合は同期的に生成
                    _generate_summary_sync(cid)

                return jsonify({
                    "ok": True, "reply": reply,
                    "reply_html": render_markdown_safe(reply),
                    "conversation_id": cid
                })
            except SearchError as e:
                logger.error(f"News/Weather search path failed due to SearchError: {e}")
                reply = f"ニュース・天気検索に失敗しました。検索サービスの設定（APIキーなど）を確認してください。エラー: {e}"
                db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                db.session.commit()
                return jsonify({"ok": True, "reply": reply, "reply_html": render_markdown_safe(reply), "conversation_id": cid})
            except Exception as e:
                logger.error(f"News/Weather search path failed unexpectedly: {e}")
                reply = f"ニュース・天気検索中に予期せぬエラーが発生しました: {e}"
                db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                db.session.commit()
                return jsonify({"ok": True, "reply": reply, "reply_html": render_markdown_safe(reply), "conversation_id": cid})

        # 通常チャット
        gc: GeminiClient = current_app.extensions["gemini_client"]
        try:
            # Gemini API: role は "user" または "model" である必要があります
            # "assistant" は "model" に変換
            history = [{"role": "model" if m.sender == "assistant" else "user", "content": m.content}
                       for m in Message.query.filter_by(conversation_id=cid).order_by(Message.id.asc()).all()][-50:]
            reply, used = gc.chat(history, msg, requested_model=(data.get("model") or "").strip())
        except GeminiFallbackError as e:
            return jsonify({"ok": False, "error": str(e)}), 502

        db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
        db.session.commit()

        # 同期的に要約を生成
        _generate_summary_sync(cid)

        return jsonify({
            "ok": True,
            "reply": reply,
            "reply_html": render_markdown_safe(reply),
            "model": used,
            "conversation_id": cid
        })

    # ----------------- Search + Summarize（独立API） -----------------
    @bp.route("/api/search_summarize", methods=["POST"])
    @login_required
    def api_search_summarize():
        data = _json()
        query = (data.get("query") or "").strip()
        if not query:
            abort(400, description="query is required")

        # 会話IDを取得または作成
        cid = data.get("conversation_id")
        conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first() if cid else None
        if not conv:
            conv = Conversation(
                title=f"新しい会話 {datetime.utcnow().strftime('%H:%M:%S')}",
                user_id=current_user.id, is_pinned=False
            )
            db.session.add(conv)
            db.session.commit()
            cid = conv.id

        # ユーザーメッセージを保存
        db.session.add(Message(content=query, sender="user", conversation_id=cid))
        db.session.commit()

        JST = timezone(timedelta(hours=9))
        today = datetime.now(JST)
        jp_full = f"{today.year}年{today.month}月{today.day}日"
        iso1 = today.strftime("%Y-%m-%d")

        sc: SearchClient = current_app.extensions["search_client"]
        gc: GeminiClient = current_app.extensions["gemini_client"]

        try:
            # クエリタイプを判定（優先順位: 時間依存 > 時間非依存 > 日付クエリ）
            # ユーザー提供資料（DeepResearch など）
            user_sources_raw = data.get("user_sources") or []
            user_sources: List[Dict[str, Any]] = []
            try:
                for src in user_sources_raw:
                    if not isinstance(src, dict):
                        continue
                    title = (src.get("title") or "ユーザー提供資料").strip()
                    content = (src.get("content") or "").strip()
                    chapter = (src.get("chapter") or "").strip()
                    if not content:
                        continue
                    user_sources.append({
                        "title": title,
                        "url": src.get("url") or f"user:{title}",
                        "snippet": content[:500],
                        "enriched_content": content,
                        "source": "user",
                        "chapter": chapter,
                    })
            except Exception:
                user_sources = []

            q_lower = query.lower()

            # 時間依存クエリ: 最新情報が必要（最優先）
            time_sensitive_keywords = ["天気", "天候", "予報", "ニュース", "速報", "最新", "weather", "forecast", "news", "latest"]
            is_time_sensitive = any(kw in q_lower for kw in time_sensitive_keywords)

            # 時間非依存クエリ: 本、歴史、概念、定義など
            timeless_keywords = ["本", "書籍", "著者", "book", "歴史", "history", "とは", "意味", "定義", "definition",
                               "方法", "how to", "やり方", "概要", "要約", "summary", "解説", "explanation"]
            is_timeless = any(kw in q_lower for kw in timeless_keywords) and not is_time_sensitive

            # 日付クエリ: 今日の日付を聞いている（他に該当しない場合のみ）
            date_keywords = ["日付", "何日"]
            is_date_query = any(kw in q_lower for kw in date_keywords) and not is_time_sensitive and not is_timeless

            # 鮮度フィルタを動的に設定
            if is_time_sensitive:
                recency = 1  # 天気・ニュース: 過去1日
            elif is_timeless:
                recency = None  # 時間非依存: フィルタなし
            else:
                recency = 7  # その他: 過去7日

            # 検索の実行（本の場合は複数クエリで検索）
            results = []
            if is_timeless and any(kw in q_lower for kw in ["本", "書籍", "book"]):
                # 書籍名を抽出（「の本」「の書籍」などを削除）
                book_name = query
                for suffix in ["の本の情報を要約して", "の本を要約して", "の書籍を要約して", "の要約", "について", "の情報"]:
                    book_name = book_name.replace(suffix, "")
                book_name = book_name.strip()

                # 本の情報検索: 複数のクエリで検索し、結果をマージ
                book_queries = [
                    f'"{book_name}"',  # 完全一致検索
                    f"{book_name} 書評",
                    f"{book_name} 内容 目次",
                    f"{book_name} レビュー 感想",
                    f"{book_name} 著者",
                ]

                seen_urls = set()
                for bq in book_queries:
                    try:
                        partial = sc.search(bq, top_k=5, recency_days=recency)
                        for item in partial:
                            url = item.get("url", "")
                            if url and url not in seen_urls:
                                seen_urls.add(url)
                                results.append(item)
                                if len(results) >= 15:  # 最大15件
                                    break
                    except Exception as e:
                        logger.warning(f"Book search query failed: {bq}, error: {e}")
                        continue
                    if len(results) >= 15:
                        break
            elif is_date_query:
                results = sc.search(query, top_k=5, recency_days=recency)
            else:
                # 通常の検索
                search_query = query if (is_date_query or is_timeless) else f"{query} {jp_full}"
                top_k = int(data.get("top_k") or 10)
                results = sc.search(search_query, top_k=top_k, recency_days=recency)

            # ユーザー提供資料があれば先頭にマージ
            try:
                if 'user_sources' in locals() and user_sources:
                    seen = set((r.get("url") or r.get("link") or r.get("info_link") or "") for r in results)
                    merged = []
                    for r in user_sources:
                        u = r.get("url") or ""
                        if u not in seen:
                            merged.append(r)
                            seen.add(u)
                    merged.extend(results)
                    results = merged
            except Exception:
                pass
        except SearchError as e:
            logger.error(f"Search/Summarize path failed due to SearchError: {e}")
            reply = f"Web検索に失敗しました。検索サービスの設定（APIキーなど）を確認してください。エラー: {e}"
            db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
            db.session.commit()
            return jsonify({"ok": True, "answer": reply, "citations": []})

        # 検索結果が少ない場合の警告
        if len(results) == 0:
            error_msg = "検索結果が見つかりませんでした。書籍名を正確に入力するか、別の検索方法をお試しください。"
            db.session.add(Message(content=error_msg, sender="assistant", conversation_id=cid))
            db.session.commit()
            return jsonify({"ok": True, "answer": error_msg, "citations": []})

        logger.info(f"Found {len(results)} search results for query: {query}")

        try:
            # クエリタイプに応じたプロンプトを構築
            if is_date_query:
                guard = f"今日の日付は {iso1}（{jp_full}、JST）です。ユーザーが「今日の日付」を尋ねている場合、この日付を直接答えてください。"
            elif is_time_sensitive:
                guard = f"今日は {iso1}（{jp_full}、JST）です。最新の情報（過去24時間以内）を優先して採用してください。"
            elif is_timeless:
                # 本の検索の場合は専用のプロンプト
                if any(kw in q_lower for kw in ["本", "書籍", "book"]):
                    guard = (
                        f"参考日: {iso1}。以下は書籍に関する情報の要約タスクです。\n\n"
                        f"**検索結果数: {len(results)}件**\n\n"
                        "**重要な指示:**\n"
                        "1. 提供された検索結果を徹底的に分析し、書籍に関する情報を可能な限り抽出してください\n"
                        "2. 書籍のタイトル、著者、出版社、テーマ、概要などの基本情報を特定してください\n"
                        "3. 書評サイト、レビュー、Amazon、読書メーターなどの情報があれば活用してください\n"
                        "4. 目次や章立てが分かる場合は、各章の内容を推測も含めて説明してください\n"
                        "5. 著者の経歴や専門分野から、本の内容や意図を推測してください\n"
                        "6. 直接的な情報がない部分でも、関連情報から合理的に推測できる内容は含めてください\n"
                        "7. 検索結果が断片的でも、得られた情報を統合して有用な要約を作成してください\n"
                        "8. 「不明」とする前に、検索結果から読み取れることを最大限活用してください\n"
                        "9. 書籍の対象読者層や、どのような場面で役立つかも考察してください\n\n"
                        "**注意:** 検索結果に直接的な書籍情報がない場合でも、諦めずに関連する情報から推測して回答してください。"
                    )
                else:
                    guard = f"参考日: {iso1}（{jp_full}、JST）。以下は時間に依存しない情報の要約です。検索結果から信頼性の高い情報を総合的に分析してください。"
            else:
                guard = f"今日は {iso1}（JST）です。最新の情報を優先してください。"

            composed = guard + "\n\nユーザーの要望: " + query
            summary = _summarize_with_citations(gc, composed, results, (data.get("model") or "").strip())

            # アシスタントの応答を保存
            reply = summary.get("answer") or summary.get("summary") or summary.get("text") or "情報を取得できませんでした。"
            db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
            db.session.commit()

            # 同期的に要約を生成
            _generate_summary_sync(cid)

            return jsonify({"ok": True, "conversation_id": cid, **summary})
        except GeminiFallbackError as e:
            logger.error(f"Search/Summarize path failed due to GeminiFallbackError: {e}")
            reply = f"検索結果の要約生成に失敗しました。モデルのレート制限や設定を確認してください。エラー: {e}"
            db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
            db.session.commit()
            return jsonify({"ok": True, "answer": reply, "citations": []})

    # ----------------- Export -----------------
    @bp.route("/api/export/<int:cid>")
    @login_required
    def export(cid: int):
        conv = Conversation.query.get(cid)
        if not conv:
            abort(404)
        if conv.user_id != current_user.id and not getattr(current_user, "is_admin", False):
            abort(403)
        messages = Message.query.filter_by(conversation_id=cid).all()
        return jsonify({
            "id": conv.id,
            "title": conv.title,
            "created_at": getattr(conv, "created_at", None).isoformat() if getattr(conv, "created_at", None) else "",
            "messages": [{"role": m.sender, "content": m.content} for m in messages]
        })

    # ----------------- Deep Research API -----------------
    @bp.route("/api/deep_research", methods=["POST"])
    @login_required
    @limiter.limit("100 per hour")  # Testing: Temporarily increased for development (TODO: restore to 5 per hour in production)
    def create_deep_research():
        """
        Create a new Deep Research job.
        Request: {query: str, conversation_id?: int}
        Response: {ok: true, job_id: int, status: str} or {ok: false, error: str}
        """
        data = request.get_json()
        query = data.get("query", "").strip()
        conversation_id = data.get("conversation_id")

        if not query:
            return jsonify({"ok": False, "error": "Query is required"}), 400

        # Check if Redis/RQ is available
        rq_queue = current_app.extensions.get("rq_queue")
        if rq_queue is None:
            return jsonify({
                "ok": False,
                "error": "Deep Research service is temporarily unavailable (background queue not available)"
            }), 503

        # Verify conversation ownership if conversation_id provided
        if conversation_id:
            conv = db.session.get(Conversation, conversation_id)
            if not conv or conv.user_id != current_user.id:
                return jsonify({"ok": False, "error": "Invalid conversation"}), 403

        try:
            # Import task function
            from services.tasks import execute_deep_research

            # Create ResearchJob record
            import uuid
            task_id = str(uuid.uuid4())

            job = ResearchJob(
                task_id=task_id,
                user_id=current_user.id,
                conversation_id=conversation_id,
                query=query,
                status="pending",
                phase="initializing",
                progress_message="研究ジョブを初期化中..."
            )
            db.session.add(job)
            db.session.flush()  # Flush to get job.id without committing

            # Enqueue RQ task (if this fails, rollback will happen in except block)
            rq_job = rq_queue.enqueue(
                execute_deep_research,
                job.id,
                job_timeout="20m"  # 20 minutes timeout (increased from 10m)
            )

            # Only commit after successful enqueue
            db.session.commit()

            logger.info(f"[DeepResearch] Created job {job.id} for user {current_user.id}, RQ job {rq_job.id}")

            return jsonify({
                "ok": True,
                "job_id": job.id,
                "status": job.status,
                "message": "Deep Research job created successfully"
            }), 201

        except Exception as e:
            db.session.rollback()
            logger.exception(f"[DeepResearch] Failed to create job: {e}")
            # Return generic error to prevent leaking internal details
            return jsonify({"ok": False, "error": "An unexpected error occurred while creating the research job."}), 500

    @bp.route("/api/deep_research/status/<int:job_id>", methods=["GET"])
    @login_required
    def get_deep_research_status(job_id: int):
        """
        Get status of a Deep Research job.
        Response: {ok: true, job_id, status, phase, progress_message, sources_count, sub_queries}
        """
        job = db.session.get(ResearchJob, job_id)

        # Ownership check (critical security requirement)
        if not job or job.user_id != current_user.id:
            abort(404)  # Use 404 to avoid leaking info about existing jobs

        response = jsonify({
            "ok": True,
            "job_id": job.id,
            "status": job.status,
            "phase": job.phase,
            "progress_message": job.progress_message,
            "sources_count": job.sources_count,
            "sub_queries": json.loads(job.sub_queries) if job.sub_queries else [],
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "error": job.error_message if job.status == "failed" else None
        })
        # Prevent caching of polling responses
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response

    @bp.route("/api/deep_research/result/<int:job_id>", methods=["GET"])
    @login_required
    def get_deep_research_result(job_id: int):
        """
        Get result of a completed Deep Research job.
        Response: {ok: true, job_id, status, result_report, ...} or error if not completed
        """
        job = db.session.get(ResearchJob, job_id)

        # Ownership check (critical security requirement)
        if not job or job.user_id != current_user.id:
            abort(404)  # Use 404 to avoid leaking info about existing jobs

        # State validation: only return result if completed
        if job.status != "completed":
            return jsonify({
                "ok": False,
                "error": f"Job is not completed yet (current status: {job.status})",
                "status": job.status,
                "phase": job.phase,
                "progress_message": job.progress_message
            }), 400

        return jsonify({
            "ok": True,
            "job_id": job.id,
            "status": job.status,
            "query": job.query,
            "result_report": job.result_report,
            "sources_count": job.sources_count,
            "sub_queries": json.loads(job.sub_queries) if job.sub_queries else [],
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None
        })

    # ----------------- Error Handlers -----------------
    @bp.errorhandler(CSRFError)
    def handle_csrf(e):
        return jsonify({"ok": False, "error": "CSRF validation failed", "details": e.description}), 400

    @bp.errorhandler(Exception)
    def handle_exception(e):
        logger.exception("Unhandled error")
        return jsonify({"ok": False, "error": str(e)}), 500

    app.register_blueprint(bp)
    return app



