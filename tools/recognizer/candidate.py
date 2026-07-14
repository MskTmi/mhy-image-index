"""
Candidate 数据结构 —— 多源识别系统中各检测器的统一输出格式。

每个 Candidate 代表"某个来源认为这张图属于某个角色"，包含：
- entity:  规范化的 entity id（如 "elysia"）
- score:   该来源给出的置信度 (0.0 ~ 1.0)
- source:  来源标识（"wd14" / "filename" / "alias" / "clip"）
- evidence: 原始证据字符串（用于调试和审计）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Candidate:
    entity: str
    score: float
    source: str
    evidence: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score 必须在 [0, 1] 之间，实际为 {self.score}")


@dataclass
class RecognitionResult:
    """多源融合后的最终识别结果。"""

    entity: str
    """最终判定的 entity id"""

    confidence: float
    """融合后的综合置信度 (0.0 ~ 1.0)"""

    candidates: List[Candidate] = field(default_factory=list)
    """所有来源的原始候选列表（按分数降序）"""

    wd14_tags: dict = field(default_factory=dict)
    """WD14 输出的完整 character tag → score（供审计）"""

    @property
    def is_confident(self) -> bool:
        """综合置信度是否达到可自动归档的阈值。"""
        return self.confidence >= 0.30

    @property
    def evidence_summary(self) -> str:
        """一行摘要，方便日志输出。"""
        parts = [f"{c.source}={c.score:.2f}" for c in self.candidates[:5]]
        return ", ".join(parts) if parts else "无"
