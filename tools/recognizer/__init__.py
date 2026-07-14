"""
多源角色识别器 (Multi-source Character Recognizer)

将识别流程从单一的 WD14 tag 匹配，升级为四层多源融合：
  1. WD14 Tagger    — 模型推理，danbooru character tag
  2. 文件名解析      — 从文件名 token 提取角色名
  3. 别名统一        — display_name / aliases / danbooru tag 统一映射
  4. CLIP 二次验证   — 图文匹配（可选）

用法：
    from recognizer import Recognizer

    r = Recognizer(entities_dir)
    result = r.recognize(image_path)

    if result:
        print(f"{result.entity} (confidence={result.confidence})")
"""

from .candidate import Candidate, RecognitionResult
from .recognizer import Recognizer

__all__ = ["Recognizer", "Candidate", "RecognitionResult"]
