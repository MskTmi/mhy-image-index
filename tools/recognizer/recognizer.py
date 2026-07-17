"""
多源识别器 —— 对外统一接口。

整合 WD14 / 文件名 / 别名 / CLIP 四个检测器，通过融合器产出最终识别结果。

用法：
    recognizer = Recognizer(entities_dir)
    result = recognizer.recognize(image_path, filename="爱莉希雅.png")

    if result:
        print(f"识别为: {result.entity} (置信度: {result.confidence})")
        print(f"证据: {result.evidence_summary}")
    else:
        print("未识别到角色")

设计原则：
- 各检测器独立运行，结果通过 Merger 融合
- CLIP 可选用，不可用时自动跳过
- 新增检测器只需在 __init__ 中注册，在 recognize() 中调用即可
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .candidate import Candidate, RecognitionResult
from .alias_detector import AliasDetector
from .wd14_detector import Wd14Detector
from .filename_detector import FilenameDetector
from .clip_detector import ClipDetector
from .merger import Merger

_log = logging.getLogger(__name__)


class Recognizer:
    """多源融合角色识别器。

    初始化时加载 entity 数据库和 WD14 模型。
    CLIP 按需加载（仅在 use_clip=True 且首次调用 recognize() 时加载）。
    """

    def __init__(
        self,
        entities_dir: Path,
        *,
        wd14_model: str = "EVA02_Large",
        wd14_char_threshold: float = 0.7,
        wd14_general_threshold: float = 0.35,
        use_clip: bool = False,
        clip_model: str = "ViT-B-32",
        clip_pretrained: str = "laion2b_s34b_b79k",
        clip_device: str = "cpu",
        fusion_threshold: float = 0.30,
        source_weights: Optional[Dict[str, float]] = None,
        allowed_langs: Optional[List[str]] = None,
    ) -> None:
        """
        Args:
            entities_dir: entities/*.json 所在目录
            wd14_model: WD14 模型名
            wd14_char_threshold: WD14 character tag 最低置信度
            use_clip: 是否启用 CLIP 检测器
            clip_model: CLIP 模型名
            fusion_threshold: 融合后最低置信度
            source_weights: 自定义来源权重（None 使用默认）
            allowed_langs: 允许的语言列表，如 ['zh','en']。None 表示不过滤。
        """
        self.allowed_langs = allowed_langs
        # 别名检测器（所有检测器的底座）
        self.alias = AliasDetector(entities_dir, allowed_langs=allowed_langs)
        _log.info("别名检测器已加载，共 %d 个 entity", self.alias.entity_count)

        # WD14 检测器
        self.wd14 = Wd14Detector(
            self.alias,
            model_name=wd14_model,
            character_threshold=wd14_char_threshold,
            general_threshold=wd14_general_threshold,
        )

        # 文件名检测器
        self.filename = FilenameDetector(self.alias)

        # CLIP 检测器（延迟加载）
        self._use_clip = use_clip
        self._clip: Optional[ClipDetector] = None
        self._clip_model = clip_model
        self._clip_pretrained = clip_pretrained
        self._clip_device = clip_device

        # 融合器（注入 CLIP 消歧能力）
        self._confusable_pairs = self.alias.get_confusable_pairs()
        self.merger = Merger(
            source_weights=source_weights,
            confusable_pairs=self._confusable_pairs,
            clip_detector=None,  # 延迟注入，等 CLIP 加载后
        )
        self.fusion_threshold = fusion_threshold

    @property
    def clip(self) -> Optional[ClipDetector]:
        """延迟加载 CLIP 检测器。"""
        if self._use_clip and self._clip is None:
            self._clip = ClipDetector(
                self.alias,
                model_name=self._clip_model,
                pretrained=self._clip_pretrained,
                device=self._clip_device,
            )
            if not self._clip.is_available:
                _log.warning("CLIP 检测器未能加载，将跳过 CLIP 验证")
                self._use_clip = False
        return self._clip

    # ---- 主识别流程 ----

    def recognize(
        self,
        image_path: Path,
        filename: Optional[str] = None,
    ) -> Optional[RecognitionResult]:
        """对单张图片执行多源识别。

        流程：
        1. WD14 推理 → 得到 character tags
        2. 文件名解析 → 从文件名提取角色
        3. （可选）CLIP 二次验证
        4. 多源融合 → 输出最终结果

        Args:
            image_path: 图片文件路径
            filename: 原始文件名（用于文件名检测）；None 则用 image_path.name

        Returns:
            RecognitionResult 或 None（无法识别）
        """
        if filename is None:
            filename = image_path.name

        all_candidates: List[Candidate] = []
        wd14_tags: dict = {}

        # 第一层：WD14
        try:
            wd14_candidates, wd14_tags = self.wd14.detect(image_path)
            all_candidates.extend(wd14_candidates)
            if wd14_candidates:
                _log.debug("WD14: %d candidates", len(wd14_candidates))
        except Exception as exc:
            _log.warning("WD14 检测失败 %s: %s", image_path.name, exc)

        # 第二层：文件名
        try:
            fn_candidates = self.filename.detect(filename)
            all_candidates.extend(fn_candidates)
            if fn_candidates:
                _log.debug("文件名: %d candidates", len(fn_candidates))
        except Exception as exc:
            _log.warning("文件名检测失败 %s: %s", filename, exc)

        # 第三层：CLIP 二次验证（可选）
        if self._use_clip and self.clip is not None and self.clip.is_available:
            # 策略：如果前两层已经给出候选，用 CLIP 做定向验证
            if all_candidates:
                candidate_ids = list(set(c.entity for c in all_candidates))
                try:
                    clip_candidates = self.clip.verify(image_path, candidate_ids)
                    all_candidates.extend(clip_candidates)
                    if clip_candidates:
                        _log.debug("CLIP: %d candidates", len(clip_candidates))
                except Exception as exc:
                    _log.warning("CLIP 验证失败 %s: %s", image_path.name, exc)
            else:
                # 前两层无结果，CLIP 扫全库（较慢，仅在 WD14+文件名都失败时触发）
                try:
                    clip_candidates = self.clip.detect(image_path, top_k=3)
                    all_candidates.extend(clip_candidates)
                    if clip_candidates:
                        _log.debug("CLIP(全库): %d candidates", len(clip_candidates))
                except Exception as exc:
                    _log.warning("CLIP 全库扫描失败 %s: %s", image_path.name, exc)

        # CLIP 冲突消解（在融合前，对 WD14+文件名同时命中的易混淆对做二选一）
        if self._use_clip and self.clip is not None and self.clip.is_available:
            if self.merger._clip is None:
                self.merger._clip = self.clip
            if self._confusable_pairs:
                all_candidates = self.merger.disambiguate(image_path, all_candidates)

        # 融合
        result = self.merger.merge(all_candidates, wd14_tags=wd14_tags, threshold=self.fusion_threshold)
        return result

    def recognize_with_alternatives(
        self,
        image_path: Path,
        filename: Optional[str] = None,
        top_k: int = 5,
    ) -> List[RecognitionResult]:
        """识别并返回 top-K 个候选角色（用于人工审核场景）。

        与 recognize() 的区别：不只返回最佳匹配，而是列出所有可能的候选。
        """
        if filename is None:
            filename = image_path.name

        all_candidates: List[Candidate] = []
        wd14_tags: dict = {}

        # WD14
        try:
            wd14_candidates, wd14_tags = self.wd14.detect(image_path)
            all_candidates.extend(wd14_candidates)
        except Exception:
            pass

        # 文件名
        try:
            fn_candidates = self.filename.detect(filename)
            all_candidates.extend(fn_candidates)
        except Exception:
            pass

        # CLIP
        if self._use_clip and self.clip is not None and self.clip.is_available:
            candidate_ids = list(set(c.entity for c in all_candidates)) if all_candidates else None
            try:
                clip_candidates = self.clip.detect(image_path, candidate_ids=candidate_ids, top_k=top_k)
                all_candidates.extend(clip_candidates)
            except Exception:
                pass

        # CLIP 冲突消解
        if self._use_clip and self.clip is not None and self.clip.is_available:
            if self.merger._clip is None:
                self.merger._clip = self.clip
            if self._confusable_pairs:
                all_candidates = self.merger.disambiguate(image_path, all_candidates)

        return self.merger.merge_all(all_candidates, wd14_tags=wd14_tags, top_k=top_k)

    # ---- 便捷方法 ----

    def reload_entities(self) -> None:
        """重新加载 entities/ 目录（新增或修改 entity 文件后调用）。"""
        self.alias = AliasDetector(self.alias.entities_dir, allowed_langs=self.allowed_langs)
        _log.info("entity 数据库已重新加载，共 %d 个", self.alias.entity_count)
