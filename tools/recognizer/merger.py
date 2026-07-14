"""
多源融合器 —— 将各检测器的 Candidate 列表合并为统一的识别结果。

融合策略（可配置权重）：
- 每个 source 贡献的分数乘以该 source 的权重
- 同名 entity 的分数累加（跨来源互相印证则总分更高）
- 最终按总分降序排列

默认权重反映了各来源的可靠性：
- wd14:     1.0（最高，模型直接输出）
- filename: 0.9（文件名通常准确，但可能有噪声）
- alias:    0.8（子串匹配偏低）
- clip:     0.7（CLIP 作为辅助验证）
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .candidate import Candidate, RecognitionResult

# 默认来源权重
DEFAULT_SOURCE_WEIGHTS: Dict[str, float] = {
    "wd14": 1.0,
    "filename": 0.9,
    "alias": 0.8,
    "clip": 0.7,
}


class Merger:
    """多源候选融合器。"""

    def __init__(self, source_weights: Optional[Dict[str, float]] = None) -> None:
        self.weights = source_weights or DEFAULT_SOURCE_WEIGHTS

    def merge(
        self,
        candidates: List[Candidate],
        wd14_tags: Optional[dict] = None,
        threshold: float = 0.30,
    ) -> Optional[RecognitionResult]:
        """融合所有候选，产出最终识别结果。

        Args:
            candidates: 来自所有检测器的 Candidate 列表
            wd14_tags: WD14 原始输出（写入结果供审计）
            threshold: 综合置信度阈值，低于此值返回 None

        Returns:
            RecognitionResult 或 None（无可靠候选）
        """
        if not candidates:
            return None

        # 按 entity 聚合加权分数
        entity_scores: Dict[str, float] = {}
        entity_candidates: Dict[str, List[Candidate]] = {}

        for c in candidates:
            weight = self.weights.get(c.source, 0.5)
            weighted = c.score * weight

            entity_scores[c.entity] = entity_scores.get(c.entity, 0.0) + weighted
            entity_candidates.setdefault(c.entity, []).append(c)

        if not entity_scores:
            return None

        # 找最高分
        best_entity = max(entity_scores, key=lambda k: entity_scores[k])
        best_total = entity_scores[best_entity]

        # 归一化到 [0, 1]：max_possible 只计该 entity 实际有贡献的来源权重之和
        # 例如只有 filename 命中 → max=0.9；WD14+filename 都命中 → max=1.9
        contributing_sources = set(c.source for c in entity_candidates[best_entity])
        max_possible = sum(self.weights.get(s, 0.5) for s in contributing_sources)
        confidence = min(best_total / max_possible, 1.0) if max_possible > 0 else 0.0

        if confidence < threshold:
            return None

        # 汇总该 entity 的所有候选，按分数降序
        best_candidates = sorted(entity_candidates[best_entity], key=lambda c: c.score, reverse=True)

        return RecognitionResult(
            entity=best_entity,
            confidence=round(confidence, 4),
            candidates=best_candidates,
            wd14_tags=wd14_tags or {},
        )

    def merge_all(
        self,
        candidates: List[Candidate],
        wd14_tags: Optional[dict] = None,
        top_k: int = 5,
    ) -> List[RecognitionResult]:
        """返回 top-K 个候选角色（用于人工审核场景）。

        与 merge() 的区别：不只取最佳，而是返回所有超过阈值的角色。
        """
        if not candidates:
            return []

        entity_scores: Dict[str, float] = {}
        entity_candidates: Dict[str, List[Candidate]] = {}

        for c in candidates:
            weight = self.weights.get(c.source, 0.5)
            weighted = c.score * weight
            entity_scores[c.entity] = entity_scores.get(c.entity, 0.0) + weighted
            entity_candidates.setdefault(c.entity, []).append(c)

        results: List[RecognitionResult] = []
        for entity, total in sorted(entity_scores.items(), key=lambda x: x[1], reverse=True):
            cands = sorted(entity_candidates[entity], key=lambda c: c.score, reverse=True)
            contributing_sources = set(c.source for c in cands)
            max_possible = sum(self.weights.get(s, 0.5) for s in contributing_sources)
            confidence = min(total / max_possible, 1.0) if max_possible > 0 else 0.0
            results.append(RecognitionResult(
                entity=entity,
                confidence=round(confidence, 4),
                candidates=cands,
                wd14_tags=wd14_tags or {},
            ))

        return results[:top_k]
