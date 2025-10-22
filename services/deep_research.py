# services/deep_research.py
import os
import logging
import json
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# モックモードの判定
USE_MOCK = os.getenv("USE_MOCK_GEMINI", "false").lower() == "true"
if USE_MOCK:
    from services.gemini_client_mock import GeminiClient
else:
    from services.gemini_client_http import GeminiClient

from services.search import SearchClient


class DeepResearchEngine:
    """
    Deep Research機能のコアエンジン
    - クエリ分解（Gemini）
    - 並列検索（ThreadPoolExecutor）
    - 統合レポート生成（Gemini）
    """

    def __init__(self):
        """
        RQワーカープロセス内で初期化
        """
        # Gemini client初期化
        self.gemini_client = GeminiClient(
            primary_model=os.getenv("DEEP_RESEARCH_GEMINI_MODEL", "gemini-2.5-pro"),
            fallback_model=os.getenv("FALLBACK_GEMINI_MODEL", "gemini-2.5-flash"),
            api_key=os.getenv("GEMINI_API_KEY"),
        )

        # Search client初期化
        self.search_client = SearchClient(
            provider=os.getenv("SEARCH_PROVIDER", "google_cse"),
            env=os.environ
        )

        # クエリ分解用モデル（高速化のためFlash使用）
        self.decomposition_model = os.getenv("DECOMPOSITION_GEMINI_MODEL", "gemini-2.5-flash")

        logger.info("[DeepResearch] Engine initialized successfully")

    def execute(self, query: str, job=None) -> Dict[str, Any]:
        """
        Deep Research実行のメインフロー

        Args:
            query: ユーザーのクエリ
            job: RQジョブオブジェクト（進捗更新用、オプショナル）

        Returns:
            {
                "report": "Markdownレポート",
                "sub_queries": ["サブクエリ1", "サブクエリ2", ...],
                "sources_count": 15,
                "citations": [{"title": "...", "url": "..."}]
            }
        """
        logger.info(f"[DeepResearch] Starting research for query: '{query}'")

        # フェーズ1: クエリ分解
        if job:
            job.meta['status'] = 'クエリを分解中...'
            job.meta['phase'] = 'decomposition'
            job.save_meta()

        try:
            sub_queries = self._decompose_query(query)
            logger.info(f"[DeepResearch] Decomposed into {len(sub_queries)} sub-queries: {sub_queries}")
            if job:
                job.meta['sub_queries'] = sub_queries
                job.save_meta()
        except Exception as e:
            logger.error(f"[DeepResearch] Failed to decompose query: {e}")
            raise

        # フェーズ2: 並列検索
        if job:
            job.meta['status'] = f'{len(sub_queries)}個のサブクエリを検索中...'
            job.meta['phase'] = 'searching'
            job.save_meta()

        try:
            enriched_content = self._execute_parallel_searches(sub_queries)
            logger.info(f"[DeepResearch] Collected {len(enriched_content)} enriched sources")
            if job:
                job.meta['sources_count'] = len(enriched_content)
                job.save_meta()
        except Exception as e:
            logger.error(f"[DeepResearch] Failed during search phase: {e}")
            raise

        # フェーズ3: レポート統合
        if job:
            job.meta['status'] = 'レポートを生成中...'
            job.meta['phase'] = 'synthesis'
            job.save_meta()

        try:
            report_data = self._synthesize_report(query, enriched_content)
            logger.info("[DeepResearch] Synthesis complete")
        except Exception as e:
            logger.error(f"[DeepResearch] Failed to synthesize report: {e}")
            raise

        # 引用情報の抽出
        citations = self._extract_citations(enriched_content)

        return {
            "report": report_data["report"],
            "sub_queries": sub_queries,
            "sources_count": len(enriched_content),
            "citations": citations,
            "model_used": report_data.get("model_used", "unknown")
        }

    def _decompose_query(self, query: str) -> List[str]:
        """
        クエリを3-5個のサブクエリに分解
        - JSON形式での出力を強制
        - Few-shot例を含める
        """
        prompt = f"""あなたはリサーチプランニングアシスタントです。ユーザーのクエリを3〜5個の異なる、重複しないサブクエリに分解してください。各サブクエリはWeb検索に最適化されている必要があります。

要件:
- 各サブクエリは異なる観点をカバーすること
- サブクエリは具体的で検索可能であること
- 有効なJSON配列（文字列のリスト）のみを返すこと

例1:
ユーザークエリ: 「間欠的断食の健康効果は？」
出力: ["間欠的断食 体重減少 研究", "間欠的断食 オートファジー メカニズム", "間欠的断食 リスク 副作用", "間欠的断食 vs カロリー制限 比較"]

例2:
ユーザークエリ: 「ブロックチェーン技術の仕組みは？」
出力: ["ブロックチェーン 暗号化ハッシュ関数", "ブロックチェーン コンセンサスメカニズム Proof of Work", "ブロックチェーン 分散台帳 アーキテクチャ", "ブロックチェーン スマートコントラクト 活用事例"]

例3:
ユーザークエリ: 「リモートワークの生産性への影響」
出力: ["リモートワーク 生産性 統計データ", "リモートワーク メリット デメリット", "リモートワーク コミュニケーション課題", "リモートワーク 成功事例 企業"]

それでは、次のクエリを分解してください:
ユーザークエリ: {query}

出力（JSON配列のみ）:"""

        try:
            # Geminiでクエリ分解を実行
            response_text, model = self.gemini_client.chat(
                messages=[],
                user_message=prompt,
                requested_model=self.decomposition_model
            )

            logger.info(f"[DeepResearch] Decomposition response: {response_text[:200]}...")

            # JSON抽出（マークダウンコードブロックを除去）
            response_text = response_text.strip()
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

            # JSONパース
            json_response = json.loads(response_text)

            # バリデーション
            if isinstance(json_response, list) and all(isinstance(q, str) for q in json_response):
                # 3-5個の範囲に調整
                if len(json_response) < 3:
                    logger.warning(f"[DeepResearch] Too few sub-queries ({len(json_response)}), using fallback")
                    return self._fallback_decomposition(query)
                if len(json_response) > 5:
                    logger.warning(f"[DeepResearch] Too many sub-queries ({len(json_response)}), trimming to 5")
                    json_response = json_response[:5]
                return json_response
            else:
                raise ValueError("JSON is not a list of strings")

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"[DeepResearch] Failed to parse JSON from model response: {e}. Response: {response_text}")
            return self._fallback_decomposition(query)
        except Exception as e:
            logger.error(f"[DeepResearch] Unexpected error in decomposition: {e}")
            return self._fallback_decomposition(query)

    def _fallback_decomposition(self, query: str) -> List[str]:
        """
        クエリ分解失敗時のフォールバック
        """
        logger.warning("[DeepResearch] Using fallback decomposition strategy")
        return [
            f"{query} 概要",
            f"{query} メリット 利点",
            f"{query} 課題 問題点",
            f"{query} 実例 事例"
        ]

    def _search_and_enrich_one(self, sub_query: str) -> List[Dict[str, Any]]:
        """
        1つのサブクエリに対して検索 + WebFetch

        Args:
            sub_query: サブクエリ文字列

        Returns:
            enriched_contentを含む検索結果のリスト
        """
        try:
            logger.info(f"[DeepResearch] Searching for: '{sub_query}'")

            # Top 5の検索結果を取得
            search_results = self.search_client.search(sub_query, top_k=5)

            if not search_results:
                logger.warning(f"[DeepResearch] No search results for sub-query: '{sub_query}'")
                return []

            # Top 3をWebFetchで詳細化（並列化はSearchClient内で実施される）
            enriched_results = self.search_client._enrich_search_results_with_webfetch(
                search_results[:3]
            )

            logger.info(f"[DeepResearch] Enriched {len(enriched_results)} results for '{sub_query}'")
            return enriched_results

        except Exception as e:
            logger.error(f"[DeepResearch] Failed to search and enrich for sub-query '{sub_query}': {e}")
            return []

    def _execute_parallel_searches(self, sub_queries: List[str]) -> List[Dict[str, Any]]:
        """
        すべてのサブクエリを並列実行
        - ThreadPoolExecutorで並列化
        - 各サブクエリの検索結果を統合
        """
        all_enriched_content = []

        # 並列実行（最大ワーカー数=サブクエリ数、ただし最大5）
        max_workers = min(len(sub_queries), 5)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # サブクエリごとにタスクを投入
            future_to_query = {
                executor.submit(self._search_and_enrich_one, sq): sq
                for sq in sub_queries
            }

            # 完了順に結果を収集
            for future in as_completed(future_to_query):
                query = future_to_query[future]
                try:
                    results = future.result()
                    all_enriched_content.extend(results)
                    logger.info(f"[DeepResearch] Completed search for '{query}', got {len(results)} results")
                except Exception as e:
                    logger.error(f"[DeepResearch] Search thread for query '{query}' failed: {e}")

        # URL重複を除去
        seen_urls = set()
        unique_content = []
        for item in all_enriched_content:
            url = item.get('url') or item.get('link')
            if url and url not in seen_urls:
                unique_content.append(item)
                seen_urls.add(url)

        logger.info(f"[DeepResearch] Total unique sources: {len(unique_content)} (from {len(all_enriched_content)} raw results)")
        return unique_content

    def _synthesize_report(self, original_query: str, all_content: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        すべての情報源から構造化されたMarkdownレポートを生成
        - 強制的なMarkdownテンプレート
        - 引用付き
        """
        if not all_content:
            logger.warning("[DeepResearch] No content available for synthesis")
            return {
                "report": f"# リサーチレポート: {original_query}\n\n検索結果が見つかりませんでした。",
                "model_used": "none"
            }

        # コンテキスト文字列の構築
        context_parts = []
        for i, item in enumerate(all_content, 1):
            title = item.get('title', '無題')
            url = item.get('url') or item.get('link', '')
            enriched = item.get('enriched_content', '')
            snippet = item.get('snippet', '')

            content_text = enriched if enriched else snippet

            context_parts.append(
                f"情報源 [{i}]\n"
                f"タイトル: {title}\n"
                f"URL: {url}\n"
                f"内容:\n{content_text}\n"
            )

        context_str = "\n---\n\n".join(context_parts)

        # 統合プロンプト（構造化テンプレート強制）
        prompt = f"""あなたはリサーチアナリストです。以下の複数の情報源に基づいて、包括的なリサーチレポートをMarkdown形式で生成してください。

ユーザーの元のクエリ: {original_query}

以下の情報源を分析し、構造化されたレポートを作成してください。
このレポートは**必ず以下の構造**に従ってください:

# リサーチレポート: {original_query}

## 要旨
（最も重要な発見を2-3文で簡潔にまとめる）

## 主要な発見
- 3〜5個の重要な事実や結論を箇条書き
- 各項目は情報源に基づいていること

## 詳細分析
論理的にセクション分けして詳細を記述（例: ### 技術的側面、### 利点、### 課題）
複数の情報源を統合して包括的な視点を提供すること。
単なる情報源の列挙ではなく、総合的な分析を行うこと。

## 結論
リサーチの総括と今後の展望

## 情報源
使用したすべての情報源URLをリスト形式で列挙

---
情報源データ:
---
{context_str}
---

上記の構造に厳密に従ってレポートを生成してください。
"""

        try:
            # Geminiでレポート生成
            report_text, model = self.gemini_client.chat(
                messages=[],
                user_message=prompt,
                requested_model=self.gemini_client.primary_model
            )

            return {
                "report": report_text.strip(),
                "model_used": model
            }

        except Exception as e:
            logger.error(f"[DeepResearch] Failed to generate report: {e}")
            raise

    def _extract_citations(self, all_content: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """
        引用情報の抽出
        """
        citations = []
        seen_urls = set()

        for item in all_content:
            title = item.get('title', '無題')
            url = item.get('url') or item.get('link', '')

            if url and url not in seen_urls:
                citations.append({
                    "title": title,
                    "url": url
                })
                seen_urls.add(url)

        return citations
