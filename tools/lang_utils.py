"""语言检测工具 —— 用于按语言过滤 entity。

提供 detect_entity_lang() 根据 entity 的 display_name 和 id 推断其语言，
供 AliasDetector / EntityLookup 在加载时按 --lang 参数过滤。
"""

from __future__ import annotations

import re
from typing import Optional

# Unicode 区块范围
_CJK_UNIFIED = re.compile(r"[\u4e00-\u9fff]")      # 中日韩统一表意文字（中文用）
_HIRAGANA = re.compile(r"[\u3040-\u309f]")          # 平假名（日文独有）
_KATAKANA = re.compile(r"[\u30a0-\u30ff]")          # 片假名（日文独有）
_PURE_DIGIT = re.compile(r"^\d+$")                  # 纯数字


def detect_entity_lang(display_name: str, entity_id: str) -> Optional[str]:
    """推断 entity 的语言。

    策略：
    1. 优先检查 display_name
    2. 若 display_name 为空，回退检查 entity_id
    3. 返回值：'zh' / 'ja' / 'en' / None（无法判断）

    规则：
    - 含有平假名或片假名 → 'ja'
    - 含有 CJK 字且不含假名 → 'zh'
    - 纯数字 id 且无有效 display_name → None（跳过）
    - 其余情况 → 'en'
    """
    text = display_name.strip() if display_name else ""

    if not text:
        # display_name 为空，用 id 判断
        text = entity_id.strip() if entity_id else ""

    if not text:
        return None

    # 纯数字 → 跳过
    if _PURE_DIGIT.match(text):
        return None

    # 含平假名或片假名 → 日语
    if _HIRAGANA.search(text) or _KATAKANA.search(text):
        return 'ja'

    # 含 CJK 字 → 中文
    if _CJK_UNIFIED.search(text):
        return 'zh'

    # 其余（ASCII 英文、数字字母混合等）→ 英文
    return 'en'
