import os
import time
import requests
from typing import List, Dict, Any, Optional


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
    def __init__(self, provider: str, env: Optional[dict] = None, timeout: int = 15, retries: int = 2):
        self.provider = (provider or "").lower().strip() or "google_cse"
        self.env = env or os.environ
        self.timeout = max(5, int(timeout))
        self.retries = max(0, int(retries))

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
        elif self.provider == "brave":
            raise SearchError("brave provider not implemented")
        elif self.provider == "bing":
            raise SearchError("bing provider not implemented")
        raise SearchError(f"unknown provider: {self.provider}")

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

