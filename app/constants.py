# app/constants.py
"""
アプリケーション全体で共有される定数
"""

# 書籍情報の信頼できるドメインリスト
TRUSTED_BOOK_SOURCES_DOMAINS = [
    "amazon.co.jp",
    "hanmoto.com",
    "books.rakuten.co.jp",
    "bookmeter.com",
    "booklog.jp",
    "honz.jp",
]

# 検索用のsite:プレフィックス付きリスト
TRUSTED_BOOK_SOURCES_SITES = [f"site:{domain}" for domain in TRUSTED_BOOK_SOURCES_DOMAINS]
