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

CLIP 冲突消解（可选）：
- 当 WD14 / 文件名同时输出属于不同 source 的易混淆角色时
- 若 CLIP 可用，自动对这些冲突对做二选一图文匹配
- 消解后只保留 CLIP 认为更匹配的一方
- 易混淆对来源：不同 source 下 display_name 相同的角色自动检测；额外对通过 confusable_pairs 参数注入
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from .candidate import Candidate, RecognitionResult

if TYPE_CHECKING:
    from .clip_detector import ClipDetector

_log = logging.getLogger(__name__)

# 默认来源权重
DEFAULT_SOURCE_WEIGHTS: Dict[str, float] = {
    "wd14": 1.0,
    "filename": 0.9,
    "alias": 0.8,
    "clip": 0.7,
}

# CLIP 消歧时使用的区分性 prompt 模板
_DISAMBIGUATE_PROMPT = "a character named {display_name} ({entity_id}) from {sources_str}"


class Merger:
    """多源候选融合器。

    支持可选的 CLIP 冲突消解：
        merger = Merger(confusable_pairs=[("Elysia", "Cyrene")], clip_detector=clip)
        merger.disambiguate(image_path, candidates)  # 在 merge() 前调用
    """

    def __init__(
        self,
        source_weights: Optional[Dict[str, float]] = None,
        confusable_pairs: Optional[List[tuple]] = None,
        clip_detector: "Optional[ClipDetector]" = None,
    ) -> None:
        self.weights = source_weights or DEFAULT_SOURCE_WEIGHTS
        self._confusable_pairs = confusable_pairs or []
        self._clip: Optional[ClipDetector] = clip_detector
        # 构建快速查找表：entity_a → {entity_b, entity_c, ...}
        self._confusable_map: Dict[str, set] = {}
        for a, b in self._confusable_pairs:
            self._confusable_map.setdefault(a, set()).add(b)
            self._confusable_map.setdefault(b, set()).add(a)

    def disambiguate(
        self,
        image_path: Path,
        candidates: List[Candidate],
    ) -> List[Candidate]:
        """使用 CLIP 对候选中的易混淆角色对做二选一消歧。

        如果 CLIP 不可用或没有冲突对，原样返回 candidates。
        消解后，每一对中只有 CLIP 得分更高的一方保留。

        Args:
            image_path: 图片路径
            candidates: 待消歧的候选列表

        Returns:
            消歧后的候选列表（可能比输入少）
        """
        if not self._confusable_pairs or self._clip is None or not self._clip.is_available:
            return candidates

        # 收集 candidates 中出现的 entity
        entity_set = set(c.entity for c in candidates)

        # 找出实际冲突的对
        conflicts: List[tuple] = []
        for a, b in self._confusable_pairs:
            if a in entity_set and b in entity_set:
                conflicts.append((a, b))

        if not conflicts:
            return candidates

        import torch

        # 对每对冲突做 CLIP 对比
        losers: set = set()
        for a, b in conflicts:
            winner = self._clip_compare(image_path, a, b)
            if winner is None:
                continue  # CLIP 失败，保留双方
            loser = b if winner == a else a
            losers.add(loser)
            _log.debug("CLIP 消歧: %s vs %s → 保留 %s", a, b, winner)

        if losers:
            return [c for c in candidates if c.entity not in losers]
        return candidates

    def _clip_compare(self, image_path: Path, entity_a: str, entity_b: str) -> Optional[str]:
        """CLIP 二选一：返回更匹配的 entity id，失败返回 None。"""
        from PIL import Image

        text_a = self._build_disambiguate_prompt(entity_a)
        text_b = self._build_disambiguate_prompt(entity_b)

        try:
            image = Image.open(image_path).convert("RGB")
            image_tensor = self._clip._transform(image).unsqueeze(0)
            if self._clip._device == "cuda":
                image_tensor = image_tensor.cuda()

            text_tokens = self._clip._tokenizer([text_a, text_b])
            if self._clip._device == "cuda":
                text_tokens = text_tokens.cuda()

            with torch.no_grad():
                image_features = self._clip._model.encode_image(image_tensor)
                text_features = self._clip._model.encode_text(text_tokens)

                image_features = image_features / image_features.norm(dim=-1, keepdim=True)
                text_features = text_features / text_features.norm(dim=-1, keepdim=True)

                similarity = (100.0 * image_features @ text_features.T)
                scores = similarity[0].tolist()

            return entity_a if scores[0] >= scores[1] else entity_b

        except Exception as exc:
            _log.warning("CLIP 消歧失败 %s: %s", image_path.name, exc)
            # 失败时不改动，标记为平局（不丢弃任何一方）
            return None

    def _build_disambiguate_prompt(self, entity_id: str) -> str:
        """为消歧构建带区分信息的 prompt。"""
        # 通过 clip detector 的 alias 引用获取信息
        if self._clip is not None and self._clip._alias is not None:
            alias = self._clip._alias
            display = alias.get_display_name(entity_id)
            sources = alias.get_entity_sources(entity_id)
            sources_str = ", ".join(sources) if sources else "unknown"
        else:
            display = entity_id
            sources_str = "unknown"

        return _DISAMBIGUATE_PROMPT.format(
            display_name=display,
            entity_id=entity_id,
            sources_str=sources_str,
        )

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
