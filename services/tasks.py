# services/tasks.py
import os
from app import db
from app.models import Conversation, Message
from datetime import datetime

# モックモードの判定
USE_MOCK = os.getenv("USE_MOCK_GEMINI", "false").lower() == "true"
if USE_MOCK:
    from services.gemini_client_mock import GeminiClient, GeminiFallbackError
else:
    # HTTP版を使用（Python SDKのタイムアウト問題を回避）
    from services.gemini_client_http import GeminiClient, GeminiFallbackError

def generate_summary_and_title(conversation_id: int):
    """非同期で要約と短縮タイトルを生成"""
    print(f"[tasks] generate_summary_and_title({conversation_id})")

    convo = Conversation.query.get(conversation_id)
    if not convo:
        print(f"[tasks] conversation {conversation_id} not found")
        return

    gemini = GeminiClient(
        primary_model=os.getenv("DEFAULT_GEMINI_MODEL", "gemini-1.5-flash"),
        fallback_model=os.getenv("FALLBACK_GEMINI_MODEL", "gemini-1.5-pro"),
        api_key=os.getenv("GEMINI_API_KEY")
    )
    msgs = Message.query.filter_by(conversation_id=conversation_id).order_by(Message.id.asc()).all()
    convo_dump = [{"role": m.sender, "content": m.content} for m in msgs][-100:]

    try:
        analysis = gemini.analyze_conversation(convo_dump)
        new_summary = (analysis.get("summary") or "").strip()
        if new_summary:
            convo.summary = new_summary

            sidebar_prompt = f"""
以下の会話要約をもとに、サイドバーで一覧表示するための「短いタイトル」を日本語で作成してください。
- 12〜18文字以内
- 名詞句（文にしない／句点不要）
- 出力は1行のみ
要約:
{new_summary}
"""
            short_title, _ = gemini.chat([], sidebar_prompt)
            short_title = (short_title or "").strip().splitlines()[0]
            if not short_title:
                short_title = (new_summary[:18] or "会話").strip()

            convo.title = short_title
            convo.updated_at = datetime.utcnow()
            db.session.commit()

            print(f"[tasks] ✅ updated summary/title for conversation {conversation_id}")
        else:
            print(f"[tasks] ⚠️ empty summary returned for {conversation_id}")
    except GeminiFallbackError as e:
        print(f"[tasks] generate_summary_and_title failed: {e}")
    except Exception as e:
        print(f"[tasks] unexpected error: {e}")
