"""
CLIP 检测器 —— 使用 CLIP 模型对图片与候选角色进行图文匹配验证。

适合作为 WD14 之后的二次验证层：
- WD14 可能只打出 "girl, pink hair, dress" 等 general tag，没有 character tag
- CLIP 直接把图片与角色描述（如 "a character named 爱莉希雅 (Elysia) from Honkai Impact"）
  做相似度匹配，不依赖 tag 体系

依赖：pip install open-clip-torch torch（首次运行会自动下载模型 ~2GB）

用法：
    detector = ClipDetector(alias_detector)
    candidates = detector.detect(image_path)
    # → [Candidate(entity="Elysia", score=0.93, source="clip", ...), ...]

如果 CLIP 不可用（未安装或加载失败），detect() 返回空列表，不会阻断流程。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .candidate import Candidate
from .alias_detector import AliasDetector

_log = logging.getLogger(__name__)

# 默认 CLIP 模型（ViT-B/32 是精度与速度的均衡选择）
DEFAULT_CLIP_MODEL = "ViT-B-32"
DEFAULT_CLIP_PRETRAINED = "laion2b_s34b_b79k"

# 候选角色数量上限（超过则分批或只取 top-N，避免显存爆炸）
MAX_CANDIDATES_PER_BATCH = 256


class ClipDetector:
    """CLIP 图文匹配检测器。

    初始化时自动加载模型。若加载失败，_available 为 False，后续 detect() 返回空列表。
    """

    def __init__(
        self,
        alias_detector: AliasDetector,
        model_name: str = DEFAULT_CLIP_MODEL,
        pretrained: str = DEFAULT_CLIP_PRETRAINED,
        device: str = "cpu",
    ) -> None:
        self._alias = alias_detector
        self._model_name = model_name
        self._pretrained = pretrained
        self._device = device
        self._available = False
        self._model = None
        self._transform = None
        self._tokenizer = None

        self._init_model()

    def _init_model(self) -> None:
        """尝试加载 CLIP 模型。失败则标记不可用。"""
        try:
            import open_clip
            import torch

            self._model, self._transform = open_clip.create_model_and_transforms(
                self._model_name, pretrained=self._pretrained
            )
            self._tokenizer = open_clip.get_tokenizer(self._model_name)

            # 选择设备
            if self._device == "cuda" and torch.cuda.is_available():
                self._model = self._model.cuda()
            elif self._device == "auto":
                if torch.cuda.is_available():
                    self._model = self._model.cuda()
                    self._device = "cuda"
                else:
                    self._device = "cpu"

            self._model.eval()
            self._available = True
            _log.info("CLIP 模型 %s/%s 加载成功 (device=%s)", self._model_name, self._pretrained, self._device)

        except ImportError:
            _log.warning("open-clip-torch 未安装，CLIP 检测器不可用。安装：pip install open-clip-torch torch")
        except Exception as exc:
            _log.warning("CLIP 模型加载失败：%s", exc)

    # ---- 构建角色文本描述 ----

    def _build_texts(self, entity_ids: Optional[List[str]] = None) -> tuple:
        """为指定（或全部）entity 构建 CLIP 文本描述。

        返回 (texts, entity_ids)，texts 与 entity_ids 一一对应。
        """
        if entity_ids is None:
            entity_ids = self._alias.get_entity_ids()

        texts: List[str] = []
        valid_ids: List[str] = []

        for eid in entity_ids:
            display = self._alias.get_display_name(eid)
            aliases = self._alias._id_to_aliases.get(eid, [])

            # 构建描述模板："角色名, also known as 别名1, 别名2"
            if aliases:
                alias_str = ", ".join(aliases[:3])  # 最多取 3 个别名
                text = f"{display} ({eid}), also known as {alias_str}"
            else:
                text = f"{display} ({eid})"

            texts.append(text)
            valid_ids.append(eid)

        return texts, valid_ids

    # ---- 推理 ----

    def detect(
        self,
        image_path: Path,
        candidate_ids: Optional[List[str]] = None,
        top_k: int = 5,
        threshold: float = 0.20,
    ) -> List[Candidate]:
        """对图片运行 CLIP，在所有（或指定）候选角色中做图文匹配。

        Args:
            image_path: 图片路径
            candidate_ids: 限定候选角色列表；None 表示与全部 entity 比对
            top_k: 返回 top-K 个最高分角色
            threshold: 最低置信度阈值

        Returns:
            Candidate 列表（按 CLIP 相似度降序，已过滤低于 threshold 的结果）
        """
        if not self._available:
            return []

        import torch
        from PIL import Image

        try:
            image = Image.open(image_path).convert("RGB")
            image_tensor = self._transform(image).unsqueeze(0)
            if self._device == "cuda":
                image_tensor = image_tensor.cuda()
        except Exception as exc:
            _log.warning("CLIP 图片加载失败 %s: %s", image_path.name, exc)
            return []

        texts, valid_ids = self._build_texts(candidate_ids)

        if not texts:
            return []

        candidates: List[Candidate] = []

        # 分批处理避免显存爆炸
        for i in range(0, len(texts), MAX_CANDIDATES_PER_BATCH):
            batch_texts = texts[i : i + MAX_CANDIDATES_PER_BATCH]
            batch_ids = valid_ids[i : i + MAX_CANDIDATES_PER_BATCH]

            with torch.no_grad():
                text_tokens = self._tokenizer(batch_texts)
                if self._device == "cuda":
                    text_tokens = text_tokens.cuda()

                image_features = self._model.encode_image(image_tensor)
                text_features = self._model.encode_text(text_tokens)

                # 归一化
                image_features = image_features / image_features.norm(dim=-1, keepdim=True)
                text_features = text_features / text_features.norm(dim=-1, keepdim=True)

                # 余弦相似度 × 100 → softmax 得到概率分布
                similarity = (100.0 * image_features @ text_features.T).softmax(dim=-1)
                scores = similarity[0].cpu().tolist()

            for eid, score in zip(batch_ids, scores):
                if score >= threshold:
                    candidates.append(Candidate(
                        entity=eid,
                        score=round(score, 4),
                        source="clip",
                        evidence=f"CLIP sim={score:.4f}",
                    ))

        # 按分数降序排列
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:top_k]

    def verify(
        self,
        image_path: Path,
        candidate_ids: List[str],
        threshold: float = 0.20,
    ) -> List[Candidate]:
        """对指定候选角色做 CLIP 二次验证。

        这是一个轻量版本：只在给定候选范围内做图文匹配，不扫描全库。
        适合 WD14 + filename 已经给出少数候选后，用 CLIP 验证哪个最像。

        Returns:
            在 candidate_ids 范围内的 CLIP 匹配结果（过滤低于 threshold 的）
        """
        return self.detect(image_path, candidate_ids=candidate_ids, top_k=len(candidate_ids), threshold=threshold)

    @property
    def is_available(self) -> bool:
        return self._available
