"""
WD14 检测器 —— 使用 WD14 Tagger 模型识别图片中的角色 tag。

将 tag_image.py 中已有的 _LocalWd14 / tag_image 逻辑抽取为独立检测器，
输出 Candidate 列表（通过 AliasDetector 把 danbooru tag 转成 entity id）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .candidate import Candidate
from .alias_detector import AliasDetector

_TOOLS_DIR = Path(__file__).resolve().parent.parent
_LOCAL_CACHE_ROOT = _TOOLS_DIR / ".hf-cache"


class _LocalWd14:
    """加载本地 WD14 模型，按 csv 切片输出 rating / features / chars 三个 dict。

    从 tag_image.py 原样迁移，保持与现有模型缓存兼容。
    """

    def __init__(self, model_name: str):
        cache_dir = _LOCAL_CACHE_ROOT / model_name.lower()
        self.model_file = cache_dir / "model.onnx"
        self.labels_file = cache_dir / "selected_tags.csv"
        if not self.model_file.is_file() or not self.labels_file.is_file():
            raise FileNotFoundError(
                f"本地未找到 {model_name} 模型，请先运行 download_models.py --model {model_name}"
            )

        import pandas as pd
        from imgutils.utils import open_onnx_model

        self.model = open_onnx_model(str(self.model_file))
        df = pd.read_csv(str(self.labels_file))
        self.tag_names = df["name"].tolist()
        self.category = df["category"].tolist()
        self.rating_idx = [i for i, c in enumerate(self.category) if c == 9]
        self.general_idx = [i for i, c in enumerate(self.category) if c == 0]
        self.character_idx = [i for i, c in enumerate(self.category) if c == 4]
        self.input_name = self.model.get_inputs()[0].name
        input_shape = self.model.get_inputs()[0].shape
        self._nchw = (len(input_shape) == 4 and input_shape[1] == 3)
        self.input_size = input_shape[2] if self._nchw else input_shape[1]

        num_tags = len(self.tag_names)
        outputs = self.model.get_outputs()
        self.output_name = outputs[0].name
        self._output_is_sigmoid = False
        for out in outputs:
            shape = out.shape
            if len(shape) == 2 and shape[1] == num_tags:
                self.output_name = out.name
                self._output_is_sigmoid = (out.name == 'prediction')
                break

    def _prepare(self, image_path):
        import numpy as np
        from imgutils.tagging.wd14 import _prepare_image_for_tagging
        image = _prepare_image_for_tagging(image_path, self.input_size)
        if self._nchw:
            image = np.transpose(image, (0, 3, 1, 2))
        return image

    def tag(self, image_path, general_threshold=0.35, character_threshold=0.7):
        import numpy as np
        from imgutils.utils import sigmoid

        image = self._prepare(image_path)
        preds = self.model.run([self.output_name], {self.input_name: image})[0]
        vec = preds[0]
        scores = vec if self._output_is_sigmoid else sigmoid(vec)

        rating = {
            self.tag_names[i]: float(scores[i])
            for i in self.rating_idx
        }
        features = {
            self.tag_names[i]: float(scores[i])
            for i in self.general_idx
            if scores[i] >= general_threshold
        }
        chars = {
            self.tag_names[i]: float(scores[i])
            for i in self.character_idx
            if scores[i] >= character_threshold
        }
        return rating, features, chars


# 模型实例缓存（避免反复加载 onnx）
_WD14_INSTANCES: Dict[str, _LocalWd14] = {}


def _get_local_wd14(model_name: str) -> _LocalWd14:
    if model_name not in _WD14_INSTANCES:
        _WD14_INSTANCES[model_name] = _LocalWd14(model_name)
    return _WD14_INSTANCES[model_name]


class Wd14Detector:
    """WD14 Tagger 检测器。

    用法：
        detector = Wd14Detector(alias_detector, model_name="EVA02_Large")
        candidates, all_tags = detector.detect(image_path)
    """

    def __init__(
        self,
        alias_detector: AliasDetector,
        model_name: str = "EVA02_Large",
        character_threshold: float = 0.7,
        general_threshold: float = 0.35,
    ) -> None:
        self._alias = alias_detector
        self.model_name = model_name
        self.character_threshold = character_threshold
        self.general_threshold = general_threshold

    def detect(self, image_path: Path) -> tuple:
        """对图片运行 WD14 推理，返回 (candidates, all_character_tags)。

        candidates: 识别到的角色 Candidate 列表（含 WD14 置信度分数）
        all_character_tags: 完整的 {tag: score} dict（含未命中 entity 的 tag）
        """
        cache_dir = _LOCAL_CACHE_ROOT / self.model_name.lower()
        if not ((cache_dir / "model.onnx").is_file() and (cache_dir / "selected_tags.csv").is_file()):
            # 回落到 imgutils 网络下载路径
            from imgutils.tagging import get_wd14_tags
            _, _, chars = get_wd14_tags(
                str(image_path),
                model_name=self.model_name,
                general_threshold=self.general_threshold,
                character_threshold=self.character_threshold,
            )
        else:
            _, _, chars = _get_local_wd14(self.model_name).tag(
                str(image_path),
                general_threshold=self.general_threshold,
                character_threshold=self.character_threshold,
            )

        # 把 danbooru tag → Candidate（带上 WD14 原始分数）
        candidates: List[Candidate] = []
        for tag, score in sorted(chars.items(), key=lambda x: x[1], reverse=True):
            resolved = self._alias.resolve_tag(tag)
            for c in resolved:
                candidates.append(Candidate(
                    entity=c.entity,
                    score=score,  # 使用 WD14 原始置信度
                    source="wd14",
                    evidence=f"{tag}={score:.4f}",
                ))

        return candidates, chars
