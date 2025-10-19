import os
import time
import logging
import requests
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.google_books_client import GoogleBooksClient
from services.ndl_client import NDLClient

logger = logging.getLogger(__name__)


class SearchError(Exception):
    pass


def _normalize(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    norm = []
    for it in items:
        title = it.get("title") or it.get("name") or it.get("url")
        url = it.get("link") or it.get("url")
        snippet = it.get("snippet") or it.get("description") or ""
        if title and url:
            norm.append({"title": title, "url": url, "snippet": snippet})
    return norm


class SearchClient:
    """
    検索クライアント（堅牢化＋鮮度対応）
    - タイムアウト、指数バックオフの簡易リトライ
    - recency_days を Google CSE の dateRestrict に反映（例: d1 = 24時間以内）
    - gl（国ターゲット）/ lr（言語）指定
    """
    def __init__(self, provider: str, env: Optional[dict] = None, timeout: int = 30, retries: int = 2):
        self.provider = (provider or "").lower().strip() or "google_cse"
        self.env = env or os.environ
        self.timeout = max(5, int(timeout))
        self.retries = max(0, int(retries))
        
        if self.provider == "google_books":
            self.google_books_client = GoogleBooksClient(api_key=self.env.get("GOOGLE_API_KEY"))
        elif self.provider == "ndl":
            self.ndl_client = NDLClient()

    # ---------------- Public ----------------
    def search(
        self,
        query: str,
        top_k: int = 5,
        recency_days: Optional[int] = None,
        gl: Optional[str] = None,
        lr: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not (query or "").strip():
            raise SearchError("query is required")

        if self.provider == "google_cse":
            return self._google_cse(query, top_k, recency_days, gl, lr, extra_params or {})
        elif self.provider == "serpapi":
            return self._serpapi(query, top_k, recency_days, gl, lr)
        elif self.provider == "google_books":
            return self._google_books(query, top_k)
        elif self.provider == "ndl":
            return self._ndl(query, top_k)
        elif self.provider == "brave":
            raise SearchError("brave provider not implemented")
        elif self.provider == "bing":
            raise SearchError("bing provider not implemented")
        raise SearchError(f"unknown provider: {self.provider}")

    def search_book(
        self,
        book_title: str,
        author: Optional[str] = None,
        top_k: int = 10,
        layer1_threshold: int = 5,
        layer2_threshold: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        書籍専用検索：3層の段階的検索戦略で信頼性と網羅性を両立

        Layer 1: 信頼できるサイトを個別に検索（高信頼性）
        Layer 2: サイト制限なしで著者+タイトル+品質キーワード（網羅性向上）
        Layer 3: 著者名フォールバック（最終手段）

        Args:
            book_title: 書籍タイトル
            author: 著者名（任意）
            top_k: 取得する検索結果数
            layer1_threshold: Layer 2に移行する最小結果数
            layer2_threshold: Layer 3に移行する最小結果数

        Returns:
            正規化された検索結果のリスト
        """
        if not (book_title or "").strip():
            raise SearchError("book_title is required")

        # 信頼できるドメインリストを取得
        try:
            from app.constants import TRUSTED_BOOK_SOURCES_DOMAINS
            trusted_domains = TRUSTED_BOOK_SOURCES_DOMAINS
        except ImportError:
            logger.warning("app.constants not found, using hardcoded trusted domains")
            trusted_domains = [
                "amazon.co.jp",
                "hanmoto.com",
                "books.rakuten.co.jp",
                "bookmeter.com",
                "booklog.jp",
                "honz.jp",
            ]

        all_results = []
        seen_urls = set()

        # ヘルパー関数: 重複を避けて結果を追加
        def _add_unique(results: List[Dict[str, Any]]) -> int:
            added = 0
            for r in results:
                url = r.get("info_link") or r.get("link")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)
                    added += 1
            return added

        # --- Layer 1: 信頼できるサイトを個別に検索 ---
        logger.info(f"[Book Search Layer 1] Searching trusted sites for: {book_title}")

        # 各サイトを並列検索（OR構文を使わず個別に）
        layer1_queries = []
        for domain in trusted_domains[:3]:  # 上位3サイト（Amazon, 版元, 楽天）
            query = f"{book_title}"
            if author:
                query += f" {author}"
            layer1_queries.append((query, domain))

        with ThreadPoolExecutor(max_workers=len(layer1_queries)) as executor:
            futures = {
                executor.submit(
                    self.search,
                    query=q,
                    top_k=3,
                    recency_days=None,
                    gl="jp",
                    lr="lang_ja",
                    extra_params={"siteSearch": domain},
                ): (q, domain) for q, domain in layer1_queries
            }

            for future in as_completed(futures):
                q, domain = futures[future]
                try:
                    results = future.result()
                    added = _add_unique(results)
                    logger.info(f"[Layer 1] {domain}: {len(results)} results ({added} new)")
                except Exception as e:
                    logger.warning(f"[Layer 1] {domain} failed: {e}")

        logger.info(f"[Layer 1] Total unique results: {len(all_results)}")

        # Layer 1で十分な結果が得られた場合は終了
        if len(all_results) >= layer1_threshold:
            return all_results[:top_k]

        # --- Layer 2: サイト制限なしで品質キーワード検索 ---
        logger.info(f"[Book Search Layer 2] Broadening search (Layer 1 had {len(all_results)} results)")

        layer2_query = f"{book_title}"
        if author:
            layer2_query += f" {author}"
        layer2_query += " (書評 OR レビュー OR 要約 OR 内容紹介)"

        try:
            layer2_results = self.search(
                query=layer2_query,
                top_k=10,
                recency_days=None,
                gl="jp",
                lr="lang_ja",
            )
            added = _add_unique(layer2_results)
            logger.info(f"[Layer 2] Added {added} new results (from {len(layer2_results)} total)")
        except Exception as e:
            logger.warning(f"[Layer 2] Broad search failed: {e}")

        logger.info(f"[Layer 2] Total unique results: {len(all_results)}")

        # Layer 2で十分な結果が得られた場合は終了
        if len(all_results) >= layer2_threshold:
            return all_results[:top_k]

        # --- Layer 3: 著者名フォールバック ---
        if author:
            logger.info(f"[Book Search Layer 3] Author fallback: {author}")
            layer3_query = f"{author} 著書 書籍"

            try:
                layer3_results = self.search(
                    query=layer3_query,
                    top_k=10,
                    recency_days=None,
                    gl="jp",
                    lr="lang_ja",
                )
                added = _add_unique(layer3_results)
                logger.info(f"[Layer 3] Added {added} new results (from {len(layer3_results)} total)")
            except Exception as e:
                logger.warning(f"[Layer 3] Author fallback failed: {e}")

        logger.info(f"[Final] Total unique results: {len(all_results)}")
        return all_results[:top_k]

    # ---------------- Providers ----------------
    def _google_cse(
        self, query: str, top_k: int, recency_days: Optional[int], gl: Optional[str], lr: Optional[str],
        extra_params: Dict[str, Any]
    ):
        api_key = self.env.get("GOOGLE_API_KEY")
        cx = self.env.get("GOOGLE_CSE_ID")
        if not api_key or not cx:
            raise SearchError("GOOGLE_API_KEY/GOOGLE_CSE_ID missing")

        params = {
            "key": api_key,
            "cx": cx,
            "q": query,
            "num": min(10, max(1, int(top_k or 5))),
            # "sort": "date",  # ※ CSEの設定によっては無視されることがあります
        }

        # ✅ 鮮度フィルタ（dateRestrict）
        #   dN = N日以内, wN = N週間以内, hN = N時間以内
        if recency_days is not None:
            d = max(1, int(recency_days))
            params["dateRestrict"] = f"d{d}"

        # ✅ ジオ/言語
        if gl:
            params["gl"] = gl.lower()  # 例: "jp"
        if lr:
            params["lr"] = lr  # 例: "lang_ja"

        # 任意の追加パラメータ（site:指定など）
        for k, v in (extra_params or {}).items():
            params[k] = v

        url = "https://www.googleapis.com/customsearch/v1"
        data = self._http_get_json(url, params)
        items = (data or {}).get("items", [])[:top_k]
        return _normalize(items)

    def _serpapi(self, query: str, top_k: int, recency_days: Optional[int], gl: Optional[str], lr: Optional[str]):
        key = self.env.get("SERPAPI_API_KEY")
        if not key:
            raise SearchError("SERPAPI_API_KEY missing")
        # Google の qdr を使って期間絞り込み（d = day, h = hour, w = week, m = month）
        tbs = None
        if recency_days is not None:
            d = max(1, int(recency_days))
            if d <= 1:
                tbs = "qdr:d"
            elif d <= 7:
                tbs = "qdr:w"
            else:
                tbs = "qdr:m"

        params = {
            "engine": "google",
            "q": query,
            "num": min(10, max(1, int(top_k or 5))),
            "api_key": key,
        }
        if tbs:
            params["tbs"] = tbs
        if gl:
            params["gl"] = gl.lower()
        if lr:
            # SerpAPI は lr=lang_ja ではなく hl=ja を使うことが多い
            params["hl"] = lr.replace("lang_", "")

        url = "https://serpapi.com/search.json"
        data = self._http_get_json(url, params)
        organic = (data or {}).get("organic_results", [])[:top_k]
        items = [{"title": o.get("title"), "url": o.get("link"), "snippet": o.get("snippet","")} for o in organic]
        return _normalize(items)

    def _google_books(self, query: str, top_k: int):
        results = self.google_books_client.search_books(query, max_results=top_k)
        return results

    def _ndl(self, query: str, top_k: int):
        results = self.ndl_client.search_books(query, max_records=top_k)
        return results

    def search_book_v2(
        self,
        book_title: str,
        author: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        if not (book_title or "").strip():
            raise SearchError("book_title is required")

        all_results = []
        seen_urls = set()

        def _add_unique(results: List[Dict[str, Any]]) -> int:
            added = 0
            for r in results:
                url = r.get("info_link") or r.get("link")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)
                    added += 1
            return added

        # --- Layer 1: Google Books API ---
        logger.info(f"[Book Search V2 Layer 1] Searching Google Books for: {book_title}")
        query = f"{book_title}"
        if author:
            query += f" {author}"
        try:
            google_books_results = self.google_books_client.search_books(query, max_results=top_k)
            added = _add_unique(google_books_results)
            logger.info(f"[Layer 1] Google Books: {len(google_books_results)} results ({added} new)")
        except Exception as e:
            logger.warning(f"[Layer 1] Google Books failed: {e}")

        # --- Layer 2: NDL API ---
        logger.info(f"[Book Search V2 Layer 2] Searching NDL for: {book_title}")
        try:
            ndl_results = self.ndl_client.search_books(book_title, max_records=top_k)
            added = _add_unique(ndl_results)
            logger.info(f"[Layer 2] NDL: {len(ndl_results)} results ({added} new)")
        except Exception as e:
            logger.warning(f"[Layer 2] NDL failed: {e}")

        return all_results[:top_k]

    # ---------------- HTTP helper ----------------
    def _http_get_json(self, url: str, params: dict) -> dict:
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                r = requests.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    raise SearchError("rate limited by provider (429)")
                if 500 <= r.status_code < 600:
                    raise SearchError(f"provider 5xx: {r.status_code}")
                if r.status_code != 200:
                    raise SearchError(f"http {r.status_code}: {r.text[:200]}")
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt < self.retries:
                    time.sleep(0.6 * (2 ** attempt))  # 0.6s, 1.2s, ...
                    continue
                raise SearchError(f"network error: {e}") from e
            except SearchError as e:
                last_exc = e
                if "5xx" in str(e) and attempt < self.retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                raise
            except Exception as e:
                last_exc = e
                raise SearchError(f"unexpected error: {e}") from e
        if last_exc:
            raise SearchError(str(last_exc))
        return {}

