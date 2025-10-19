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
