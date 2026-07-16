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

# Pixiv 通用泛指标签 —— 不是角色名，文件名中裸出现时跳过别名解析。
# 这些词可能合法地存在于某个 entity 的 aliases 中（如 Damselette 的"少女"），
# 但作为文件名 token 时是泛指而非角色名。遇到时宁可不识别，也不要误识别。
# 比较用归一化形式（繁简归一 + lowercase），故「少女」「女の子」「美少女」等
# 繁体/简体变体均能命中。
_GENERIC_PIXIV_TAGS = {
    # 人称/年龄泛称
    "少女", "女の子", "美少女", "萝莉", "乙女", "ロリ",
    # 版权/来源标记
    "original", "原创", "ai", "ai生成",
    # 外貌特征（发色/瞳色/肤色/体型）
    "银髪赤眼", "茶髪", "褐色", "ピンク髪ロング",
    "巨乳化", "巨乳",
    # 姿势/构图
    "四つん这い",
    # 内容/题材标记
    "百合キス",
    # Pixiv 常用诱人标签（不是角色名）
    "魅惑のふともも", "魅惑の谷间", "魅惑の尻",
    "极上の乳", "极上の女体",
    "おっぱい", "ふともも", "裸足", "全裸", "お尻", "尻神様", "原尻",
    "腋", "背中", "おへそ", "へそ", "腹", "お腹", "足指", "足里",
}


def _is_generic_pixiv_tag(token: str) -> bool:
    """token 是否属于 Pixiv 通用泛指标签（需归一化后比较以覆盖繁简变体）。"""
    from .alias_detector import _normalize_cjk
    return _normalize_cjk(token) in _GENERIC_PIXIV_TAGS


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
            # 跳过 Pixiv 通用泛指标签（如「少女」「女の子」），避免误识别
            if _is_generic_pixiv_tag(token):
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
