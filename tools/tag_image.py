"""基于多源融合的图片批量识别 + 分类工具。

识别流程（四层多源融合）：
  1. WD14 Tagger   — 模型推理 danbooru character tag
  2. 文件名解析     — 从文件名 token 提取角色名
  3. 别名统一       — display_name / aliases / tag 统一映射
  4. CLIP 二次验证  — 图文匹配（可选）

用法:
    python tools/tag_image.py [--input <待分类图片目录>] [--repo <仓库根目录>]
        [--character-threshold 0.7] [--general-threshold 0.35]
        [--model SwinV2_v3] [--quality 92]
        [--fusion-threshold 0.30] [--use-clip]

默认在 tools 下维护两个固定文件夹构成工作流：
    tools/workspace/inbox/           待处理：把要识别的图丢进来即可
    tools/workspace/processed/       处理后归档，按结果分四个子目录：
        recognized/     已成功写入 data/ 与 meta/ 的源图
        pending_review/ 检测到角色但 entity 未登记，保留供人工筛选
        unrecognized/   所有检测器均未识别到角色，留给人工分类
        duplicate/      与仓库已有图重复、已跳过的源图

跑完一轮后 inbox 会被清空，源图全部移入 processed/ 的对应子目录；
命名冲突会自动追加后缀 `_1`、`_2`，不会覆盖既有文件。

工具与作品无关：只要 entities/ 里登记了对应角色，无论是游戏 / 动漫 / 影视 /
VTuber / 舰船 都会被识别，作品归属直接取自 entity 自身的 sources 字段。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Windows 控制台默认 GBK 编码，print 中文会乱码。
# 必须在任何输出之前设置 UTF-8 模式。
if sys.platform == "win32":
    # 方式 1：控制台代码页设为 UTF-8（影响当前 shell）
    import ctypes
    ctypes.windll.kernel32.SetConsoleCP(65001)
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    # 方式 2：Python 内部重配 stdout/stderr
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 在导入 imgutils 之前先约定本工具的本地副作用目录：
#   tools/.hf-cache  —— HuggingFace 模型/标签缓存（WD14 权重几百 MB，留在仓库外）
# 两个目录都被 .gitignore 排除，不污染全局环境，也不入库。
_TOOLS_DIR = Path(__file__).resolve().parent
_DEFAULT_HF_HOME = _TOOLS_DIR / ".hf-cache"
LOGS_DIR = _TOOLS_DIR / "logs"
os.environ.setdefault("HF_HOME", str(_DEFAULT_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_DEFAULT_HF_HOME / "hub"))
os.environ.setdefault("HF_XET_CACHE", str(_DEFAULT_HF_HOME / "xet"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 自动发现 nvidia pip 包的 DLL 路径（nvidia-cublas-cu12 等），加到 PATH
# 使 onnxruntime-gpu 能加载 CUDA 运行时，无需安装完整 CUDA Toolkit。
def _setup_cuda_dll_path() -> None:
    import sys as _sys
    try:
        import nvidia  # noqa: F401
        _nv_base = Path(_sys.prefix) / "Lib" / "site-packages" / "nvidia"
        # rglob("bin") 找到的 p 就是 bin 目录本身，要加进 PATH 的是 bin 目录，
        # 不是它的父目录（否则 cublasLt64_12.dll 在 nvidia\cublas\bin\ 下找不到）。
        _bin_dirs = sorted(set(
            str(p) for p in _nv_base.rglob("bin") if p.is_dir()
        ))
        if _bin_dirs:
            _path_sep = os.pathsep
            _current = os.environ.get("PATH", "")
            for d in _bin_dirs:
                if d not in _current:
                    os.environ["PATH"] = d + _path_sep + os.environ.get("PATH", "")
    except ImportError:
        pass  # 未装 nvidia pip 包，跳过

_setup_cuda_dll_path()

from entities_db import EntityLookup, load_entity_db
from recognizer import Recognizer

# ---- 自动创建实体 ----


def auto_create_entities(
    unmatched_tags: List[str],
    lookup: EntityLookup,
    entities_dir: Path,
) -> List[str]:
    """为未登记的 character tag 批量创建占位 entity 并注册到 lookup。

    返回新创建的 entity id 列表。
    """
    new_ids: List[str] = []
    for tag in unmatched_tags:
        eid = lookup.add_auto(tag, entities_dir)
        if eid:
            new_ids.append(eid)
    return new_ids


# ----------------------------------------------------------------

# 优先检测 tools/.hf-cache/<model>/ 下本地模型文件，若有则直接读取，
# 跳过 hf_hub 网络层。本地缺失时回落到 imgutils 默认路径。
_LOCAL_CACHE_ROOT = _TOOLS_DIR / ".hf-cache"


def _patch_imgutils_to_local_cache(model_name: str) -> bool:
    """检测本地缓存是否齐全，齐全则 patch imgutils 内部 WD14 加载函数。

    返回 True 表示已 patch（用本地路径）；False 表示未 patch（仍走 hf_hub）。
    v3 系模型没有 inv.npz，缺失时跳过 weights 加载，imgutils 内部对 v3 模型
    也不会调用 _get_wd14_weights（v3 直接走 onnx 输出，不依赖 inv 矩阵）。
    """
    cache_dir = _LOCAL_CACHE_ROOT / model_name.lower()
    model_file = cache_dir / "model.onnx"
    weights_file = cache_dir / "inv.npz"
    labels_file = cache_dir / "selected_tags.csv"
    has_weights = weights_file.is_file()
    if not (model_file.is_file() and labels_file.is_file()):
        return False

    # 这些导入会触发 imgutils -> onnxruntime -> huggingface_hub 的初始化，但还不会
    # 真正下模型（懒加载），patch 在调用 get_wd14_tags 之前生效即可。
    import numpy as np
    import pandas as pd
    from imgutils.tagging import wd14 as _wd14
    from imgutils.utils import open_onnx_model

    _wd14._get_wd14_model = lambda name: open_onnx_model(str(model_file))
    if has_weights:
        _wd14._get_wd14_weights = lambda name: np.load(str(weights_file))

    def _local_labels(name, no_underline=False):
        df = pd.read_csv(str(labels_file))
        name_series = df["name"]
        if no_underline:
            name_series = name_series.map(_wd14.remove_underline)
        tag_names = name_series.tolist()
        rating_indexes = df.index[df["category"] == 9].tolist()
        general_indexes = df.index[df["category"] == 0].tolist()
        character_indexes = df.index[df["category"] == 4].tolist()
        return tag_names, rating_indexes, general_indexes, character_indexes

    _wd14._get_wd14_labels = _local_labels
    return True


# SmilingWolf 的 v2 / v3 模型在 onnx 里都只有一个输出（shape [N, 9083]），
# 把 rating + general + character 全部塞在同一个向量里，按 selected_tags.csv
# 里的 category 列分片。imgutils 0.19 的 get_wd14_tags 期望 2 输出（旧格式）会
# 触发 AssertionError，所以我们自己跑 onnx 推理，绕开这个不兼容的函数。
_V2_OUTPUT_WARN_PRINTED = False


class _LocalWd14:
    """加载本地 WD14 模型，按 csv 切片输出 rating / features / chars 三个 dict。"""

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
        # category 取值：0=general, 4=character, 9=rating
        self.rating_idx = [i for i, c in enumerate(self.category) if c == 9]
        self.general_idx = [i for i, c in enumerate(self.category) if c == 0]
        self.character_idx = [i for i, c in enumerate(self.category) if c == 4]
        self.input_name = self.model.get_inputs()[0].name
        input_shape = self.model.get_inputs()[0].shape
        # 自动检测 NCHW vs NHWC：NCHW=[N,3,H,W]，NHWC=[N,H,W,3]
        self._nchw = (len(input_shape) == 4 and input_shape[1] == 3)
        self.input_size = input_shape[2] if self._nchw else input_shape[1]  # 448

        # 选择输出：优先取维度匹配标签数的（如 logits/prediction），
        # 部分模型（PixAI）同时输出 embedding/logits/prediction 多个张量
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
        # WD14 用 NHWC [1,H,W,3]，PixAI ONNX 用 NCHW [1,3,H,W]
        if self._nchw:
            image = np.transpose(image, (0, 3, 1, 2))  # NHWC → NCHW
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


_WD14_INSTANCES: Dict[str, _LocalWd14] = {}


def _get_local_wd14(model_name: str) -> _LocalWd14:
    if model_name not in _WD14_INSTANCES:
        _WD14_INSTANCES[model_name] = _LocalWd14(model_name)
    return _WD14_INSTANCES[model_name]

# 支持 .jpg/.jpeg/.png/.webp/.bmp/.gif 这几个常见格式
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 跟仓库 JS 那批保持一致的 id 字母表（小写字母 + 数字）
ID_ALPHABET = string.digits + string.ascii_lowercase
ID_LENGTH = 8

# 推荐模型：EVA02_Large（精度最高，1.26GB）和 SwinV2_v3（均衡，467MB）
# 完整列表见 download_models.py
DEFAULT_MODEL = "EVA02_Large"
DEFAULT_CHAR_THRESHOLD = 0.7
DEFAULT_GENERAL_THRESHOLD = 0.35
DEFAULT_QUALITY = 92

# tools 下的固定工作目录名。INBOX 放待处理图，PROCESSED 下按结果分三类归档。
INBOX_DIR_NAME = "workspace/inbox"
PROCESSED_DIR_NAME = "workspace/processed"
PROCESSED_RECOGNIZED = "recognized"
PROCESSED_UNRECOGNIZED = "unrecognized"
PROCESSED_DUPLICATE = "duplicate"
PROCESSED_PENDING = "pending_review"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于 WD14 Tagger 的批量图片识别与分类工具。"
    )
    parser.add_argument(
        "--input",
        default=None,
        help="待分类的图片所在目录（递归扫描），默认 tools/workspace/inbox；"
        "处理后源图会被移动到 tools/workspace/processed/ 下归档。",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="仓库根目录，默认取 tools 目录的上一级；data/ 与 meta/ 会写到该目录下。",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="WD14 模型名（imgutils 提供的 model_name）")
    parser.add_argument("--character-threshold", type=float, default=DEFAULT_CHAR_THRESHOLD, help="character tag 置信度阈值")
    parser.add_argument("--general-threshold", type=float, default=DEFAULT_GENERAL_THRESHOLD, help="general tag 置信度阈值（识别只看 character，仅影响模型内部裁剪）")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="写入 data/ 的 JPEG 质量 1-100")
    parser.add_argument(
        "--fusion-threshold",
        type=float,
        default=0.30,
        help="多源融合后最低置信度（0.0~1.0），低于此值归档到 unrecognized",
    )
    parser.add_argument(
        "--use-clip",
        action="store_true",
        help="启用 CLIP 二次验证（需 pip install open-clip-torch torch，首次运行自动下载模型 ~2GB）",
    )
    parser.add_argument(
        "--no-compress",
        dest="compress",
        action="store_false",
        help="禁用智能压缩，直接保存 JPEG（文件更大但颜色完全无损）",
    )
    parser.set_defaults(compress=True)
    parser.add_argument(
        "--auto-create",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="发现未登记角色时自动创建 entity 占位文件（WD14 未命中 tag 或文件名提示均可触发），"
        "创建后自动重新识别。使用 --no-auto-create 禁用。",
    )
    parser.add_argument(
        "--dump-tags",
        action="store_true",
        help="仅输出每张图 WD14 识别到的所有 character tag 及置信度（按分数降序），"
        "不做 entity 匹配、不移动文件、不写入 data/meta。用于诊断模型识别能力。",
    )
    return parser.parse_args(argv)


def resolve_repo(args: argparse.Namespace) -> Path:
    if args.repo:
        return Path(args.repo).expanduser().resolve()
    # 默认取 tools 目录的父目录（仓库根）
    return Path(__file__).resolve().parent.parent


def find_images(input_dir: Path) -> List[Path]:
    images: List[Path] = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
            images.append(path)
    return images


def generate_id(data_dir: Path, meta_dir: Path) -> str:
    while True:
        candidate = "".join(random.choices(ID_ALPHABET, k=ID_LENGTH))
        if (data_dir / f"{candidate}.jpg").exists() or (meta_dir / f"{candidate}.json").exists():
            continue
        return candidate


def load_existing_hashes(meta_dir: Path) -> Dict[str, str]:
    """把已有 meta 里的 hash -> image 路径映射加载出来用于查重。"""
    hashes: Dict[str, str] = {}
    if not meta_dir.is_dir():
        return hashes
    for file_path in meta_dir.glob("*.json"):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("hash"):
            hashes[str(data["hash"])] = str(data.get("image", ""))
    return hashes


def optimize_to_jpeg(source: Path, dest: Path, quality: int, compress: bool = False) -> tuple[int, int, str]:
    """把任意常见图片格式转 JPEG（按 EXIF 旋转，丢弃原始元数据）。

    当 compress=True 时启用 TinyPNG 风格的智能压缩：
      1. 对图片做自适应调色板量化（减少冗余颜色），尤其适合二次元图
      2. 关闭色度子采样（subsampling="4:4:4"），保留线条/文字锐度
      3. 量化可能轻微偏色，对写实照片会跳过量化保留原色

    返回 (width, height, sha256)，元数据从输出文件重新读取以保证 meta 里写的是
    压缩后文件的真实尺寸与哈希。
    """
    from PIL import Image  # 延迟导入，避免只是 --help 也要装 Pillow

    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as img:
        img = img.convert("RGB")
        original_size = img.size

        if compress:
            max_dim = max(img.size)
            if max_dim > 3000:
                ratio = 3000 / max_dim
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 先保存一份不带量化/子采样的基准 JPEG，作为回退参照
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                baseline_path = tmp.name
            try:
                img.save(baseline_path, format="JPEG", quality=quality, optimize=True)
                baseline_size = Path(baseline_path).stat().st_size

                # 自适应调色板量化：用中位切法把颜色压缩到 256 色
                # 然后转回 RGB 保存 JPEG，Huffman 表优化 + 无子采样
                try:
                    quantized = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
                    quantized = quantized.convert("RGB")
                except (ValueError, OSError):
                    quantized = img  # 量化失败，回退原图

                quantized.save(dest, format="JPEG", quality=quality, optimize=True, subsampling="4:4:4")
                if dest.stat().st_size > baseline_size:
                    # 压缩后反而变大，用基准 JPEG 覆盖
                    shutil.copy2(baseline_path, str(dest))
            finally:
                Path(baseline_path).unlink(missing_ok=True)
        else:
            img.save(dest, format="JPEG", quality=quality, optimize=True)

    with Image.open(dest) as out:
        width, height = out.size

    with open(dest, "rb") as fp:
        digest = hashlib.sha256(fp.read()).hexdigest()

    return width, height, digest


def tag_image(image_path: Path, model_name: str, general_threshold: float, character_threshold: float) -> Dict[str, float]:
    """跑 WD14 拿到 character dict（tag -> 置信度）。

    优先用本地缓存的 _LocalWd14（兼容 v2/v3 单输出 onnx）。本地缓存缺失时
    回落到 imgutils.get_wd14_tags（需要网络且仅兼容旧双输出格式）。
    """
    cache_dir = _LOCAL_CACHE_ROOT / model_name.lower()
    if (cache_dir / "model.onnx").is_file() and (cache_dir / "selected_tags.csv").is_file():
        _, _, chars = _get_local_wd14(model_name).tag(
            str(image_path),
            general_threshold=general_threshold,
            character_threshold=character_threshold,
        )
        return chars

    from imgutils.tagging import get_wd14_tags
    _, _, chars = get_wd14_tags(
        str(image_path),
        model_name=model_name,
        general_threshold=general_threshold,
        character_threshold=character_threshold,
    )
    return chars


def write_meta(meta_path: Path, *, uid: str, sources: List[str], entities: List[str], sha: str, width: int, height: int) -> None:
    meta = {
        "id": uid,
        "image": f"data/{uid}.jpg",
        "hash": sha,
        "width": width,
        "height": height,
        "sources": sources,
        "entities": entities,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def archive_source(src: Path, tools_dir: Path, input_root: Path, category: str) -> Path:
    """把处理完的源图按相对原 input 目录的子路径移动到 tools/processed/<category>/。

    category 取值：recognized / unrecognized / duplicate。同目录内若已有同名文件，
    自动追加 `_1`、`_2` 后缀避免覆盖。移动而非复制，处理完 inbox 自然清空。
    """
    dest_root = tools_dir / PROCESSED_DIR_NAME / category
    rel = src.relative_to(input_root)
    dest = dest_root / rel
    if dest.exists():
        stem, suffix, counter = dest.stem, dest.suffix, 1
        while dest.exists():
            dest = dest.with_name(f"{stem}_{counter}{suffix}")
            counter += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


# 常见分词符号
_NAME_SPLIT_RE = re.compile(r"[-_,.\s()（）]+")
# 看起来像角色名的启发式规则：含中日韩字符 或 大驼峰英文单词
_NAME_HINT_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]|[A-Z][a-z]+")
# 明显不是角色名的常见词黑名单
_NAME_BLACKLIST = {"ai", "ai生成", "png", "jpg", "jpeg", "webp", "美少女", "女の子", "女の子", "原创", "3rd"}
# Pixiv "收藏数 users入り" 系列标记（5000users入り / 10000users入り 等），不是角色名
_USERS_IRI_RE = re.compile(r"^\d+users入り$", re.IGNORECASE)


def _extract_name_hints(filename: str, source_keywords: dict = None) -> list:
    """从文件名中提取可能的角色名，用于提示用户创建 entity。"""
    stem = Path(filename).stem
    tokens = [t.strip() for t in _NAME_SPLIT_RE.split(stem) if t.strip()]

    # 收集已知作品名关键词
    source_terms = set()
    if source_keywords:
        for keywords in source_keywords.values():
            for kw in keywords:
                source_terms.add(kw.lower())

    hints = []
    for t in tokens:
        low = t.lower()
        if len(t) < 2 or low.isdigit() or (len(low) >= 2 and low[0] == 'p' and low[1:].isdigit()):
            continue
        if low in _NAME_BLACKLIST:
            continue
        if _USERS_IRI_RE.match(low):
            continue  # 跳过 Pixiv "N users入り" 标记
        if low in source_terms:
            continue  # 跳过已知作品名
        if _NAME_HINT_RE.search(t):
            hints.append(t)
    # 优先：含中文的排前面（最像角色名），然后按长度降序
    def _sort_key(s: str) -> tuple:
        has_cjk = int(any('\u4e00' <= c <= '\u9fff' for c in s))
        return (-has_cjk, -len(s), s.lower())
    hints.sort(key=_sort_key)
    return hints


def _create_entity_from_hint(hint: str, entities_dir: Path, source_keywords: dict = None) -> str:
    """根据文件名提示创建一个最小的 entity JSON 文件。

    返回 entity id，文件已写入 entities/。
    如果 source_keywords 有匹配，自动填入 sources。
    """
    # 用中文名作 display_name，英文大驼峰作 id
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in hint)
    if has_cjk:
        safe_id = re.sub(r'[^a-zA-Z0-9_]', '', hint)
        if not safe_id:
            safe_id = f"char_{hash(hint) & 0xFFFF:04x}"
        entity = {
            "id": safe_id,
            "display_name": hint,
            "sources": [],
            "aliases": [],
        }
    else:
        safe_id = hint[0].upper() + hint[1:].lower() if len(hint) > 1 else hint.upper()
        entity = {
            "id": safe_id,
            "display_name": "",
            "sources": [],
            "aliases": [],
        }

    # 从 source_keywords 推测作品归属
    if source_keywords:
        # 检查 hint 本身是否匹配
        hint_low = hint.lower()
        for source_id, keywords in source_keywords.items():
            for kw in keywords:
                if kw.lower() in hint_low:
                    if source_id not in entity["sources"]:
                        entity["sources"].append(source_id)
                    break
    if not entity["sources"]:
        entity["sources"] = ["unknown"]

    file_path = entities_dir / f"{safe_id}.json"
    file_path.write_text(json.dumps(entity, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return safe_id


def _guess_sources_from_filename(filename: str, entity_path: Path, source_keywords: dict) -> None:
    """从文件名关键词推测 sources，回写到 entity JSON 中。"""
    if not source_keywords or not entity_path.exists():
        return
    try:
        entity = json.loads(entity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    filename_low = filename.lower()
    guessed = list(entity.get("sources", []))
    if "unknown" in guessed:
        guessed.remove("unknown")

    for source_id, keywords in source_keywords.items():
        for kw in keywords:
            if kw.lower() in filename_low and source_id not in guessed:
                guessed.append(source_id)
                break

    if guessed and guessed != entity.get("sources", []):
        entity["sources"] = guessed
        entity_path.write_text(json.dumps(entity, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not (1 <= args.quality <= 100):
        print("错误：--quality 必须在 1-100 之间。", file=sys.stderr)
        return 2

    # ---- 日志：捕获 stdout 全部输出到日志文件 ----
    log_buffer = io.StringIO()

    class _TeeWriter:
        """同时写入原始 stdout 和日志缓冲区。"""
        def __init__(self, original, buffer):
            self.original = original
            self.buffer = buffer

        def write(self, s: str) -> int:
            self.original.write(s)
            self.buffer.write(s)
            return len(s)

        def flush(self) -> None:
            self.original.flush()

        def isatty(self) -> bool:
            return hasattr(self.original, "isatty") and self.original.isatty()

    _orig_stdout = sys.stdout
    sys.stdout = _TeeWriter(_orig_stdout, log_buffer)  # type: ignore[assignment]

    try:
        return _main_impl(args, log_buffer)
    finally:
        sys.stdout = _orig_stdout


def _main_impl(args: argparse.Namespace, log_buffer: io.StringIO) -> int:
    """主逻辑（在 stdout tee 的保护下运行）。"""

    # 启动前优先用 tools/.hf-cache/<model>/ 本地模型；找不到运行时会回落到 hf_hub。
    local_cache_dir = _LOCAL_CACHE_ROOT / args.model.lower()
    if (local_cache_dir / "model.onnx").is_file() and (local_cache_dir / "selected_tags.csv").is_file():
        print(f"[info] 使用本地缓存的 WD14 模型：{local_cache_dir}")
    else:
        print(
            f"[info] 未找到本地 WD14 缓存，将尝试通过 huggingface_hub 下载 {args.model}。"
            f" 若失败请先运行 download_models.py 拉取本地副本。",
            file=sys.stderr,
        )

    tools_dir = Path(__file__).resolve().parent
    if args.input:
        input_dir = Path(args.input).expanduser().resolve()
    else:
        input_dir = tools_dir / INBOX_DIR_NAME
    input_dir.mkdir(parents=True, exist_ok=True)
    if not input_dir.is_dir():
        print(f"错误：输入目录不存在：{input_dir}", file=sys.stderr)
        return 2

    repo_root = resolve_repo(args)
    data_dir = repo_root / "data"
    meta_dir = repo_root / "meta"
    entities_dir = repo_root / "entities"
    # processed 下按结果分三类：识别成功 / 未识别 / 重复。
    processed_root = tools_dir / PROCESSED_DIR_NAME
    processed_dirs = {
        PROCESSED_RECOGNIZED: processed_root / PROCESSED_RECOGNIZED,
        PROCESSED_UNRECOGNIZED: processed_root / PROCESSED_UNRECOGNIZED,
        PROCESSED_DUPLICATE: processed_root / PROCESSED_DUPLICATE,
        PROCESSED_PENDING: processed_root / PROCESSED_PENDING,
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    entities_dir.mkdir(parents=True, exist_ok=True)
    for d in processed_dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    lookup = load_entity_db(entities_dir)
    if lookup.is_empty:
        print(
            "警告：entities/ 中没有任何已登记角色，所有图都会归档到 processed/unrecognized/。",
            file=sys.stderr,
        )

    existing_hashes = load_existing_hashes(meta_dir)
    images = find_images(input_dir)
    if not images:
        print(f"输入目录下没有图片：{input_dir}", file=sys.stderr)
        return 1

    print(f"找到 {len(images)} 张图，开始识别（仓库根：{repo_root}）")

    # --dump-tags 模式：仅输出模型原始 character tag，不做 entity 匹配
    if args.dump_tags:
        print(f"模型：{args.model}，character 阈值：{args.character_threshold}\n")
        total_with_tags = 0
        total_no_tags = 0
        for src in images:
            try:
                chars = tag_image(src, args.model, args.general_threshold, args.character_threshold)
            except Exception as exc:
                print(f"[X] {src.name} 标注失败：{exc}\n")
                continue

            if not chars:
                print(f"[?] {src.name} — 阈值 {args.character_threshold} 以上无任何 character tag\n")
                total_no_tags += 1
                continue

            total_with_tags += 1
            sorted_tags = sorted(chars.items(), key=lambda x: x[1], reverse=True)
            # 只显示已有 entity 的 tag（加 * 标记）和分数 ≥0.5 的未知 tag
            known = [(t, s) for t, s in sorted_tags if lookup.tag_to_ids.get(t.lower().strip())]
            high_unknown = [(t, s) for t, s in sorted_tags if not lookup.tag_to_ids.get(t.lower().strip()) and s >= 0.5]

            print(f"[>] {src.name} （共 {len(sorted_tags)} 个 character tag）")
            if known:
                print(f"    ★ 已登记 entity：")
                for tag, score in known:
                    ids = lookup.tag_to_ids[tag.lower().strip()]
                    names = [lookup.id_to_name.get(eid, eid) for eid in ids]
                    print(f"      {tag} = {score:.4f}  →  {', '.join(names)}")
            if high_unknown:
                print(f"    ? 未登记但高置信（≥0.5）：")
                for tag, score in high_unknown[:20]:  # 最多显示 20 个
                    print(f"      {tag} = {score:.4f}")
            if not known and not high_unknown:
                top_n = sorted_tags[:5]
                print(f"    最高分 tag：")
                for tag, score in top_n:
                    print(f"      {tag} = {score:.4f}")
            print()

        print(f"汇总：{total_with_tags} 张有 tag，{total_no_tags} 张无 tag（阈值以上）")
        return 0

    # ---- 多源融合识别模式 ----
    recognizer = Recognizer(
        entities_dir,
        wd14_model=args.model,
        wd14_char_threshold=args.character_threshold,
        wd14_general_threshold=args.general_threshold,
        use_clip=args.use_clip,
        fusion_threshold=args.fusion_threshold,
    )
    # 加载 source_keywords 用于 auto-create 时推测作品
    _tag_map_path = _TOOLS_DIR / "danbooru_tag_map.json"
    source_keywords = {}
    if _tag_map_path.is_file():
        try:
            _tag_config = json.loads(_tag_map_path.read_text(encoding="utf-8"))
            source_keywords = _tag_config.get("source_keywords", {})
        except (OSError, json.JSONDecodeError):
            pass
    print(f"[info] 模型: {args.model} | 融合阈值: {args.fusion_threshold} | CLIP: {'启用' if args.use_clip else '未启用'} | 压缩: {'启用' if args.compress else '关闭'}")
    print(f"[info] 输入: {input_dir.relative_to(tools_dir)} ({len(images)} 张图)")
    print()

    recognized = 0
    unrecognized = 0
    duplicates = 0
    pending_review = 0
    auto_created_entities: Dict[str, str] = {}  # entity_id -> danbooru tag，用于最终汇总
    # 多实体阈值：主 entity 需 >= fusion_threshold，次要 entity >= secondary_threshold
    secondary_threshold = max(args.fusion_threshold * 0.5, 0.15)

    for src in images:
        # ---- 多源融合识别（返回所有候选角色，支持多实体） ----
        try:
            all_results = recognizer.recognize_with_alternatives(src, top_k=10)
        except Exception as exc:
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
            print(f"[X] {src.name}\n")
            print(f"  error   : {exc}\n")
            print(f"  ✓ archive   {dest.relative_to(tools_dir)}")
            print()
            unrecognized += 1
            continue

        if not all_results:
            # 尝试直接从 WD14 raw tags 获取未登记的 tag
            try:
                _, wd14_tags = recognizer.wd14.detect(src)
            except Exception:
                wd14_tags = {}

            if wd14_tags:
                unmatched = [t for t in wd14_tags if not recognizer.alias.resolve_tag(t)]
                if unmatched and args.auto_create:
                    new_ids = auto_create_entities(unmatched, lookup, entities_dir)
                    for eid in new_ids:
                        auto_created_entities[eid] = eid
                    if new_ids:
                        recognizer.reload_entities()
                        try:
                            all_results = recognizer.recognize_with_alternatives(src, top_k=10)
                        except Exception:
                            all_results = []

            if not all_results:
                if wd14_tags:
                    tags_str = ", ".join(
                        f"{t}={wd14_tags[t]:.2f}"
                        for t in sorted(wd14_tags, key=lambda t: wd14_tags[t], reverse=True)[:10]
                    )
                    dest = archive_source(src, tools_dir, input_dir, PROCESSED_PENDING)
                    print(f"[~] {src.name}\n")
                    print(f"  reason  : WD14 有 tag 但未命中任何 entity")
                    print(f"  tags    : {tags_str}\n")
                    print(f"  ✓ archive   {dest.relative_to(tools_dir)}")
                    pending_review += 1
                else:
                    # 从文件名提取可能的角色名
                    name_hints = _extract_name_hints(src.name, source_keywords)
                    if name_hints and args.auto_create:
                        best_hint = name_hints[0]
                        new_id = _create_entity_from_hint(best_hint, entities_dir, source_keywords)
                        _guess_sources_from_filename(src.name, entities_dir / f"{new_id}.json", source_keywords)
                        auto_created_entities[new_id] = best_hint
                        recognizer.reload_entities()
                        try:
                            all_results = recognizer.recognize_with_alternatives(src, top_k=10)
                        except Exception:
                            all_results = []
                        if all_results:
                            print(f"[?] {src.name}\n")
                            print(f"  → 自动创建 entities/{new_id}.json，重新识别\n")
                            # fall through to 识别成功流程
                        else:
                            all_results = []
                            # 自动创建后仍未识别，继续走 unrecognized
                            dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
                            print(f"[?] {src.name}\n")
                            print(f"  reason  : 所有检测器均未识别到角色")
                            print(f"  → 已自动创建 entities/{new_id}.json（但仍未识别）")
                            print(f"\n  ✓ archive   {dest.relative_to(tools_dir)}")
                            unrecognized += 1
                            continue
                    else:
                        dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
                        print(f"[?] {src.name}\n")
                        print(f"  reason  : 所有检测器均未识别到角色")
                        if name_hints:
                            hints_str = ", ".join(name_hints[:5])
                            print(f"  suggest : 创建 entities/{{id}}.json  →  {hints_str}")
                        print(f"\n  ✓ archive   {dest.relative_to(tools_dir)}")
                        unrecognized += 1
                        continue
                if not all_results:
                    continue

        # ---- 筛选：主实体 + 次要实体 ----
        primary = [r for r in all_results if r.confidence >= args.fusion_threshold]
        secondary = [r for r in all_results if secondary_threshold <= r.confidence < args.fusion_threshold]
        all_entities = primary + secondary

        if not primary:
            # 主实体不达标 → 检查 WD14 raw tags，自动创建 entity 后再试
            try:
                _, wd14_tags = recognizer.wd14.detect(src)
            except Exception:
                wd14_tags = {}

            if wd14_tags:
                unmatched = [t for t in wd14_tags if not recognizer.alias.resolve_tag(t)]
                if unmatched and args.auto_create:
                    new_ids = auto_create_entities(unmatched, lookup, entities_dir)
                    for eid in new_ids:
                        auto_created_entities[eid] = eid
                    if new_ids:
                        recognizer.reload_entities()
                        try:
                            all_results = recognizer.recognize_with_alternatives(src, top_k=10)
                            primary = [r for r in all_results if r.confidence >= args.fusion_threshold]
                            secondary = [r for r in all_results if secondary_threshold <= r.confidence < args.fusion_threshold]
                            all_entities = primary + secondary
                        except Exception:
                            pass

            if not primary:
                if all_entities:
                    entity_list = ", ".join(
                        f"{r.entity}({r.confidence:.2f})" for r in all_entities[:5]
                    )
                    dest = archive_source(src, tools_dir, input_dir, PROCESSED_PENDING)
                    print(f"[~] {src.name}\n")
                    print(f"  reason     : 融合置信度不足")
                    print(f"  candidates : {entity_list}\n")
                    print(f"  ✓ archive   {dest.relative_to(tools_dir)}")
                    print()
                    pending_review += 1
                    continue
                elif wd14_tags:
                    tags_str = ", ".join(
                        f"{t}={wd14_tags[t]:.2f}"
                        for t in sorted(wd14_tags, key=lambda t: wd14_tags[t], reverse=True)[:10]
                    )
                    dest = archive_source(src, tools_dir, input_dir, PROCESSED_PENDING)
                    print(f"[~] {src.name}\n")
                    print(f"  reason  : WD14 有 tag 但多源融合未达标")
                    print(f"  tags    : {tags_str}\n")
                    print(f"  ✓ archive   {dest.relative_to(tools_dir)}")
                    print()
                    pending_review += 1
                    continue
                else:
                    # 从文件名提取可能的角色名
                    name_hints = _extract_name_hints(src.name, source_keywords)
                    if name_hints and args.auto_create:
                        best_hint = name_hints[0]
                        new_id = _create_entity_from_hint(best_hint, entities_dir, source_keywords)
                        _guess_sources_from_filename(src.name, entities_dir / f"{new_id}.json", source_keywords)
                        auto_created_entities[new_id] = best_hint
                        recognizer.reload_entities()
                        try:
                            all_results = recognizer.recognize_with_alternatives(src, top_k=10)
                        except Exception:
                            all_results = []
                        if all_results:
                            primary = [r for r in all_results if r.confidence >= args.fusion_threshold]
                            secondary = [r for r in all_results if secondary_threshold <= r.confidence < args.fusion_threshold]
                            all_entities = primary + secondary
                            if primary:
                                print(f"[?] {src.name}\n")
                                print(f"  → 自动创建 entities/{new_id}.json，重新识别\n")
                                # fall through to 识别成功
                            else:
                                dest = archive_source(src, tools_dir, input_dir, PROCESSED_PENDING)
                                print(f"[~] {src.name}\n")
                                print(f"  → 已自动创建 entities/{new_id}.json")
                                print(f"  reason  : 融合置信度不足\n")
                                print(f"  ✓ archive   {dest.relative_to(tools_dir)}")
                                print()
                                pending_review += 1
                                continue
                        else:
                            dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
                            print(f"[?] {src.name}\n")
                            print(f"  reason  : 所有检测器均未识别到角色")
                            print(f"  → 已自动创建 entities/{new_id}.json（但仍未识别）")
                            print(f"\n  ✓ archive   {dest.relative_to(tools_dir)}")
                            print()
                            unrecognized += 1
                            continue
                    else:
                        dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
                        print(f"[?] {src.name}\n")
                        print(f"  reason  : 所有检测器均未识别到角色")
                        if name_hints:
                            hints_str = ", ".join(name_hints[:5])
                            print(f"  suggest : 创建 entities/{{id}}.json  →  {hints_str}")
                        print(f"\n  ✓ archive   {dest.relative_to(tools_dir)}")
                        print()
                        unrecognized += 1
                        continue

        # ---- 识别成功：写入 data/meta ----
        uid = generate_id(data_dir, meta_dir)
        dest_jpg = data_dir / f"{uid}.jpg"
        meta_path = meta_dir / f"{uid}.json"

        try:
            src_size = src.stat().st_size
            width, height, sha = optimize_to_jpeg(src, dest_jpg, args.quality, compress=args.compress)
            dst_size = dest_jpg.stat().st_size
        except ImportError as exc:
            entity_list = ", ".join(
                f"{r.entity}({recognizer.alias.get_display_name(r.entity)})" for r in all_entities
            )
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_RECOGNIZED)
            print(f"[!] {src.name}\n")
            print(f"  error    : {exc}\n")
            print(f"  entities : {entity_list}\n")
            print(f"  ✓ archive   {dest.relative_to(tools_dir)} (未写入 data/meta)")
            recognized += 1
            print()
            continue

        if sha in existing_hashes:
            dest_jpg.unlink(missing_ok=True)
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_DUPLICATE)
            print(f"[D] {src.name}\n")
            print(f"  duplicate : {existing_hashes[sha]}\n")
            print(f"  ✓ archive   {dest.relative_to(tools_dir)}")
            print()
            duplicates += 1
            continue
        existing_hashes[sha] = dest_jpg.relative_to(repo_root).as_posix()

        entity_ids = [r.entity for r in all_entities]
        sources = lookup.sources_for(entity_ids)
        write_meta(meta_path, uid=uid, sources=sources, entities=entity_ids, sha=sha, width=width, height=height)

        dest = archive_source(src, tools_dir, input_dir, PROCESSED_RECOGNIZED)
        # ---- 格式化输出 ----
        lines = [f"[O] {src.name}", ""]
        indent = "  "

        # entities 行（单/多实体统一）
        entity_items = []
        for r in all_entities:
            display = recognizer.alias.get_display_name(r.entity)
            entity_items.append(f"{r.entity} ({display}) [{r.confidence:.2f}]")
        lines.append(f"{indent}entities : {entity_items[0]}")
        for item in entity_items[1:]:
            lines.append(f"{indent}           {item}")

        # scores 行
        if all_entities:
            scores = all_entities[0].evidence_summary
            if scores:
                lines.append(f"")
                lines.append(f"{indent}scores   : {scores}")

        # compress 行
        if args.compress and src_size > 0:
            pct = (1 - dst_size / src_size) * 100
            sign = "+" if dst_size > src_size else "-"
            lines.append(f"")
            lines.append(f"{indent}compress : {src_size/1024:.0f}KB → {dst_size/1024:.0f}KB ({sign}{abs(pct):.0f}%)")

        # ✓ meta / archive
        lines.append(f"")
        lines.append(f"{indent}✓ meta      meta/{uid}.json")
        lines.append(f"{indent}✓ archive   {dest.relative_to(tools_dir)}")

        print("\n".join(lines))
        recognized += 1
        print()

    print(f"\n── 汇总 ──")
    print(f"  recognized     : {recognized}")
    print(f"  pending_review : {pending_review}")
    print(f"  unrecognized   : {unrecognized}")
    print(f"  duplicate      : {duplicates}")
    if auto_created_entities:
        print(f"\n── 自动创建实体 ──")
        for entity_id in sorted(auto_created_entities):
            entity_path = entities_dir / f"{entity_id}.json"
            print(f"  [NEW] {entity_path}")
        print(f"\n  请编辑这些文件填写 display_name、aliases 并修正 sources 字段。")
        print(f"  完成后运行 node scripts/build-index.js 重建索引。")

    # 将完整运行日志写入文件
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"tag-image-{timestamp}.txt"
    log_path.write_text(log_buffer.getvalue(), encoding="utf-8")
    print(f"日志已保存到 {log_path.relative_to(Path(__file__).resolve().parent)}", file=sys.stderr)
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())