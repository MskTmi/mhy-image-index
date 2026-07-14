"""
文件名检测器 —— 从图片文件名中提取角色信息。

典型文件名格式：
- Pixiv: 147081776_p0-爱莉爱2-AI生成,美少女,崩坏3,爱莉希雅,婚纱.png
- Danbooru: elysia_(honkai_impact)__abcdef1234.png
- 手动: 爱莉希雅-01.jpg

解析策略：
1. 去掉扩展名和常见前缀（pixiv id、页码等）
2. 按常见分隔符（-、,、_、空格、()）拆成 token
3. 每个 token 调用 AliasDetector.resolve_alias() 查询
4. 命中则生成 Candidate
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .candidate import Candidate
from .alias_detector import AliasDetector

# 常见文件名分隔符
_TOKEN_SPLIT_RE = re.compile(r"[-_,.\s()（）]+")

# 纯数字 token（如 pixiv id "147081776"、页码 "p0"）不需要查别名
_NUMERIC_RE = re.compile(r"^[pP]?\d+$")

# 过短的 token（如 "2"、"ab"）容易误匹配，跳过
_MIN_TOKEN_LEN = 2


class FilenameDetector:
    """文件名角色识别器。

    用法：
        detector = FilenameDetector(alias_detector)
        candidates = detector.detect("147081776_p0-爱莉希雅-婚纱.png")
        # → [Candidate(entity="Elysia", score=1.0, source="filename", evidence="爱莉希雅")]
    """

    def __init__(self, alias_detector: AliasDetector) -> None:
        self._alias = alias_detector

    def detect(self, filename: str) -> List[Candidate]:
        """从文件名中检测角色。

        filename 可以是完整路径或纯文件名。
        """
        # 取纯文件名（去掉路径）
        name = Path(filename).stem

        # 按分隔符拆 token
        tokens = [t.strip() for t in _TOKEN_SPLIT_RE.split(name) if t.strip()]

        candidates: List[Candidate] = []
        seen_entities: set = set()

        for token in tokens:
            # 跳过纯数字 token
            if _NUMERIC_RE.match(token):
                continue
            # 跳过过短 token
            if len(token) < _MIN_TOKEN_LEN:
                continue

            result = self._alias.resolve_alias(token)
            for c in result:
                if c.entity not in seen_entities:
                    seen_entities.add(c.entity)
                    candidates.append(Candidate(
                        entity=c.entity,
                        score=c.score,  # 别名匹配分数（精确=1.0, 子串=0.85）
                        source="filename",
                        evidence=f"'{token}' → {c.evidence}",
                    ))

        return candidates
