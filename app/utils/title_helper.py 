import re

def clean_and_shorten_title(text: str, max_length: int = 18) -> str:
    """
    会話タイトルを安全に整形・短縮する共通関数。
    - 改行や特殊文字を削除
    - 禁止文字を除去（記号や引用符など）
    - 指定文字数で安全に短縮
    """

    if not text:
        return "会話"

    # 改行や余分なスペースを除去
    title = re.sub(r"[\r\n\t]+", " ", text).strip()

    # 禁止文字や装飾記号を除去
    forbidden = ['「', '」', '"', "'", '。', '、', '：', ':', '|', '/', '\\', '　']
    for ch in forbidden:
        title = title.replace(ch, "")

    # 連続スペース → 1つ
    title = re.sub(r"\s+", " ", title)

    # 長さ制限
    if len(title) > max_length:
        title = title[:max_length - 1] + "…"

    return title.strip() or "会話"

