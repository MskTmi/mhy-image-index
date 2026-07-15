"""统一的图片压缩入口（本地 tag_image.py 与 GitHub Issue 导入共用）。

把任意常见图片格式转 JPEG（按 EXIF 旋转，丢弃原始元数据），并用
"TinyPNG 风格"压缩：
  1. 自适应调色板量化（中位切法 + Floyd-Steinberg 抖动），减冗余颜色，
     尤其适合二次元图
  2. 关闭色度子采样（subsampling="4:4:4"），保留线条 / 文字锐度
  3. 大图降采样到 3000px 以内
  4. 量化可能轻微偏色，对写实照片可加 --no-compress 跳过量化保留原色
  5. 兜底防线：再编码后体积绝不超过原图字节，否则直接拷贝原始字节

CLI 返回 JSON 一行： {"width":..,"height":..,"sha256":..,"bytes":..}，
供 Node 子进程（scripts/optimize-image.js）解析后写入 meta。

为何不用 sharp / mozjpeg：
  - mozjpeg 不会做 256 色量化，对二次元图压缩率明显劣于本算法
  - 两边走同一份 Python 代码 + 锁定 Pillow 大版本，可保证本地 / CI
    跨源字节级一致，从而 hash 一致，避免同图跨源被入库两次
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


def optimize_to_jpeg(
    source: Path,
    dest: Path,
    quality: int = 92,
    compress: bool = False,
) -> tuple[int, int, str]:
    """把任意常见图片格式转 JPEG（按 EXIF 旋转，丢弃原始元数据）。

    当 compress=True 时启用 TinyPNG 风格的智能压缩：
      1. 对图片做自适应调色板量化（减少冗余颜色），尤其适合二次元图
      2. 关闭色度子采样（subsampling="4:4:4"），保留线条/文字锐度
      3. 量化可能轻微偏色，对写实照片会跳过量化保留原色

    返回 (width, height, sha256)，元数据从输出文件重新读取以保证 meta 里写的是
    压缩后文件的真实尺寸与哈希。
    """
    from PIL import Image, ImageOps  # 延迟导入，避免只是 --help 也要装 Pillow

    dest.parent.mkdir(parents=True, exist_ok=True)

    # 兜底防线：再编码后体积绝不能超过原图字节
    src_size = source.stat().st_size

    with Image.open(source) as img:
        # 按 EXIF orientation 旋转像素后再丢弃 EXIF，与 sharp 的 .rotate() 行为对齐
        # —— 必须做这一步，否则手机拍 EXIF orientation!=0 的图两边 hash 会差
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        if compress:
            max_dim = max(img.size)
            if max_dim > 3000:
                ratio = 3000 / max_dim
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 先保存一份不带量化/子采样的基准 JPEG，作为回退参照
            with NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                baseline_path = tmp.name
            try:
                img.save(baseline_path, format="JPEG", quality=quality, optimize=True)
                baseline_size = Path(baseline_path).stat().st_size

                # 自适应调色板量化：用中位切法把颜色压缩到 256 色
                # 然后转回 RGB 保存 JPEG，Huffman 表优化 + 无子采样
                try:
                    quantized = img.quantize(
                        colors=256,
                        method=Image.Quantize.MEDIANCUT,
                        dither=Image.Dither.FLOYDSTEINBERG,
                    )
                    quantized = quantized.convert("RGB")
                except (ValueError, OSError):
                    quantized = img  # 量化失败，回退原图

                quantized.save(
                    dest,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    subsampling="4:4:4",
                )
                if dest.stat().st_size > baseline_size:
                    # 压缩后反而变大，用基准 JPEG 覆盖
                    shutil.copy2(baseline_path, str(dest))
            finally:
                Path(baseline_path).unlink(missing_ok=True)
        else:
            img.save(dest, format="JPEG", quality=quality, optimize=True)

    if dest.stat().st_size > src_size:
        # 重编码后体积膨胀，直接拷贝原始字节（对已充分压缩的 JPEG/WebP 尤其适用）
        shutil.copy2(source, dest)

    with Image.open(dest) as out:
        width, height = out.size

    with open(dest, "rb") as fp:
        digest = hashlib.sha256(fp.read()).hexdigest()

    return width, height, digest


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统一图片压缩 CLI（本地 tag_image.py 与 Issue 导入共用）"
    )
    parser.add_argument("--src", required=True, help="输入图片路径")
    parser.add_argument("--dst", required=True, help="输出 JPEG 路径")
    parser.add_argument("--quality", type=int, default=92, help="JPEG 质量 1-100，默认 92")
    parser.add_argument(
        "--compress",
        dest="compress",
        action="store_true",
        help="启用智能压缩（量化 + 4:4:4 + 降采样 + 兜底）",
    )
    parser.add_argument(
        "--no-compress",
        dest="compress",
        action="store_false",
        help="禁用智能压缩，直接保存 JPEG",
    )
    parser.set_defaults(compress=False)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not (1 <= args.quality <= 100):
        print("error: --quality must be in 1-100", file=sys.stderr)
        return 2
    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()
    if not src.is_file():
        print(f"error: source not found: {src}", file=sys.stderr)
        return 2

    try:
        width, height, sha = optimize_to_jpeg(src, dst, quality=args.quality, compress=args.compress)
    except Exception as exc:  # noqa: BLE001 —— 给 Node 子进程一个明确退出码
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Node 一行 JSON 解析：只输出一行 stdout
    payload = {
        "width": width,
        "height": height,
        "sha256": sha,
        "bytes": dst.stat().st_size,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())