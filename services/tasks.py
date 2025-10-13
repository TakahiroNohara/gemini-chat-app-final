import logging
from flask import current_app
from app import create_app
from app.models import db, Conversation, Message
from services.gemini_client import GeminiClient

logger = logging.getLogger("gemini_chat_app.tasks")

# ここでも簡易版のタイトル整形（app/__init__.py と同等のロジック）
import re
def _clean_and_shorten_title(text: str, max_length: int = 18) -> str:
    if not text:
        return "会話"
    title = re.sub(r"[\r\n\t]+", " ", str(text)).strip()
    for ch in ['「', '」', '"', "'", '。', '、', '：', ':', '|', '/', '\\', '　']:
        title = title.replace(ch, "")
    title = re.sub(r"\s+", " ", title)
    if len(title) > max_length:
        title = title[:max_length - 1] + "…"
    return title.strip() or "会話"


def generate_summary_and_title(conversation_id: int) -> bool:
    """
    会話全体を要約し、DBの summary と title を更新する。
    失敗しても例外は投げず False を返す（チャット継続を阻害しない）。
    """
    app = create_app()
    with app.app_context():
        try:
            conv = Conversation.query.get(conversation_id)
            if not conv:
                logger.warning(f"[tasks] conversation not found: {conversation_id}")
                return False

            # 全メッセージから直近100件
            msgs = Message.query.filter_by(conversation_id=conversation_id).order_by(Message.id.asc()).all()
            convo_dump = [{"role": m.sender, "content": m.content} for m in msgs][-100:]

            gemini: GeminiClient = current_app.extensions["gemini_client"]
            analysis = gemini.analyze_conversation(convo_dump)  # {"summary": "..."}
            new_summary = (analysis.get("summary") or "").strip()

            if new_summary:
                conv.summary = new_summary
                # 短いタイトル生成
                try:
                    sidebar_prompt = f"""
以下の会話要約をもとに、サイドバーで一覧表示するための「超短いタイトル」を日本語で作成してください。
- 12〜18文字以内
- 名詞句（文にしない／句点不要）
- 必要なら先頭に絵文字1つ（例: 📝, 💡, 📅, ⚙️ など）
- 出力は1行のみ（改行なし）
要約:
{new_summary}
"""
                    short_title, _ = gemini.chat([], sidebar_prompt)
                    conv.title = _clean_and_shorten_title(short_title or new_summary)
                except Exception as e:
                    logger.warning(f"[tasks] sidebar title generation failed: {e}")
                    # 失敗時フォールバック
                    if conv.title.startswith("新しい会話 "):
                        conv.title = _clean_and_shorten_title(new_summary)

                db.session.commit()
                return True

            # 要約が取れない場合も成功扱い（無理に上書きしない）
            return True

        except Exception as e:
            logger.exception(f"[tasks] generate_summary_and_title failed: {e}")
            db.session.rollback()
            return False
