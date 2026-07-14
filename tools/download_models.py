"""下载 WD14 Tagger 模型文件到 tools/.hf-cache 本地缓存目录。

用法:
    .venv\Scripts\python.exe tools\download_models.py [--model SwinV2_v3]

下载后会落地到：
    tools/.hf-cache/wd14-swinv2-v3/
        model.onnx
        inv.npz
        selected_tags.csv
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

# imgutils 内部 model_name -> (model 子仓库路径, 主仓库路径)
# 见 imgutils/tagging/wd14.py 的 MODEL_NAMES 表。
# ★ 推荐：EVA02_Large（精度最高）和 SwinV2_v3（速度快）
SUPPORTED_MODELS = {
    # -- v3 系（推荐）--
    "EVA02_Large": "SmilingWolf/wd-eva02-large-tagger-v3",
    "SwinV2_v3":   "SmilingWolf/wd-swinv2-tagger-v3",
    "ConvNext_v3": "SmilingWolf/wd-convnext-tagger-v3",
    "ViT_v3":      "SmilingWolf/wd-vit-tagger-v3",
    "PixAI_v09":   "deepghs/pixai-tagger-v0.9-onnx",
    # -- v2 系（旧版，保留兼容）--
    "SwinV2":      "SmilingWolf/wd-v1-4-swinv2-tagger-v2",
    "ConvNext":    "SmilingWolf/wd-v1-4-convnext-tagger-v2",
    "ConvNextV2":  "SmilingWolf/wd-v1-4-convnextv2-tagger-v2",
    "ViT":         "SmilingWolf/wd-v1-4-vit-tagger-v2",
    "MOAT":        "SmilingWolf/wd-v1-4-moat-tagger-v2",
}

HF_ENDPOINT = "https://huggingface.co"
# deepghs 的聚合仓库：里面按 model 子目录放了 model.onnx 与 inv.npz
DEEPGHS_REPO = "deepghs/wd14_tagger_with_embeddings"

DEFAULT_MODEL = "EVA02_Large"

_TOOLS_DIR = Path(__file__).resolve().parent
CACHE_ROOT = _TOOLS_DIR / ".hf-cache"
LOGS_DIR = _TOOLS_DIR / "logs"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="流式下载 WD14 模型到本地缓存目录。")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(SUPPORTED_MODELS),
                        help=f"WD14 模型名，默认 {DEFAULT_MODEL}")
    parser.add_argument("--cache-root", default=str(CACHE_ROOT),
                        help="本地缓存根目录，默认 tools/.hf-cache")
    parser.add_argument("--force", action="store_true", help="即使文件已存在也重新下载")
    return parser.parse_args(argv)


def build_session() -> requests.Session:
    """创建 requests session。"""
    return requests.Session()


def stream_download(session: requests.Session, url: str, dest: Path, force: bool,
                    max_retries: int = 8, chunk_size: int = 1024 * 512) -> None:
    """支持断点续传的流式下载，中断会自动 retry 续传剩余字节。

    HuggingFace CDN 对 Range 请求可能返回 200（忽略 Range，从头传完整文件）
    而不是 206。代码必须区分两种响应：返回 206 才追加，返回 200 必须丢掉旧 .part
    从 0 覆盖，否则两段拼接会得到损坏文件。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        size_mb = dest.stat().st_size / 1024 / 1024
        print(f"  [skip] {dest.name} 已存在 ({size_mb:.1f} MB)，加 --force 可覆盖")
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  [GET] {url}")

    for attempt in range(1, max_retries + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}

        try:
            with session.get(url, stream=True, timeout=(30, 120),
                             allow_redirects=True, headers=headers) as resp:
                if resp.status_code == 206:
                    # 服务器接受续传，从 have 字节追加
                    mode = "ab"
                    content_remaining = int(resp.headers.get("Content-Length", 0))
                    total = have + content_remaining
                elif resp.status_code == 200:
                    # 服务器忽略 Range，从头传整文件，必须丢弃旧 .part
                    mode = "wb"
                    have = 0
                    total = int(resp.headers.get("Content-Length", 0))
                else:
                    resp.raise_for_status()
                    continue

                downloaded = have
                last_report = downloaded
                with open(tmp, mode) as fp:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        fp.write(chunk)
                        downloaded += len(chunk)
                        if total and downloaded - last_report >= max(total * 0.10, 50 * 1024 * 1024):
                            print(f"    {downloaded / 1024 / 1024:7.1f}/{total / 1024 / 1024:.1f} MB")
                            last_report = downloaded
                if total and downloaded != total:
                    raise IOError(f"{dest.name} 大小不对：{downloaded} != {total}")
                tmp.replace(dest)
                print(f"  [ok ] {dest.name} 完成 ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
                return
        except Exception as exc:
            print(f"  [retry {attempt}/{max_retries}] {dest.name} 中断：{exc}")
            if attempt == max_retries:
                raise
    raise IOError(f"{dest.name} 多次重试仍失败")


def main(argv=None) -> int:
    args = parse_args(argv)
    cache_root = Path(args.cache_root)
    cache_dir = cache_root / args.model.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)

    # deepghs 聚合仓库 (deepghs/wd14_tagger_with_embeddings) 额外存了每个模型的 inv.npz
    # （imgutils 内部用作 embedding 去规范化的权重矩阵），路径在 {sub_repo}/inv.npz 下。
    sub_repo = SUPPORTED_MODELS[args.model]
    is_v3 = args.model.endswith("_v3")
    required_files = [
        (f"{HF_ENDPOINT}/{sub_repo}/resolve/main/model.onnx",
         cache_dir / "model.onnx"),
        (f"{HF_ENDPOINT}/{sub_repo}/resolve/main/selected_tags.csv",
         cache_dir / "selected_tags.csv"),
    ]
    # inv.npz 只在 deepghs 聚合仓库里，v3 在 imgutils 0.19 上未支持推理，也跳过这个权重
    optional_files = [] if is_v3 else [
        (f"{HF_ENDPOINT}/{DEEPGHS_REPO}/resolve/main/{sub_repo}/inv.npz",
         cache_dir / "inv.npz"),
    ]
    files = required_files + optional_files

    print(f"模型 {args.model} 下载到：{cache_dir}")
    session = build_session()
    for url, dest in files:
        try:
            stream_download(session, url, dest, args.force)
        except Exception as exc:
            print(f"  [ERR] 下载 {dest.name} 失败：{exc}", file=sys.stderr)
            return 1

    # 写入一个 manifest，方便 tag_image.py 校验目录里有什么
    file_list = "model.onnx,selected_tags.csv"
    if not is_v3:
        file_list += ",inv.npz"
    manifest = cache_dir / "MANIFEST.txt"
    manifest.write_text(
        f"model={args.model}\n"
        f"source_repo={sub_repo}\n"
        f"files={file_list}\n",
        encoding="utf-8",
    )

    print(f"\n完成。tag_image.py 启动时会自动从 {cache_dir} 读取这些文件。")

    # 将下载结果写入日志文件，方便事后查看。
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "download-models.txt"
    from datetime import datetime, timezone
    log_lines = [
        f"=== 模型下载完成 ===",
        f"时间: {datetime.now(timezone.utc).isoformat()}",
        f"模型: {args.model}",
        f"源仓库: {sub_repo}",
        f"缓存目录: {cache_dir}",
        f"文件: {file_list}",
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())