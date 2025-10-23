#!/usr/bin/env python
"""
RQ Worker スタートアップスクリプト
Render での環境変数展開問題を回避するため、
Python から直接 RQ ワーカーを起動する
"""
import os
import sys
import time
import logging
from typing import Optional, Tuple
from rq import Worker, Queue
import redis
from redis.client import Redis

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 環境変数から設定を読み込む（デフォルト値を持つ）
MAX_RETRIES = int(os.getenv("RQ_MAX_RETRIES", "5"))
RETRY_BACKOFF = int(os.getenv("RQ_RETRY_BACKOFF", "2"))

def validate_redis_url_format(redis_url: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Redis URL が有効な形式かチェック

    Args:
        redis_url: チェック対象の Redis URL

    Returns:
        (is_valid, error_message) タプル
        - is_valid: URL 形式が有効な場合は True
        - error_message: エラーメッセージ（エラーがない場合は None）
    """
    if not redis_url:
        return False, "REDIS_URL is empty"

    if not (redis_url.startswith("redis://") or redis_url.startswith("rediss://") or redis_url.startswith("unix://")):
        return False, f"REDIS_URL must start with redis://, rediss://, or unix://. Got: {redis_url[:50]}"

    return True, None

def connect_to_redis(redis_url: str, max_retries: int = 5, initial_backoff: int = 2) -> Optional[Redis]:
    """
    Redis に接続（リトライロジック付き）

    設定エラー（無効な URL）：即座に None を返す
    ネットワークエラー（一時的な接続失敗）：指数バックオフでリトライ

    Args:
        redis_url: Redis の接続 URL（redis://, rediss://, unix:// スキーム）
        max_retries: 最大リトライ回数（デフォルト: 5回）
        initial_backoff: 初期バックオフ時間（秒、デフォルト: 2秒）
                        指数バックオフにより倍々で増加（最大30秒）

    Returns:
        接続に成功した場合は Redis クライアント、失敗した場合は None

    Environment Variables:
        RQ_MAX_RETRIES: 最大リトライ回数（デフォルト: 5）
        RQ_RETRY_BACKOFF: 初期バックオフ時間（デフォルト: 2秒）
    """
    # ステップ1：URL 形式を検証（設定エラーチェック）
    is_valid, error_msg = validate_redis_url_format(redis_url)
    if not is_valid:
        logger.error(f"❌ Invalid REDIS_URL format: {error_msg}")
        logger.error(f"Full URL: {redis_url}")
        return None  # 設定エラーは exit させない（呼び出し側で処理）

    # ステップ2：Redis に接続（ネットワークエラーはリトライ）
    logger.info(f"Connecting to Redis: {redis_url[:50]}...")

    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            redis_conn = redis.from_url(
                redis_url,
                socket_connect_timeout=5,
                socket_timeout=5,
                decode_responses=True
            )
            redis_conn.ping()
            logger.info(f"✅ Redis connection established (attempt {attempt}/{max_retries})")
            return redis_conn
        except redis.exceptions.ConnectionError as e:
            if attempt < max_retries:
                logger.warning(f"⚠️  Connection failed (attempt {attempt}/{max_retries}): {e}")
                logger.warning(f"   Retrying in {backoff} seconds...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)  # Exponential backoff, max 30s
            else:
                logger.error(f"❌ Failed to connect to Redis after {max_retries} attempts: {e}")
                return None
        except Exception as e:
            logger.error(f"❌ Unexpected error connecting to Redis: {e}")
            return None

    return None

def main():
    # Redis URL を環境変数から取得
    redis_url = os.getenv("REDIS_URL") or os.getenv("VALKEY_URL")

    if not redis_url:
        logger.error("❌ REDIS_URL or VALKEY_URL environment variable not set")
        sys.exit(1)

    logger.info(f"DEBUG: Raw REDIS_URL from environment: {redis_url}")

    # 一時的な回避策: RenderのinternalConnectionStringが解決できないホスト名を返す問題への対応
    # このホスト名がデプロイごとに変わる可能性があるため、あくまで一時的なデバッグ手段とする
    if "red-d3mdth6r433s73aj45j0" in redis_url:
        logger.warning("⚠️  Temporary workaround: Replacing unresolvable Redis hostname.")
        redis_url = redis_url.replace("red-d3mdth6r433s73aj45j0", "gemini-chat-redis-discovery")
        logger.info(f"DEBUG: Modified REDIS_URL for connection: {redis_url}")

    # Redis に接続（環境変数で設定可能）
    redis_conn = connect_to_redis(redis_url, max_retries=MAX_RETRIES, initial_backoff=RETRY_BACKOFF)

    if not redis_conn:
        logger.error("❌ Failed to establish Redis connection")
        logger.error(f"Please ensure REDIS_URL is set correctly in Render environment variables.")
        logger.error(f"Expected format: redis://[password@]host:port or rediss://...")
        sys.exit(1)

    # RQ Queue を作成
    queue = Queue("default", connection=redis_conn)

    # ワーカーを起動
    logger.info("Starting RQ worker on queue 'default'...")
    worker = Worker([queue], connection=redis_conn)

    try:
        worker.work(with_scheduler=True)
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Worker failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
