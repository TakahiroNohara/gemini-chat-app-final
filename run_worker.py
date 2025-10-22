#!/usr/bin/env python
"""
RQ Worker スタートアップスクリプト
Render での環境変数展開問題を回避するため、
Python から直接 RQ ワーカーを起動する
"""
import os
import sys
import logging
from rq import Worker, Queue
import redis

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    # Redis URL を環境変数から取得
    redis_url = os.getenv("REDIS_URL") or os.getenv("VALKEY_URL")

    if not redis_url:
        logger.error("REDIS_URL or VALKEY_URL environment variable not set")
        sys.exit(1)

    logger.info(f"Connecting to Redis: {redis_url[:50]}...")

    try:
        # Redis 接続
        redis_conn = redis.from_url(
            redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
            decode_responses=True
        )
        redis_conn.ping()
        logger.info("✅ Redis connection established")
    except Exception as e:
        logger.error(f"❌ Failed to connect to Redis: {e}")
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
