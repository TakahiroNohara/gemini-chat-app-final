# app/constants.py
"""
繧｢繝励Μ繧ｱ繝ｼ繧ｷ繝ｧ繝ｳ蜈ｨ菴薙〒蜈ｱ譛峨＆繧後ｋ螳壽焚・・NV縺ｧ險ｭ螳壼喧・上ヨ繧ｰ繝ｫ蜿ｯ・・"""

import os
from typing import List


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    return v in ("1", "true", "yes", "on")


# 菫｡鬆ｼ繝峨Γ繧､繝ｳ縺ｮ蛻ｩ逕ｨ蜿ｯ蜷ｦ・域里螳・ 譛牙柑・峨６SE_TRUSTED_DOMAINS=false 縺ｧ辟｡蜉ｹ蛹悶・USE_TRUSTED_DOMAINS: bool = _env_bool("USE_TRUSTED_DOMAINS", True)


def _load_domains_from_env() -> List[str]:
    env_val = os.getenv("TRUSTED_BOOK_DOMAINS", "").strip()
    if env_val:
        items = [d.strip().rstrip("/") for d in env_val.split(",") if d.strip()]
        return items
    return [
        "amazon.co.jp",
        "hanmoto.com",
        "books.rakuten.co.jp",
        "bookmeter.com",
        "booklog.jp",
        "honz.jp",
    ]

# 譖ｸ邀肴ュ蝣ｱ縺ｮ菫｡鬆ｼ縺ｧ縺阪ｋ繝峨Γ繧､繝ｳ繝ｪ繧ｹ繝・
TRUSTED_BOOK_SOURCES_DOMAINS = [
    "amazon.co.jp",
    "hanmoto.com",
    "books.rakuten.co.jp",
    "bookmeter.com",
    "booklog.jp",
    "honz.jp",
]

# 讀懃ｴ｢逕ｨ縺ｮsite:繝励Ξ繝輔ぅ繝・け繧ｹ莉倥″繝ｪ繧ｹ繝・
TRUSTED_BOOK_SOURCES_SITES = [f"site:{domain}" for domain in TRUSTED_BOOK_SOURCES_DOMAINS]

