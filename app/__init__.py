# app/__init__.py
import os
import re
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

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

# Redis/RQ（あれば使う）
import redis as redis_lib
from rq import Queue

# Markdown/XSS 対策
import markdown as md
import bleach

from services.gemini_client import GeminiClient, GeminiFallbackError
from services.search import SearchClient, SearchError
from .models import db, User, Conversation, Message, Announcement

logger = logging.getLogger("gemini_chat_app")


# ===============================
# タイトル整形の共通関数
# ===============================
def clean_and_shorten_title(text: str, max_length: int = 18) -> str:
    if not text:
        return "会話"
    title = re.sub(r"[\r\n\t]+", " ", str(text)).strip()
    forbidden = ['「', '」', '"', "'", '。', '、', '：', ':', '|', '/', '\\', '　']
    for ch in forbidden:
        title = title.replace(ch, "")
    title = re.sub(r"\s+", " ", title)
    if len(title) > max_length:
        title = title[:max_length - 1] + "…"
    return title.strip() or "会話"


# ===============================
# 安全なMarkdownレンダラ（linkify例外に耐性）
# ===============================
_ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union({
    "p", "br", "pre", "code", "blockquote",
    "ul", "ol", "li",
    "strong", "em",
    "h1", "h2", "h3", "h4",
    "table", "thead", "tbody", "tr", "th", "td"
})
_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def _linkify_callback(attrs, new=False):
    # bleach>=6 の attrs は dict keys が (ns, name) なので tuple を使う
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
            html,
            callbacks=[_linkify_callback],
            skip_tags=["code", "pre"],
            parse_email=True
        )
    except Exception as e:
        logger.warning(f"bleach.linkify failed, fallback without linkify: {e}")
    clean = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True
    )
    return clean


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
    db_path = Path(app.instance_path) / "database.db"

    # --- 基本設定 ---
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # --- Cookie / セッション保護設定 ---
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
    app.config["SESSION_COOKIE_HTTPONLY"] = os.getenv("SESSION_COOKIE_HTTPONLY", "true").lower() == "true"
    app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    app.config["PERMANENT_SESSION_LIFETIME"] = int(os.getenv("PERMANENT_SESSION_LIFETIME", 604800))

    # --- CSRF 設定（Referrer 厳格チェックを緩和：Renderでの登録フォーム対策） ---
    app.config.setdefault("WTF_CSRF_SSL_STRICT", False)

    # --- Flask-Talisman（セキュリティヘッダ / Referer 対策 / 本番HTTPS） ---
    # Render での “Referrer missing” 回避のために referrer_policy を適切に。
    Talisman(
        app,
        content_security_policy=None,  # まずはCSPを緩めて機能優先（必要に応じて強化）
        referrer_policy="strict-origin-when-cross-origin",
        force_https=True,              # 実運用はHTTPS前提
        frame_options="SAMEORIGIN",
        strict_transport_security=True,
    )

    # --- 拡張 ---
    CSRFProtect(app)

    # Limiter: Redis があれば使う、無ければ memory:// にフォールバック（Renderでの接続拒否対策）
    redis_url = os.getenv("REDIS_URL") or os.getenv("VALKEY_URL")
    storage_uri = redis_url if redis_url else "memory://"
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=["100/minute"],
        storage_uri=storage_uri,
    )
    limiter.init_app(app)

    # RQ（Redis Queue）：REDIS_URL があるときだけ初期化
    rq_queue = None
    if redis_url:
        try:
            rq_conn = redis_lib.from_url(redis_url)
            rq_conn.ping()
            rq_queue = Queue("default", connection=rq_conn, default_timeout=180)
            logger.info("RQ queue connected.")
        except Exception as e:
            logger.warning(f"RQ init failed (continue without async): {e}")
            rq_queue = None
    app.extensions["rq_queue"] = rq_queue

    db.init_app(app)

    # Flask-Migrate
    Migrate(app, db)

    # --- ログイン ---
    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.query.get(int(user_id))

    # --- 外部クライアント ---
    app.extensions["gemini_client"] = GeminiClient(
        primary_model=os.environ.get("DEFAULT_GEMINI_MODEL", "gemini-2.0-pro"),
        fallback_model=os.environ.get("FALLBACK_GEMINI_MODEL", "gemini-2.0-flash"),
        api_key=os.environ.get("GEMINI_API_KEY"),
    )
    app.extensions["search_client"] = SearchClient(
        provider=os.environ.get("SEARCH_PROVIDER", "google_cse"),
        env=os.environ
    )

    # --- 認証BP ---
    from .auth import auth_bp
    app.register_blueprint(auth_bp)

    # --- 初期化 ---
    with app.app_context():
        db.create_all()

    # =======================================================
    # Core
    # =======================================================
    bp = Blueprint("core", __name__)

    # ヘルスチェック
    @bp.route("/healthz")
    def healthz():
        return jsonify(status="ok")

    # 429 Too Many Requests をJSONで
    @bp.app_errorhandler(429)
    def handle_ratelimit(e):
        return jsonify(error="Too Many Requests", detail=str(e.description)), 429

    # ----------------- 内部ユーティリティ -----------------
    def _json():
        try:
            return request.get_json(force=True)
        except Exception:
            abort(400, description="Invalid JSON")

    def _admin_required():
        if not (current_user.is_authenticated and current_user.is_admin):
            abort(403, description="admin required")

    # ----------------- Pages -----------------
    @bp.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        return redirect(url_for("core.chat"))

    @bp.route("/chat")
    @login_required
    def chat():
        latest_announcement = Announcement.query.filter_by(is_active=True)\
            .order_by(Announcement.timestamp.desc())\
            .first()
        return render_template(
            "chat.html",
            username=current_user.username,
            is_admin=bool(current_user.is_admin),
            conversation_id=datetime.utcnow().strftime("%Y%m%d%H%M%S%f"),
            announcement=latest_announcement
        )

    # ----------------- Admin -----------------
    @bp.route("/admin_dashboard")
    @login_required
    def admin_dashboard():
        _admin_required()
        users = User.query.order_by(User.id.asc()).all()
        conversations = Conversation.query.order_by(Conversation.is_pinned.desc(), Conversation.id.desc()).limit(100).all()
        announcements = Announcement.query.order_by(Announcement.timestamp.desc().nullslast()).all()
        return render_template("admin_dashboard.html", users=users, conversations=conversations, announcements=announcements)

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

    # ----------------- Conversations API (Sidebar) -----------------
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
                "title": c.title,                 # ← サイドバー表示
                "summary": (c.summary or ""),     # ← 上部要約
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

    # ----------------- History (summary付き + 安全HTML） -----------------
    @bp.route("/api/history/<int:conversation_id>", methods=["GET"])
    @login_required
    def api_history(conversation_id: int):
        conv = Conversation.query.filter_by(id=conversation_id, user_id=current_user.id).first()
        if not conv and not current_user.is_admin:
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

    # ----------------- Chat（天気/ニュースは検索経由で“今日”に強制寄せ） -----------------
    @bp.route("/api/chat", methods=["POST"])
    @login_required
    def api_chat():
        data = _json()
        msg = (data.get("message") or "").strip()
        if not msg:
            abort(400, description="message is required")

        # 会話ID：未指定/不正なら新規作成
        cid = data.get("conversation_id")
        conv = None
        if cid:
            conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first()
        if not conv:
            conv = Conversation(title=f"新しい会話 {datetime.utcnow().strftime('%H:%M:%S')}", user_id=current_user.id, is_pinned=False)
            db.session.add(conv); db.session.commit()
            cid = conv.id

        # 直近履歴（チャット用）
        last_msgs = Message.query.filter_by(conversation_id=cid).order_by(Message.id.asc()).all()
        history = [{"role": m.sender, "content": m.content} for m in last_msgs][-50:]

        # 1) 先にユーザ発言を保存
        db.session.add(Message(content=msg, sender="user", conversation_id=cid))
        db.session.commit()

        # --- 鮮度ロジック：天気/ニュースのときは検索経由で回答 ---
        q_lower = msg.lower()
        is_weather = any(w in msg for w in ["天気", "天候", "予報"]) or any(w in q_lower for w in ["weather", "forecast"])
        is_news    = any(w in msg for w in ["ニュース", "速報"]) or any(w in q_lower for w in ["news", "headline", "breaking"])

        if is_weather or is_news:
            try:
                JST = timezone(timedelta(hours=9))
                today_jst = datetime.now(JST).date()
                yyyy = today_jst.year; mm = today_jst.month; dd = today_jst.day
                jp_full = f"{yyyy}年{mm}月{dd}日"
                iso1 = f"{yyyy}-{mm:02d}-{dd:02d}"
                en1  = datetime.now(JST).strftime("%b %d, %Y")

                # 言語/ジオ
                is_japanese = bool(re.search(r"[ぁ-んァ-ン一-龥]", msg))
                gl = "jp" if is_japanese else None
                lr = "lang_ja" if is_japanese else None

                # 信頼ドメインに寄せる
                def add_site_bias(q: str) -> str:
                    if is_weather:
                        site = "site:tenki.jp OR site:weather.yahoo.co.jp OR site:jma.go.jp OR site:weather.com"
                        return f"{q} {site}"
                    if is_news:
                        site = "site:news.yahoo.co.jp OR site:www3.nhk.or.jp OR site:asahi.com OR site:mainichi.jp OR site:nikkei.com"
                        return f"{q} {site}"
                    return q

                sc: SearchClient = current_app.extensions["search_client"]
                gc: GeminiClient = current_app.extensions["gemini_client"]

                # 今日の日付を付与して 24h に寄せる
                query1 = add_site_bias(f"{msg} {jp_full}")
                results = sc.search(query1, top_k=5, recency_days=1, gl=gl, lr=lr)

                date_patterns = [jp_full, f"{mm}月{dd}日", iso1, f"{yyyy}/{mm:02d}/{dd:02d}", en1]

                def filter_today(rs):
                    outs = []
                    for r in rs:
                        hay = f"{r.get('title','')} {r.get('snippet','')} {r.get('url','')}"
                        if any(p in hay for p in date_patterns):
                            outs.append(r)
                    return outs

                today_hits = filter_today(results)

                if not today_hits:
                    # 緩めて48h
                    query2 = add_site_bias(msg)
                    results = sc.search(query2, top_k=5, recency_days=2, gl=gl, lr=lr)
                    today_hits = filter_today(results)

                final_results = today_hits or results

                guard = f"今日は {iso1}（JST）です。今日の情報のみ採用し、過去日付は無視してください。出典の更新日時を確認し、曖昧なら『最新の公式情報を確認してください』と注記。"
                composed_query = guard + "\n\nユーザー入力: " + msg
                summary = gc.summarize_with_citations(composed_query, final_results, (request.args.get("model") or "").strip())

                reply = summary.get("answer") or summary.get("summary") or summary.get("text") or summary.get("content")
                if not reply:
                    bullets = "\n".join([f"- [{r['title']}]({r['url']})" for r in final_results])
                    reply = f"最新の情報ソースです（{jp_full} 時点）。\n\n{bullets}"

                used = "search+summarize"

                # 保存
                db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
                db.session.commit()

                # 要約＋タイトル生成は RQ があれば非同期
                try:
                    q = current_app.extensions.get("rq_queue")
                    if q is not None:
                        q.enqueue("services.tasks.generate_summary_and_title", cid, job_timeout=180)
                    else:
                        logger.info("RQ not available; skip async summarization.")
                except Exception as e:
                    logger.warning(f"enqueue summary job failed: {e}")

                return jsonify({
                    "ok": True,
                    "reply": reply,
                    "reply_html": render_markdown_safe(reply),
                    "model": used,
                    "conversation_id": cid
                })

            except SearchError as e:
                logger.warning(f"fresh search in chat failed: {e}")
                # 検索失敗時は通常チャットにフォールバック

        # --- 通常のGeminiチャット ---
        gemini: GeminiClient = current_app.extensions["gemini_client"]
        try:
            reply, used = gemini.chat(
                messages=history,
                user_message=msg,
                requested_model=(data.get("model") or "").strip()
            )
        except GeminiFallbackError as e:
            return jsonify({"ok": False, "error": "Gemini fallback failed", "details": str(e)}), 502

        db.session.add(Message(content=reply, sender="assistant", conversation_id=cid))
        db.session.commit()

        # 要約＋タイトル生成は RQ があれば非同期
        try:
            q = current_app.extensions.get("rq_queue")
            if q is not None:
                q.enqueue("services.tasks.generate_summary_and_title", cid, job_timeout=180)
            else:
                logger.info("RQ not available; skip async summarization.")
        except Exception as e:
            logger.warning(f"enqueue summary job failed: {e}")

        return jsonify({
            "ok": True,
            "reply": reply,
            "reply_html": render_markdown_safe(reply),
            "model": used,
            "conversation_id": cid
        })

    # ----------------- Search + Summarize（鮮度＆日付ガード付き） -----------------
    @bp.route("/api/search_summarize", methods=["POST"])
    @login_required
    def api_search_summarize():
        data = _json()
        query = (data.get("query") or "").strip()
        if not query:
            abort(400, description="query is required")

        JST = timezone(timedelta(hours=9))
        today_jst = datetime.now(JST).date()
        yyyy = today_jst.year
        mm = today_jst.month
        dd = today_jst.day
        jp_full = f"{yyyy}年{mm}月{dd}日"
        jp_md   = f"{mm}月{dd}日"
        iso1    = f"{yyyy}-{mm:02d}-{dd:02d}"
        iso2    = f"{yyyy}/{mm:02d}/{dd:02d}"
        en1     = datetime.now(JST).strftime("%b %d, %Y")
        date_patterns = [jp_full, jp_md, iso1, iso2, en1]

        q_lower = query.lower()
        is_weather = any(w in query for w in ["天気", "天候", "予報"]) or any(w in q_lower for w in ["weather", "forecast"])
        is_news    = any(w in query for w in ["ニュース", "速報"]) or any(w in q_lower for w in ["news", "headline", "breaking"])

        is_japanese = bool(re.search(r"[ぁ-んァ-ン一-龥]", query))
        gl = "jp" if is_japanese else None
        lr = "lang_ja" if is_japanese else None

        sc: SearchClient = current_app.extensions["search_client"]
        gc: GeminiClient = current_app.extensions["gemini_client"]

        def add_site_bias(q: str) -> str:
            if is_weather:
                site = "site:tenki.jp OR site:weather.yahoo.co.jp OR site:weather.com OR site:jma.go.jp"
                return f"{q} {site}"
            if is_news:
                site = "site:news.yahoo.co.jp OR site:www3.nhk.or.jp OR site:news.livedoor.com OR site:asahi.com OR site:mainichi.jp OR site:nikkei.com"
                return f"{q} {site}"
            return q

        def filter_today(results):
            out = []
            for r in results:
                hay = f"{r.get('title','')} {r.get('snippet','')} {r.get('url','')}"
                if any(p in hay for p in date_patterns):
                    out.append(r)
            return out

        base_q = query
        if is_weather or is_news:
            base_q = f"{query} {jp_full}"
        biased_q = add_site_bias(base_q)

        try:
            results = sc.search(
                biased_q,
                top_k=int(data.get("top_k") or 5),
                recency_days=1,
                gl=gl,
                lr=lr,
            )
        except SearchError as e:
            return jsonify({"ok": False, "error": str(e)}), 502

        today_hits = filter_today(results)
        if not today_hits:
            try:
                results = sc.search(
                    add_site_bias(query),
                    top_k=int(data.get("top_k") or 5),
                    recency_days=2,
                    gl=gl,
                    lr=lr,
                )
            except SearchError as e:
                return jsonify({"ok": False, "error": str(e)}), 502
            today_hits = filter_today(results)

        final_results = today_hits or results

        guard_note = f"""
今日は {iso1}（JST）です。今日の情報のみを採用し、過去日付は無視してください。
本文に日付が無い場合は、見出し・URL・更新時刻を確認して判断してください。
""".strip()

        try:
            composed_query = guard_note + "\n\nユーザーの要望: " + query
            summary = gc.summarize_with_citations(composed_query, final_results, (data.get("model") or "").strip())
            return jsonify({"ok": True, **summary})
        except GeminiFallbackError as e:
            return jsonify({"ok": False, "error": "summarization failed", "details": str(e)}), 502

    # ----------------- Export -----------------
    @bp.route("/api/export/<int:cid>")
    @login_required
    def export(cid: int):
        conv = Conversation.query.get(cid)
        if not conv:
            abort(404)
        if conv.user_id != current_user.id and not current_user.is_admin:
            abort(403)
        messages = Message.query.filter_by(conversation_id=cid).all()
        return jsonify({
            "id": conv.id,
            "title": conv.title,
            "created_at": getattr(conv, "created_at", None).isoformat() if getattr(conv, "created_at", None) else "",
            "messages": [{"role": m.sender, "content": m.content} for m in messages]
        })

    # ----------------- Error handlers -----------------
    @bp.errorhandler(CSRFError)
    def handle_csrf(e):
        return jsonify({"ok": False, "error": "CSRF validation failed", "details": e.description}), 400

    @bp.errorhandler(Exception)
    def handle_exception(e):
        logger.exception("Unhandled error")
        return jsonify({"ok": False, "error": str(e)}), 500

    app.register_blueprint(bp)
    return app

