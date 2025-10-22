# services/gemini_client_http.py
import os
import logging
import requests
from typing import List, Dict, Any, Tuple, Optional

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

        self.base_url = f"https://generativelanguage.googleapis.com/{self.api_version}"

        logger.info(
            f"[Gemini HTTP] api_version={self.api_version} primary={self.primary_model} "
            f"fallback={self.fallback_model}"
        )

    # --------------------------------
    # 内部：生成 API 呼び出し（HTTP版）
    # --------------------------------
    def _run_generate(self, model: str, contents: List[Dict[str, str]]) -> str:
        """
        contents は chat 風 [{"role":"user","content":"..."}, ...] を受け取り、
        Gemini API が期待する [{"role":..., "parts":[{"text":...}]}] に正規化する。
        """
        try:
            # メッセージを正規化
            normalized_contents = []
            for m in contents:
                role = m.get("role") or ("user" if m.get("content") else "model")
                text = m.get("content") or m.get("text") or ""
                normalized_contents.append({"role": role, "parts": [{"text": text}]})

            # リクエストボディ
            payload = {
                "contents": normalized_contents,
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 4096,  # より長い要約に対応
                }
            }

            # エンドポイント
            url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"

            # HTTPリクエスト送信
            headers = {"Content-Type": "application/json"}
            response = requests.post(url, json=payload, headers=headers, timeout=120)  # 2分 - 長いコンテンツ要約に対応

            # エラーハンドリング
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Gemini API error: {error_msg}")
                if response.status_code == 404:
                    raise GeminiFallbackError(f"Model not found: {model}")
                elif response.status_code == 429:
                    raise GeminiFallbackError("Rate limit exceeded")
                else:
                    raise GeminiFallbackError(error_msg)

            # レスポンスパース
            result = response.json()
            candidates = result.get("candidates", [])
            if not candidates:
                logger.warning(f"No candidates in response for model {model}")
                return ""

            # テキスト取得
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            if not parts:
                logger.warning(f"No parts in response for model {model}")
                return ""

            text = parts[0].get("text", "").strip()
            return text

        except requests.exceptions.Timeout:
            logger.error(f"Request timeout for model {model}")
            raise GeminiFallbackError(f"Request timeout for model {model}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error for model {model}: {e}")
            raise GeminiFallbackError(f"Request error: {str(e)}")

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
            except GeminiFallbackError as e:
                logger.error(f"Gemini error on {m}: {e}")
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
        # プロンプトをより詳細化
        prompt = (
            "## 指示\n"
            "以下の会話履歴に基づき、この会話の主題を要約してください。\n\n"
            "## 制約条件\n"
            "- 要約は、会話の主題がすぐに分かるように、20文字程度の簡潔な日本語のテキストにしてください。\n"
            "- この要約は、チャットアプリのサイドバーに表示されるタイトルとして使用されます。\n"
            "- 出力には、要約のテキストのみを含めてください。接頭辞（例: 「要約:」）やMarkdownは不要です。\n"
            "- 会話の冒頭部分を参考に、主要なトピックを抽出してください。\n\n"
            "## 会話履歴\n"
        )
        
        # 会話履歴をプロンプトに追加
        history_text = []
        for m in messages:
            role = "User" if m["role"] == "user" else "Assistant"
            history_text.append(f"{role}: {m['content']}")
        
        prompt += "\n".join(history_text)
        prompt += "\n\n## 要約"

        # self.chatを使用して要約を生成
        text, used = self.chat([], prompt, requested_model=self.primary_model)
        
        # 不要な部分を削除
        summary_text = text.strip().replace("要約:", "").replace("タイトル:", "").strip()

        return {"summary": summary_text, "model": used}

    def summarize_with_citations(
        self,
        query: str,
        search_results: List[Dict[str, str]],
        requested_model: str = "",
    ) -> Dict[str, Any]:
        """
        検索結果を踏まえた要約
        """
        lines = [f"ユーザーの要望: {query}", "\n参考資料:"]
        for i, r in enumerate(search_results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            snip = r.get("snippet", "")
            lines.append(f"[{i}] {title}\nURL: {url}\n内容: {snip}\n")

        lines.append(
            "\n**指示:**\n"
            "上記の参考資料を徹底的に分析し、ユーザーの要望に答えてください。\n\n"
            "**分析手順:**\n"
            "1. すべての参考資料を注意深く読み、関連する情報を抽出する\n"
            "2. 複数の情報源で一致する内容を重視し、信頼性を確保する\n"
            "3. 情報源の信頼性を評価（公式サイト、書評サイト、専門家の意見などを優先）\n"
            "4. 矛盾する情報がある場合は、より詳細で信頼できる情報源を採用する\n"
            "5. 検索結果に含まれる情報から、ユーザーの要望に最も関連する内容を抽出する\n"
            "6. 情報を統合し、包括的で分かりやすい回答を作成する\n\n"
            "**回答の形式:**\n"
            "- できる限り具体的に説明してください\n"
            "- 箇条書きや段落を適切に使い、読みやすく構成してください\n"
            "- 検索結果から得られた情報を最大限活用してください\n"
            "- 最後に、参考にした主要な情報源のURLを列挙してください"
        )
        prompt = "\n".join(lines)

        text, used = self.chat([], prompt, requested_model=requested_model or self.primary_model)
        return {"answer": text, "model": used}

    def summarize_with_citations_enriched(
        self,
        query: str,
        search_results: List[Dict[str, str]],
        requested_model: str = "",
    ) -> Dict[str, Any]:
        """
        検索結果を踏まえた要約（enriched_content 対応版）
        - enriched_content があればスニペットより優先
        - 「情報不足」の早期宣言を避け、合理的な推測も含めてまとめる
        """
        lines: List[str] = [f"ユーザーの要望: {query}", ""]

        # ユーザー提供資料を優先表示
        user_entries: List[str] = []
        other_entries: List[str] = []
        for i, r in enumerate(search_results, 1):
            title = r.get("title", "")
            url = r.get("url") or r.get("link") or r.get("info_link") or ""
            snip = r.get("snippet", "")
            enriched = r.get("enriched_content", "")
            is_user = (r.get("source") == "user") or (url.startswith("user:"))

            if enriched:
                entry = f"[{i}] {title}\nURL: {url}\n詳細情報: {enriched}\n"
            elif snip:
                entry = f"[{i}] {title}\nURL: {url}\n要旨: {snip}\n"
            else:
                entry = f"[{i}] {title}\nURL: {url}\n（要約情報なし）\n"

            if is_user:
                user_entries.append(entry)
            else:
                other_entries.append(entry)

        if user_entries:
            lines.append("ユーザー提供資料（優先）:")
            lines.extend(user_entries)
            lines.append("")

        lines.append("参照資料:")
        lines.extend(other_entries)

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("指示:")
        lines.append("- 上記の資料を総合して、日本語で簡潔かつ具体的に要約してください。")
        lines.append("- 直接的な記述が不足している箇所は、資料全体の傾向や整合から合理的に推測し、空白を埋めてください。")
        lines.append("- 断片的な情報しかない場合でも、有用性を重視して最も妥当な全体像を提示してください。")
        lines.append("- 最後に参考URLを列挙してください。")

        prompt = "\n".join(lines)

        text, used = self.chat([], prompt, requested_model=requested_model or self.primary_model)
        return {"answer": text, "model": used}

    def summarize_book_with_toc(
        self,
        book_title: str,
        table_of_contents: str,
        search_results: List[Dict[str, str]],
        requested_model: str = "",
        use_webfetch: bool = True,
    ) -> Dict[str, Any]:
        """
        書籍専用要約。目次構造を尊重し、信頼できる出版社・書評サイトを優先。
        """
        try:
            from app.constants import TRUSTED_BOOK_SOURCES_DOMAINS, USE_TRUSTED_DOMAINS
            trusted_domains = TRUSTED_BOOK_SOURCES_DOMAINS if USE_TRUSTED_DOMAINS else []
        except Exception:
            trusted_domains = [
                "amazon.co.jp",
                "books.rakuten.co.jp",
                "hanmoto.com",
                "bookmeter.com",
                "booklog.jp",
                "honz.jp",
            ]

        lines: List[str] = []
        lines.append(f"対象書籍: {book_title}")
        lines.append("")
        lines.append("提供された目次:")
        lines.append(table_of_contents.strip() or "（目次情報なし）")
        lines.append("")
        # ユーザー提供の章ヒント、資料を優先提示
        user_hint_entries: List[str] = []
        user_entries: List[str] = []
        other_entries: List[str] = []

        for i, r in enumerate(search_results, 1):
            title = r.get("title", "")
            url = r.get("url") or r.get("link") or r.get("info_link") or ""
            snip = r.get("snippet", "")
            enriched = r.get("enriched_content", "")
            is_user = (r.get("source") == "user") or (url.startswith("user:"))
            chapter_hint = (r.get("chapter") or "").strip()

            if enriched:
                entry = f"[{i}] {title}\nURL: {url}\n詳細情報: {enriched}\n"
            elif snip:
                entry = f"[{i}] {title}\nURL: {url}\n要旨: {snip}\n"
            else:
                entry = f"[{i}] {title}\nURL: {url}\n（詳細情報なし）\n"

            if is_user and chapter_hint:
                user_hint_entries.append(f"- 章: {chapter_hint}\n  ヒント: {enriched or snip}")
                user_entries.append(entry)
            elif is_user:
                user_entries.append(entry)
            else:
                # 信頼ドメイン優先の並べ替え用に分類
                if any(d in (url or "") for d in trusted_domains):
                    other_entries.append("__TRUSTED__\n" + entry)
                else:
                    other_entries.append(entry)

        if user_hint_entries:
            lines.append("ユーザー提供：章ヒント（優先）")
            lines.extend(user_hint_entries)
            lines.append("")

        if user_entries:
            lines.append("ユーザー提供資料（優先）:")
            lines.extend(user_entries)
            lines.append("")

        lines.append("参照資料:")

        # 信頼ドメイン（__TRUSTED__マーク）を優先表示
        for e in other_entries:
            if e.startswith("__TRUSTED__\n"):
                lines.append(e.replace("__TRUSTED__\n", ""))
        for e in other_entries:
            if not e.startswith("__TRUSTED__\n"):
                lines.append(e)

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("重要な指示:")
        lines.append("1. 参照資料から書籍の主題・対象読者・著者の意図を把握してください。")
        lines.append("2. 目次の章立てに沿って、各章の要点を日本語でまとめてください。")
        lines.append("3. 直接的な記述がない章は、章タイトル・書籍の全体テーマ・著者の傾向から合理的に推測して要約してください。")
        lines.append("4. 有用性を重視し、可能な範囲で具体例やキーワードも含めてください。")
        lines.append("5. 参照先の信頼性を意識し、公式・大手書店・書評サイトの情報を優先してください。")
        lines.append("6. 最後に参考URLを列挙してください。")

        lines.append("")
        lines.append("出力フォーマット例:")
        lines.append("```")
        lines.append("## 書籍名")
        lines.append("")
        lines.append("### 序章/第1章: [章タイトル]")
        lines.append("[この章の要約]")
        lines.append("")
        lines.append("### 第2章: [章タイトル]")
        lines.append("[この章の要約]")
        lines.append("")
        lines.append("...")
        lines.append("")
        lines.append("## 参考URL")
        lines.append("- [URL1]")
        lines.append("- [URL2]")
        lines.append("```")

        prompt = "\n".join(lines)
        text, used = self.chat([], prompt, requested_model=requested_model or self.primary_model)
        return {"answer": text, "model": used}
