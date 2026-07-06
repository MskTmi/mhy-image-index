"""基于 imgutils / WD14 Tagger 的图片批量识别 + 分类工具。

用法:
    python tools/tag_image.py [--input <待分类图片目录>] [--repo <仓库根目录>]
        [--character-threshold 0.7] [--general-threshold 0.35]
        [--model SwinV2_v3] [--quality 92]

默认在 tools 下维护两个固定文件夹构成工作流：
    tools/inbox/        待处理：把要识别的图丢进来即可
    tools/processed/   处理后归档，按结果分四个子目录：
        recognized/     已成功写入 data/ 与 meta/ 的源图
        pending_review/ WD 检测到角色但 entity 未登记，保留供人工筛选
        unrecognized/   WD 未检测到任何角色 tag，留给人工分类
        duplicate/      与仓库已有图重复、已跳过的源图

跑完一轮后 inbox 会被清空，源图全部移入 processed/ 的对应子目录；
命名冲突会自动追加后缀 `_1`、`_2`，不会覆盖既有文件。

工具是游戏无关的：只要是 entities/ 里登记了 danbooru_tags 的角色都会被识别，
游戏归属直接取自 entity 自身的 games 字段。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import random
import string
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
os.environ.setdefault("HF_HOME", str(_DEFAULT_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_DEFAULT_HF_HOME / "hub"))
os.environ.setdefault("HF_XET_CACHE", str(_DEFAULT_HF_HOME / "xet"))
# hf-xet 后端通过 HTTP 代理做 HEAD 请求时容易拿到上游 308/空响应，
# 进而触发 huggingface_hub FileMetadataError；默认禁用回传统 requests 路径。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 自动发现 nvidia pip 包的 DLL 路径（nvidia-cublas-cu12 等），加到 PATH
# 使 onnxruntime-gpu 能加载 CUDA 运行时，无需安装完整 CUDA Toolkit。
def _setup_cuda_dll_path() -> None:
    import sys as _sys
    try:
        import nvidia  # noqa: F401
        _nv_base = Path(_sys.prefix) / "Lib" / "site-packages" / "nvidia"
        _bin_dirs = sorted(set(
            str(p.parent) for p in _nv_base.rglob("bin") if p.is_dir()
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

# imgutils 默认通过 hf_hub_download 从 HuggingFace 拉模型。当本机走 HTTP 代理时，
# huggingface_hub 1.22 做 HEAD 元数据请求会因代理丢失 X-Repo-Commit 头而失败。
# 我们优先检测 tools/.hf-cache/<model>/ 下是否有 download_models.py 抓下来的本地
# 模型文件，若有就 monkey-patch 掉 imgutils 三个内部加载函数让它们直接读本地路径，
# 完全跳过 hf_hub。本地目录不存在或文件不全时回落到原始 hf_hub 路径（要求网络通）。
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
INBOX_DIR_NAME = "inbox"
PROCESSED_DIR_NAME = "processed"
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
        help="待分类的图片所在目录（递归扫描），默认 tools/inbox；"
        "处理后源图会被移动到 tools/processed/ 下归档。",
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


def optimize_to_jpeg(source: Path, dest: Path, quality: int) -> tuple[int, int, str]:
    """把任意常见图片格式转 JPEG（按 EXIF 旋转，丢弃原始元数据）。

    返回 (width, height, sha256)，元数据从输出文件重新读取以保证 meta 里写的是
    压缩后文件的真实尺寸与哈希。
    """
    from PIL import Image  # 延迟导入，避免只是 --help 也要装 Pillow

    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as img:
        # EXIF 自动旋转，再去掉 alpha 通道（JPEG 不带 alpha）
        img = img.convert("RGB")
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


def write_meta(meta_path: Path, *, uid: str, games: List[str], entities: List[str], sha: str, width: int, height: int) -> None:
    meta = {
        "id": uid,
        "image": f"data/{uid}.jpg",
        "hash": sha,
        "width": width,
        "height": height,
        "games": games,
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


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not (1 <= args.quality <= 100):
        print("错误：--quality 必须在 1-100 之间。", file=sys.stderr)
        return 2

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
            "警告：entities/ 中没有任何声明 danbooru_tags 的角色，所有图都会归档到 processed/unrecognized/。",
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

    recognized = 0
    unrecognized = 0
    duplicates = 0
    pending_review = 0
    auto_created_entities: Dict[str, str] = {}  # entity_id -> danbooru tag，用于最终汇总

    for src in images:
        try:
            chars = tag_image(src, args.model, args.general_threshold, args.character_threshold)
        except Exception as exc:  # 模型跑挂了的图原样归档进 unrecognized 由人工处理
            print(f"[X] {src.name} 标注失败：{exc}")
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
            print(f"    -> 已归档到 {dest.relative_to(tools_dir)}")
            unrecognized += 1
            continue

        if not chars:
            print(f"[?] {src.name} 未识别到任何角色 tag")
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_UNRECOGNIZED)
            print(f"    -> 已归档到 {dest.relative_to(tools_dir)}")
            unrecognized += 1
            continue

        matched, unmatched = lookup.resolve(list(chars.keys()))

        # 有未被登记的 tag？自动创建占位 entity，再重新解析一次
        if unmatched:
            new_ids = auto_create_entities(unmatched, lookup, entities_dir)
            for eid in new_ids:
                auto_created_entities[eid] = eid  # id 自身就是 danbooru tag
            if new_ids:
                # 重新解析，新 entity 应该能命中 unmatched 中的 tag
                matched, _ = lookup.resolve(list(chars.keys()))

        if not matched:
            tags_str = ", ".join(f"{t}={chars[t]:.2f}" for t in sorted(chars, key=lambda t: chars[t], reverse=True)[:10])
            print(f"[~] {src.name} 有角色 tag 但未命中任何 entity，保留待人工审核")
            print(f"    tags: {tags_str}")
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_PENDING)
            print(f"    -> 已归档到 {dest.relative_to(tools_dir)}")
            pending_review += 1
            continue

        uid = generate_id(data_dir, meta_dir)
        dest_jpg = data_dir / f"{uid}.jpg"
        meta_path = meta_dir / f"{uid}.json"

        width, height, sha = optimize_to_jpeg(src, dest_jpg, args.quality)

        if sha in existing_hashes:
            # 重复图：撤回刚写的 jpg，不污染仓库，源图归档到 duplicate
            dest_jpg.unlink(missing_ok=True)
            dest = archive_source(src, tools_dir, input_dir, PROCESSED_DUPLICATE)
            print(f"[D] {src.name} 与已有图重复（{existing_hashes[sha]}）-> {dest.relative_to(tools_dir)}")
            duplicates += 1
            continue
        existing_hashes[sha] = dest_jpg.relative_to(repo_root).as_posix()

        games = lookup.games_for(matched)
        write_meta(meta_path, uid=uid, games=games, entities=matched, sha=sha, width=width, height=height)

        dest = archive_source(src, tools_dir, input_dir, PROCESSED_RECOGNIZED)
        tags_str = ", ".join(f"{t}={chars[t]:.2f}" for t in chars)
        print(f"[O] {src.name} -> meta/{uid}.json | entity={matched} games={games} | {tags_str}")
        print(f"    -> 源图已归档到 {dest.relative_to(tools_dir)}")
        recognized += 1

    print(
        f"\n汇总：识别 {recognized}，待审核 {pending_review}，未识别 {unrecognized}，重复跳过 {duplicates}。"
        f"源图归档目录：{processed_root.relative_to(tools_dir)}/"
    )
    if auto_created_entities:
        print(f"\n自动创建了 {len(auto_created_entities)} 个占位实体（display_name / aliases 待填写）：")
        for entity_id in sorted(auto_created_entities):
            entity_path = entities_dir / f"{entity_id}.json"
            print(f"  [NEW] {entity_path}")
        print(
            "\n请编辑这些文件填写 display_name（中文名）、aliases（别名）并修正 games 字段。\n"
            "完成后运行 build-index.js 重建索引即可。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())