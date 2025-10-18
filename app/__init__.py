from dotenv import load_dotenv
load_dotenv()
# app/__init__.py
import os
import re
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

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

from .models import db, User, Conversation, Message, Announcement

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
# Helpers: Redis availability
# ===============================
def choose_redis_url_or_memory() -> tuple[str, bool]:
    """
    REDIS_URL が使えればそれを返す。接続不可なら ('memory://', False) を返す。
    """
    redis_url = os.getenv("REDIS_URL") or os.getenv("VALKEY_URL")
    if not redis_url:
        logger.info("REDIS_URL not set -> using memory storage for limiter and disabling RQ.")
        return "memory://", False

    try:
        conn = redis_lib.from_url(redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
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
            rq_conn = redis_lib.from_url(os.getenv("REDIS_URL") or os.getenv("VALKEY_URL"))
            rq_queue = Queue("default", connection=rq_conn, default_timeout=180)
            app.extensions["rq_queue"] = rq_queue
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
        provider=os.getenv("SEARCH_PROVIDER", "google_cse"),
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
            convo_dump = [{"role": m.sender, "content": m.content} for m in msgs][-100:]

            analysis = gc.analyze_conversation(convo_dump)
            new_summary = (analysis.get("summary") or "").strip()

            if new_summary:
                conv.summary = new_summary

                sidebar_prompt = f"""
以下の会話要約をもとに、サイドバーで一覧表示するための「短いタイトル」を日本語で作成してください。
- 12〜18文字以内
- 名詞句（文にしない／句点不要）
- 出力は1行のみ
要約:
{new_summary}
"""
                short_title, _ = gc.chat([], sidebar_prompt)
                short_title = (short_title or "").strip().splitlines()[0]
                if not short_title:
                    short_title = (new_summary[:18] or "会話").strip()

                conv.title = short_title
                conv.updated_at = datetime.utcnow()
                db.session.commit()
                logger.info(f"✅ Generated summary/title for conversation {conversation_id}")
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
        users = User.query.order_by(User.id.asc()).all()
        conversations = Conversation.query.order_by(
            Conversation.is_pinned.desc(), Conversation.id.desc()
        ).limit(100).all()
        announcements = Announcement.query.order_by(
            Announcement.timestamp.desc().nullslast()
        ).all()
        return render_template(
            "admin_dashboard.html",
            users=users, conversations=conversations, announcements=announcements
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

        # 天気／ニュース判定 → 検索優先
        q_lower = msg.lower()
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

                guard = f"今日は {iso1}（JST）です。今日の情報のみ採用してください。過去日付は除外。"
                composed = guard + "\n\nユーザー入力: " + msg
                summary = gc.summarize_with_citations(composed, results, (data.get("model") or "").strip())

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
            except Exception as e:
                logger.warning(f"Search path failed, fallback to normal chat. reason={e}")

        # 通常チャット
        gc: GeminiClient = current_app.extensions["gemini_client"]
        try:
            history = [{"role": m.sender, "content": m.content}
                       for m in Message.query.filter_by(conversation_id=cid).order_by(Message.id.asc()).all()][-50:]
            reply, used = gc.chat(history, msg, requested_model=(data.get("model") or "").strip())
        except GeminiFallbackError as e:
            return jsonify({"ok": False, "error": str(e)}), 502

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
            # クエリタイプを判定
            date_keywords = ["日付", "今日", "date", "today"]
            time_sensitive_keywords = ["天気", "天候", "予報", "ニュース", "速報", "weather", "forecast", "news"]

            is_date_query = any(kw in query.lower() for kw in date_keywords)
            is_time_sensitive = any(kw in query.lower() for kw in time_sensitive_keywords)

            # 検索クエリの構築
            search_query = query if is_date_query else f"{query} {jp_full}"

            # 鮮度フィルタを動的に設定
            if is_time_sensitive:
                recency = 1  # 天気・ニュース: 過去1日
            else:
                recency = 7  # その他: 過去7日

            results = sc.search(search_query, top_k=int(data.get("top_k") or 10), recency_days=recency)
        except SearchError as e:
            error_msg = f"検索エラー: {str(e)}"
            db.session.add(Message(content=error_msg, sender="assistant", conversation_id=cid))
            db.session.commit()
            return jsonify({"ok": False, "error": str(e)}), 502

        try:
            # 日付クエリの場合は、現在の日付を明示的に伝える
            if is_date_query:
                guard = f"今日の日付は {iso1}（{jp_full}、JST）です。ユーザーが「今日の日付」を尋ねている場合、この日付を直接答えてください。"
            else:
                guard = f"今日は {iso1}（JST）です。今日の情報のみ採用し、過去日付は除外。"

            composed = guard + "\n\nユーザーの要望: " + query
            summary = gc.summarize_with_citations(composed, results, (data.get("model") or "").strip())

            # アシスタントの応答を保存
            reply = summary.get("answer") or summary.get("summary") or summary.get("text") or "情報を取得できませんでした。"
            db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
            db.session.commit()

            # バックグラウンドタスクでサマリーとタイトルを生成（Redisがなければ同期実行）
            q = current_app.extensions.get("rq_queue")
            if q:
                q.enqueue("services.tasks.generate_summary_and_title", cid, job_timeout=180)
            else:
                # Redisがない場合は同期的に生成
                _generate_summary_sync(cid)

            return jsonify({"ok": True, "conversation_id": cid, **summary})
        except GeminiFallbackError as e:
            error_msg = f"サマリー生成エラー: {str(e)}"
            db.session.add(Message(content=error_msg, sender="assistant", conversation_id=cid))
            db.session.commit()
            return jsonify({"ok": False, "error": "summarization failed", "details": str(e)}), 502

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



