# services/tasks.py
import os
import json
from app import db
from app.models import Conversation, Message, ResearchJob
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

    # Flaskアプリケーションコンテキストを取得
    from app import create_app
    app = create_app()

    with app.app_context():
        convo = db.session.get(Conversation, conversation_id)
        if not convo:
            print(f"[tasks] conversation {conversation_id} not found")
            return

        gemini = GeminiClient(
            primary_model=os.getenv("DEFAULT_GEMINI_MODEL", "gemini-1.5-flash"),
            fallback_model=os.getenv("FALLBACK_GEMINI_MODEL", "gemini-1.5-pro"),
            api_key=os.getenv("GEMINI_API_KEY")
        )
        msgs = db.session.query(Message).filter_by(conversation_id=conversation_id).order_by(Message.id.asc()).all()
        convo_dump = [{"role": m.sender, "content": m.content} for m in msgs][-100:]

        try:
            analysis = gemini.analyze_conversation(convo_dump)
            new_summary = (analysis.get("summary") or "").strip()
            if new_summary:
                convo.summary = new_summary
                # タイトルは自動生成しない(要約をサイドバーに表示するため)
                # ユーザーが手動でタイトルを設定することは可能
                convo.updated_at = datetime.utcnow()
                db.session.commit()

                print(f"[tasks] [OK] updated summary for conversation {conversation_id}")
            else:
                print(f"[tasks] [WARNING] empty summary returned for {conversation_id}")
        except GeminiFallbackError as e:
            print(f"[tasks] generate_summary_and_title failed: {e}")
        except Exception as e:
            print(f"[tasks] unexpected error: {e}")


def execute_deep_research(job_id: int):
    """
    Deep Research タスク（RQワーカーで実行）

    Args:
        job_id: ResearchJobのID（整数）
    """
    print(f"[tasks] execute_deep_research(job_id={job_id})")

    # Flaskアプリケーションコンテキストを取得
    from app import create_app
    app = create_app()

    with app.app_context():
        # ResearchJobレコードを取得
        job_record = db.session.get(ResearchJob, job_id)
        if not job_record:
            print(f"[tasks] [WARNING] ResearchJob record not found for job_id={job_id}, aborting")
            return {"error": "ResearchJob record not found"}

        query = job_record.query
        user_id = job_record.user_id
        conversation_id = job_record.conversation_id

        try:
            # DeepResearchEngineを初期化
            from services.deep_research import DeepResearchEngine
            from rq import get_current_job

            engine = DeepResearchEngine()

            # RQジョブオブジェクトを取得（進捗更新用）
            rq_job = get_current_job()

            # ステータスを更新: processing開始
            job_record.status = "processing"
            db.session.commit()

            # Deep Research実行
            result = engine.execute(query, job=rq_job)

            # 成功: データベースを更新
            job_record.status = "completed"
            job_record.result_report = result["report"]
            job_record.sub_queries = json.dumps(result["sub_queries"], ensure_ascii=False)
            job_record.sources_count = result["sources_count"]
            job_record.completed_at = datetime.utcnow()
            db.session.commit()

            print(f"[tasks] [OK] Deep research completed for job_id={job_id}")

            # 会話にメッセージを保存して要約を生成
            if conversation_id:
                try:
                    # AI応答（レポート）のみを保存
                    # ユーザーメッセージはフロントエンドですでに保存されているため不要
                    assistant_msg = Message(
                        content=result["report"],
                        sender="assistant",
                        conversation_id=conversation_id
                    )
                    db.session.add(assistant_msg)

                    # 会話のupdated_atを更新
                    conv = db.session.get(Conversation, conversation_id)
                    if conv:
                        conv.updated_at = datetime.utcnow()

                    db.session.commit()
                    print(f"[tasks] [OK] Saved deep research result to conversation {conversation_id}")

                    # 要約を非同期で生成（RQキューが利用可能な場合）
                    try:
                        from flask import current_app
                        rq_queue = current_app.extensions.get("rq_queue")
                        if rq_queue:
                            rq_queue.enqueue(
                                generate_summary_and_title,
                                conversation_id,
                                job_timeout="5m"
                            )
                            print(f"[tasks] [OK] Enqueued summary generation for conversation {conversation_id}")
                        else:
                            print(f"[tasks] [WARNING] RQ queue not available, skipping summary generation")
                    except Exception as summary_err:
                        print(f"[tasks] [WARNING] Failed to enqueue summary generation: {summary_err}")
                        import traceback
                        traceback.print_exc()

                except Exception as e:
                    print(f"[tasks] [WARNING] Failed to save to conversation: {e}")
                    db.session.rollback()

            return {
                "status": "completed",
                "report": result["report"],
                "sub_queries": result["sub_queries"],
                "sources_count": result["sources_count"],
                "citations": result.get("citations", [])
            }

        except Exception as e:
            print(f"[tasks] [ERROR] Deep research failed for job_id={job_id}: {e}")

            # 失敗: データベースを更新
            # まずロールバックしてセッションをクリーンな状態にする
            db.session.rollback()

            try:
                job_record = db.session.get(ResearchJob, job_id)
                if job_record:
                    job_record.status = "failed"
                    job_record.error_message = str(e)
                    job_record.completed_at = datetime.utcnow()
                    db.session.commit()
            except Exception as inner_e:
                print(f"[tasks] [ERROR] Failed to update job status to failed: {inner_e}")
                db.session.rollback()

            return {"error": str(e)}

        finally:
            # セッションをクリーンアップしてコネクションリークを防止
            db.session.remove()
