# services/gemini_client.py
import os
import logging
from typing import List, Dict, Any, Tuple, Optional

import google.generativeai as genai
from google.api_core import exceptions as gexc

logger = logging.getLogger(__name__)

# -------------------------------
# モデル名の正規化（廃止/旧名 → 現行名）
# -------------------------------
_MODEL_ALIASES: Dict[str, str] = {
    # 1.5 系や旧名は 2.5 系へ寄せる
    "gemini-1.5-pro-latest": "gemini-2.5-pro",
    "gemini-1.5-flash-latest": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-2.5-pro",
    "gemini-1.5-flash": "gemini-2.5-flash",
    # 2.0/旧称の保険
    "gemini-2.0-pro": "gemini-2.5-pro",
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini-2.0-flash-001": "gemini-2.5-flash",
    "gemini-pro-latest": "gemini-2.5-pro",
    "gemini-flash-latest": "gemini-2.5-flash",
    # 明示の新称
    "models/gemini-1.5-pro-latest": "gemini-2.5-pro",
    "models/gemini-1.5-flash-latest": "gemini-2.5-flash",
}

def _norm(model: str) -> str:
    m = (model or "").strip()
    m = m.replace("models/", "")  # list_models の表記でも使えるように
    if m in _MODEL_ALIASES:
        fixed = _MODEL_ALIASES[m]
        logger.warning(f"[Gemini] Model '{m}' -> '{fixed}' に置換（互換マッピング）")
        return fixed
    return m

class GeminiFallbackError(Exception):
    """全候補モデルが失敗したときの例外"""
    pass

class GeminiClient:
    def __init__(
        self,
        primary_model: str,
        fallback_model: str,
        api_key: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")

        # 2025/10 現在 v1beta 推奨
        self.api_version = api_version or os.getenv("GOOGLE_API_VERSION", "v1beta")

        self.primary_model = _norm(primary_model or "gemini-2.5-pro")
        self.fallback_model = _norm(fallback_model or "gemini-2.5-flash")

        genai.configure(
            api_key=self.api_key,
            client_options={"api_endpoint": f"https://generativelanguage.googleapis.com/{self.api_version}"},
        )

        logger.info(
            f"[Gemini] api_version={self.api_version} primary={self.primary_model} "
            f"fallback={self.fallback_model}"
        )

    # --------------------------------
    # 内部：生成 API 呼び出し（フォーマット吸収）
    # --------------------------------
    def _run_generate(self, model: str, contents: List[Dict[str, str]]) -> str:
        """
        contents は chat 風 [{"role":"user","content":"..."}, ...] を受け取り、
        GenerativeModel が期待する [{"role":..., "parts":[{"text":...}]}] に正規化する。
        """
        try:
            def norm_contents(msgs):
                out = []
                for m in msgs:
                    role = m.get("role") or ("user" if m.get("content") else "model")
                    text = m.get("content") or m.get("text") or ""
                    out.append({"role": role, "parts": [{"text": text}]})
                return out

            data = norm_contents(contents)
            # タイムアウト設定付きで生成
            generation_config = {
                "temperature": 0.7,
                "max_output_tokens": 2048,
            }
            resp = genai.GenerativeModel(model).generate_content(
                data,
                generation_config=generation_config
            )
            return (getattr(resp, "text", None) or "").strip()
        except gexc.GoogleAPICallError:
            # 上位でフォールバック処理するためそのまま投げる
            raise

    def _chat_once(self, model: str, messages: List[Dict[str, str]], user_message: str) -> str:
        payload = []
        for m in messages:
            payload.append({"role": m["role"], "content": m["content"]})
        payload.append({"role": "user", "content": user_message})
        return self._run_generate(model, payload)

    # --------------------------------
    # 公開 API
    # --------------------------------
    def chat(
        self,
        messages: List[Dict[str, str]],
        user_message: str,
        requested_model: str = "",
    ) -> Tuple[str, str]:
        """
        returns (reply_text, used_model)
        """
        candidates = []
        req = _norm(requested_model)
        if req:
            candidates.append(req)
        candidates.extend([self.primary_model, self.fallback_model])

        last_err: Optional[Exception] = None
        for m in candidates:
            try:
                logger.info(f"Trying Gemini model: {m}")
                out = self._chat_once(m, messages, user_message)
                if out:
                    logger.info(f"Success with model: {m}")
                    return out, m
            except gexc.NotFound as e:
                logger.error(f"Gemini error on {m}: NotFound: {e}")
                last_err = e
                continue
            except gexc.DeadlineExceeded as e:
                logger.error(f"Gemini timeout on {m}: {e}")
                last_err = e
                continue
            except gexc.GoogleAPICallError as e:
                logger.error(f"Gemini API error on {m}: {e}")
                last_err = e
                continue
            except Exception as e:
                logger.error(f"Gemini unexpected error on {m}: {e}")
                last_err = e
                continue

        error_msg = str(last_err) if last_err else "all candidates failed"
        if "timeout" in error_msg.lower() or "deadline" in error_msg.lower():
            raise GeminiFallbackError("Gemini APIがタイムアウトしました。しばらく待ってから再試行してください。")
        raise GeminiFallbackError(f"Gemini APIエラー: {error_msg}")

    def analyze_conversation(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        会話の要約（同期版）
        """
        prompt = (
            "以下の会話を短く要約してください。箇条書きでも可。"
            "誤情報や個人情報は含めないでください。\n\n"
        )
        for m in messages:
            role = "User" if m["role"] == "user" else "Assistant"
            prompt += f"{role}: {m['content']}\n"
        prompt += "\n---\n短い要約: "

        text, used = self.chat([], prompt, requested_model=self.primary_model)
        return {"summary": text, "model": used}

    def summarize_with_citations(
        self,
        query: str,
        search_results: List[Dict[str, str]],
        requested_model: str = "",
    ) -> Dict[str, Any]:
        """
        検索結果を踏まえた要約（enriched_content対応）
        """
        lines = [f"ユーザーの要望: {query}", "\n参考資料:"]
        for i, r in enumerate(search_results, 1):
            title = r.get("title", "")
            url = r.get("url", "") or r.get("link", "") or r.get("info_link", "")
            snip = r.get("snippet", "")
            enriched = r.get("enriched_content", "")

            # enriched_contentがあればそれを優先、なければsnippet
            if enriched:
                lines.append(f"[{i}] {title}\nURL: {url}\n詳細情報: {enriched}\n")
            else:
                lines.append(f"[{i}] {title}\nURL: {url}\n要旨: {snip}\n")

        lines.append(
            "\n指示: 上記の信頼できる情報を最大限活用して、簡潔に日本語で要約してください。"
            "直接的な情報がない場合でも、関連情報から合理的に推測できる内容は含めてください。"
            "最後に参考URLを列挙してください。"
        )
        prompt = "\n".join(lines)

        text, used = self.chat([], prompt, requested_model=requested_model or self.primary_model)
        return {"answer": text, "model": used}

    def _enrich_search_results_with_webfetch(
        self,
        search_results: List[Dict[str, str]],
        max_fetch: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        検索結果の上位を WebFetch で取得してコンテンツを強化

        Args:
            search_results: 検索結果リスト
            max_fetch: WebFetch を実行する最大件数（デフォルト2件）

        Returns:
            enriched_content フィールドを追加した検索結果
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import requests

        enriched_results = []

        # WebFetch を実行する対象を選択（信頼できるドメイン優先）
        try:
            from app.constants import TRUSTED_BOOK_SOURCES_DOMAINS, USE_TRUSTED_DOMAINS
            trusted_domains = TRUSTED_BOOK_SOURCES_DOMAINS if USE_TRUSTED_DOMAINS else []
        except ImportError:
            trusted_domains = ["amazon.co.jp", "hanmoto.com", "books.rakuten.co.jp"]

        # 信頼できるドメインの結果を優先的に選択
        fetch_targets = []
        for r in search_results:
            url = r.get("url", "")
            if trusted_domains and any(domain in url for domain in trusted_domains):
                fetch_targets.append(r)
                if len(fetch_targets) >= max_fetch:
                    break

        # 信頼できるドメインが不足している場合は他の結果も追加
        if len(fetch_targets) < max_fetch:
            for r in search_results:
                if r not in fetch_targets:
                    fetch_targets.append(r)
                    if len(fetch_targets) >= max_fetch:
                        break

        def fetch_content(result: Dict[str, str]) -> Dict[str, Any]:
            """単一URLのコンテンツを取得"""
            url = result.get("url", "")
            try:
                # 簡易的なHTML取得（タイムアウト10秒）
                response = requests.get(url, timeout=10, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; BookSummaryBot/1.0)"
                })
                if response.status_code == 200:
                    # HTMLからテキストを抽出（簡易版）
                    html = response.text
                    # <p>, <div>, <span> タグ内のテキストを抽出
                    import re
                    # scriptとstyleタグを除去
                    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
                    # HTMLタグを除去
                    text = re.sub(r'<[^>]+>', ' ', html)
                    # 連続する空白を1つに
                    text = re.sub(r'\s+', ' ', text).strip()
                    # 最大2000文字に制限
                    enriched_text = text[:2000] if len(text) > 2000 else text

                    result_copy = result.copy()
                    result_copy["enriched_content"] = enriched_text
                    logger.info(f"WebFetch success for {url[:50]}... (fetched {len(enriched_text)} chars)")
                    return result_copy
                else:
                    logger.warning(f"WebFetch failed for {url}: HTTP {response.status_code}")
                    return result
            except Exception as e:
                logger.warning(f"WebFetch failed for {url}: {e}")
                return result

        # 並列でWebFetch実行
        with ThreadPoolExecutor(max_workers=min(2, len(fetch_targets))) as executor:
            futures = {executor.submit(fetch_content, r): r for r in fetch_targets}
            for future in as_completed(futures):
                try:
                    enriched_result = future.result()
                    enriched_results.append(enriched_result)
                except Exception as e:
                    logger.error(f"WebFetch thread error: {e}")
                    # エラー時は元の結果をそのまま追加
                    original = futures[future]
                    enriched_results.append(original)

        # WebFetch対象外の結果も追加
        for r in search_results:
            if r not in fetch_targets:
                enriched_results.append(r)

        return enriched_results

    def summarize_book_with_toc(
        self,
        book_title: str,
        table_of_contents: str,
        search_results: List[Dict[str, str]],
        requested_model: str = "",
        use_webfetch: bool = True,
    ) -> Dict[str, Any]:
        """
        書籍専用要約：目次構造を尊重し、信頼できる出版社情報や書評を優先

        Args:
            book_title: 書籍タイトル
            table_of_contents: ユーザーが提供した目次（章立て）
            search_results: 検索結果（出版社サイト、書評サイトなど）
            requested_model: 使用するモデル名
            use_webfetch: WebFetchでコンテンツを取得するか（デフォルトTrue）

        Returns:
            要約結果を含む辞書
        """
        # WebFetchでコンテンツを強化
        if use_webfetch and search_results:
            logger.info(f"[Book Summary] Enriching search results with WebFetch (top 2 results)")
            enriched_results = self._enrich_search_results_with_webfetch(search_results, max_fetch=2)
        else:
            enriched_results = search_results

        lines = [
            f"書籍タイトル: {book_title}",
            "",
            "ユーザーが提供した目次:",
            table_of_contents,
            "",
            "参考資料（出版社サイト、書評サイト、大手書店の情報）:",
        ]

        # 信頼できるソースを優先的にリスト化
        # app/constants.pyから信頼できるドメインリストを読み込む
        try:
            from app.constants import TRUSTED_BOOK_SOURCES_DOMAINS, USE_TRUSTED_DOMAINS
            trusted_domains = TRUSTED_BOOK_SOURCES_DOMAINS if USE_TRUSTED_DOMAINS else []
        except ImportError:
            # フォールバック: constants.pyが存在しない場合
            logger.warning("app.constants not found, using hardcoded trusted domains")
            trusted_domains = [
                "amazon.co.jp", "hanmoto.com", "books.rakuten.co.jp",
                "bookmeter.com", "booklog.jp", "honz.jp"
            ]

        trusted_sources = []
        other_sources = []

        for i, r in enumerate(enriched_results, 1):
            title = r.get("title", "")
            url = r.get("url", "") or r.get("link", "") or r.get("info_link", "")
            snip = r.get("snippet", "") or r.get("description", "")
            enriched_content = r.get("enriched_content", "")

            # WebFetchで取得したコンテンツがある場合はそれを使用
            if enriched_content:
                entry = f"[{i}] {title}\nURL: {url}\n詳細情報（実際のWebページコンテンツ）: {enriched_content}\n"
            elif snip:
                entry = f"[{i}] {title}\nURL: {url}\n要旨: {snip}\n"
            else:
                entry = f"[{i}] {title}\nURL: {url}\n（詳細情報なし）\n"

            # 信頼できるドメインを優先
            if trusted_domains and any(domain in url for domain in trusted_domains):
                trusted_sources.append(entry)
            else:
                other_sources.append(entry)

        # 信頼できるソースを先に配置
        for entry in trusted_sources:
            lines.append(entry)
        for entry in other_sources:
            lines.append(entry)

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 重要な指示")
        lines.append("")
        lines.append("### 情報源の活用方針")
        lines.append("1. **「詳細情報（実際のWebページコンテンツ）」を最優先で参照**: これは実際のWebページから取得した豊富なテキストです。書評、目次、内容紹介などが含まれている可能性が高いです。")
        lines.append("2. **信頼できるドメインを優先**: Amazon、楽天ブックス、版元ドットコム、読書メーターなどの大手書店・書評サイトの情報を重視してください。")
        lines.append("3. **複数の情報源を統合**: 断片的な情報でも、複数のソースを組み合わせることで全体像を把握してください。")
        lines.append("")
        lines.append("### 要約作成の戦略")
        lines.append("4. **書籍全体のテーマを理解**: まず参考資料から書籍の主題、対象読者、著者の意図を把握してください。")
        lines.append("5. **目次構造に沿った要約**: ユーザーが提供した目次の各章について、以下の優先順位で要約を作成:")
        lines.append("   a. **直接的な情報**: 参考資料に明確に記載されている章の内容をそのまま使用")
        lines.append("   b. **文脈からの推論**: 章タイトル + 書籍全体のテーマ + 著者の専門分野から、その章で扱われている内容を合理的に推論")
        lines.append("   c. **一般知識の活用**: 書籍のテーマ（例：論語）に関する一般的な知識と章タイトルを組み合わせて、教育的な要約を提供")
        lines.append("")
        lines.append("### 情報不足時の対応（重要）")
        lines.append("6. **「情報不足」は最後の手段**: 以下の順序で対応してください:")
        lines.append("   - まず、Webページコンテンツに含まれる断片的な情報を探す")
        lines.append("   - 次に、章タイトルから推測される内容を説明（例: 「この章では、[章タイトル]について論語の教えを実践的に解説していると推測されます」）")
        lines.append("   - 著者の専門分野や書籍のテーマから、章の目的を推測（例: 「書籍全体のテーマから、この章では[推測される内容]を扱っていると考えられます」）")
        lines.append("   - どうしても情報がない場合のみ「情報不足により詳細は不明」と記載")
        lines.append("")
        lines.append("### その他の注意事項")
        lines.append("7. **無関係な資料の除外**: 学術論文、大学パンフレット、明らかに無関係なレビューは無視してください。")
        lines.append("8. **有用性を最優先**: ユーザーに価値のある情報を提供することを最優先としてください。確実な情報が少なくても、合理的な推測を含めた有用な要約を作成してください。")
        lines.append("9. **参考情報源の明記**: 最後に「## 参考情報源」セクションで、実際に参照したURLを列挙してください。")
        lines.append("")
        lines.append("出力形式:")
        lines.append("```")
        lines.append("## 書籍名")
        lines.append("")
        lines.append("### 序章/第一章: [章タイトル]")
        lines.append("[この章の要約]")
        lines.append("")
        lines.append("### 第二章: [章タイトル]")
        lines.append("[この章の要約]")
        lines.append("")
        lines.append("...")
        lines.append("")
        lines.append("## 参考情報源")
        lines.append("- [URL1]")
        lines.append("- [URL2]")
        lines.append("```")

        prompt = "\n".join(lines)

        text, used = self.chat([], prompt, requested_model=requested_model or self.primary_model)
        return {"answer": text, "model": used}


