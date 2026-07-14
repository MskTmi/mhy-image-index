"""将人工分类的图片批量导入 data/ + meta/。

默认扫描 tools/manual/ 目录，也支持 --input 指定其他目录。

用法:
    python tools/process_tmp.py [--input <分类图片目录>] [--repo <仓库根目录>]
        [--quality 92] [--dry-run]

手动分类流程：
    1. 在 manual/ 下创建以角色名命名的文件夹（如 manual/流萤/）
    2. 把对应图片放进去
    3. 确保 entities/ 中有对应实体（display_name 或 id 匹配文件夹名）
    4. 运行 python tools/process_tmp.py（建议先 --dry-run 预览）

文件夹名按实体 display_name 或 id 匹配，支持多角色（× 或空格分隔）。
图片优化为 JPEG 写入 data/，meta 写入 meta/。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import string
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleCP(65001)
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ID_ALPHABET = string.ascii_lowercase + string.digits
ID_LENGTH = 8

_TOOLS_DIR = Path(__file__).resolve().parent
LOGS_DIR = _TOOLS_DIR / "logs"


def load_entities(entities_dir: Path) -> Tuple[Dict[str, dict], Dict[str, str], Dict[str, str]]:
    """加载所有实体文件，返回 (id→entry, display_name→id, id_lower→id)。"""
    entities: Dict[str, dict] = {}
    dn_to_id: Dict[str, str] = {}
    id_lower: Dict[str, str] = {}

    for fp in sorted(entities_dir.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"  [WARN] 跳过无效 entity 文件: {fp.name}")
            continue
        eid = (data.get("id") or "").strip()
        if not eid:
            continue
        entry = {
            "display_name": (data.get("display_name") or "").strip(),
            "sources": data.get("sourceurces", []),
            "aliases": data.get("aliases", []),
        }
        entities[eid] = entry
        id_lower[eid.lower()] = eid
        dn = entry["display_name"]
        if dn:
            dn_to_id[dn] = eid
        # 别名也做映射
        for alias in entry["aliases"]:
            dn_to_id[alias] = eid

    return entities, dn_to_id, id_lower


def resolve_folder_name(folder: str, dn_to_id: Dict[str, str], id_lower: Dict[str, str]) -> List[str]:
    """将文件夹名解析为实体 ID 列表。支持 × 和空格分隔。"""
    # 先尝试直接匹配整个名字
    if folder in dn_to_id:
        return [dn_to_id[folder]]
    if folder.lower() in id_lower:
        return [id_lower[folder.lower()]]

    # 尝试 × 分隔
    if "×" in folder:
        parts = [p.strip() for p in folder.split("×") if p.strip()]
    elif "  " in folder:
        parts = [p.strip() for p in folder.split("  ") if p.strip()]
    else:
        parts = [p.strip() for p in folder.split(" ") if p.strip()]

    if len(parts) <= 1:
        # 单部分但没匹配到
        print(f"  [WARN] 无法匹配文件夹名: {folder}")
        return []

    # 先从左到右尝试匹配，不匹配的相邻部分合并再试
    ids = []
    i = 0
    while i < len(parts):
        matched = False
        # 尝试从 i 开始的 1/2/3 个连续部分的组合
        for span in range(min(3, len(parts) - i), 0, -1):
            candidate = " ".join(parts[i:i + span])
            # 尝试原始、去下划线、去空格
            for variant in [candidate, candidate.replace("_", ""), candidate.replace(" ", ""), candidate.replace("_", "").replace(" ", "")]:
                if variant in dn_to_id:
                    ids.append(dn_to_id[variant])
                    matched = True
                    break
                if variant.lower() in id_lower:
                    ids.append(id_lower[variant.lower()])
                    matched = True
                    break
            if matched:
                i += span
                break
        if not matched:
            print(f"  [WARN] 无法匹配: {folder!r} 中的 {parts[i]!r}")
            i += 1
    return ids
    return ids


def load_existing_hashes(meta_dir: Path) -> Dict[str, str]:
    """加载已有 meta 的 hash → image 映射，用于查重。"""
    hashes: Dict[str, str] = {}
    if not meta_dir.is_dir():
        return hashes
    for fp in meta_dir.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("hash"):
            hashes[str(data["hash"])] = str(data.get("image", ""))
    return hashes


def generate_id(data_dir: Path, meta_dir: Path) -> str:
    while True:
        cid = "".join(random.choices(ID_ALPHABET, k=ID_LENGTH))
        if not (data_dir / f"{cid}.jpg").exists() and not (meta_dir / f"{cid}.json").exists():
            return cid


def optimize_to_jpeg(source: Path, dest: Path, quality: int) -> Tuple[int, int, str]:
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as img:
        img = img.convert("RGB")
        img.save(dest, format="JPEG", quality=quality, optimize=True)

    with Image.open(dest) as out:
        width, height = out.size

    with open(dest, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()

    return width, height, digest


def main():
    parser = argparse.ArgumentParser(description="导入 tmp 下人工分类的图片")
    parser.add_argument("--input", type=Path, default=None,
                        help="分类图片目录（默认 manual/）")
    parser.add_argument("--repo", type=Path, default=None,
                        help="仓库根目录（默认脚本所在目录的上级）")
    parser.add_argument("--quality", type=int, default=92,
                        help="JPEG 质量 (1-100)")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览，不实际写入")
    args = parser.parse_args()

    if args.repo is None:
        args.repo = Path(__file__).resolve().parent.parent
    if args.input is None:
        args.input = args.repo / "tools" / "workspace" / "manual"

    repo = args.repo
    input_dir = args.input
    data_dir = repo / "data"
    meta_dir = repo / "meta"
    entities_dir = repo / "entities"

    input_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    # 加载实体
    entities, dn_to_id, id_lower = load_entities(entities_dir)
    print(f"已加载 {len(entities)} 个实体")

    # 加载已有哈希
    existing_hashes = load_existing_hashes(meta_dir)
    print(f"已加载 {len(existing_hashes)} 条已有图片哈希")

    # 扫描 input 下所有图片
    image_files = []
    for subdir in sorted(input_dir.iterdir()):
        if not subdir.is_dir():
            continue
        folder_name = subdir.name
        for img_file in sorted(subdir.iterdir()):
            if img_file.is_file() and img_file.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                image_files.append((folder_name, img_file))

    print(f"找到 {len(image_files)} 张图片待处理\n")

    # 统计
    success = 0
    skipped_dup = 0
    skipped_nomatch = 0
    errors = 0

    for folder_name, img_path in image_files:
        # 解析实体
        entity_ids = resolve_folder_name(folder_name, dn_to_id, id_lower)
        if not entity_ids:
            print(f"  [SKIP] {img_path.name} ← 文件夹 {folder_name!r} 无法匹配实体")
            skipped_nomatch += 1
            continue

        # 查重（用文件哈希快速预检）
        with open(img_path, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()

        if file_hash in existing_hashes:
            print(f"  [DUP]  {img_path.name} ← 与 {existing_hashes[file_hash]} 重复")
            skipped_dup += 1
            continue

        # 生成 ID
        cid = generate_id(data_dir, meta_dir)

        # 收集 sources
        sources_set = set()
        for eid in entity_ids:
            if eid in entities:
                sources_set.update(entities[eid]["sources"])
        sources = sorted(sources_set) if sources_set else ["unknown"]

        dest_path = data_dir / f"{cid}.jpg"

        if args.dry_run:
            print(f"  [DRY]  {img_path.name} → {cid}.jpg  entities={entity_ids}  sources={sources}")
            success += 1
            continue

        try:
            width, height, digest = optimize_to_jpeg(img_path, dest_path, args.quality)
        except Exception as e:
            print(f"  [ERR]  {img_path.name}: 图片处理失败 - {e}")
            errors += 1
            continue

        # 写入 meta
        meta = {
            "id": cid,
            "image": f"data/{cid}.jpg",
            "hash": digest,
            "width": width,
            "height": height,
            "sources": sources,
            "entities": sorted(entity_ids),
        }
        meta_path = meta_dir / f"{cid}.json"
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # 记录哈希防止后续重复
        existing_hashes[digest] = f"data/{cid}.jpg"

        print(f"  [OK]   {img_path.name} → {cid}.jpg  entities={entity_ids}")
        success += 1

    print(f"\n--- 处理完成 ---")
    print(f"  成功: {success}")
    print(f"  跳过(重复): {skipped_dup}")
    print(f"  跳过(无法匹配): {skipped_nomatch}")
    print(f"  错误: {errors}")

    # 将处理结果写入日志文件，方便事后查看。
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "process-tmp.txt"
    from datetime import datetime, timezone
    log_lines = [
        f"=== process_tmp 处理完成 ===",
        f"时间: {datetime.now(timezone.utc).isoformat()}",
        f"输入目录: {input_dir}",
        f"成功: {success}",
        f"跳过(重复): {skipped_dup}",
        f"跳过(无法匹配): {skipped_nomatch}",
        f"错误: {errors}",
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    # 构建验证
    if not args.dry_run and success > 0:
        import subprocess
        print("\n正在运行 build-index.js ...")
        result = subprocess.run(
            ["node", "scripts/build-index.js"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("构建验证通过 ✅")
        else:
            print(f"构建失败:\n{result.stderr}")


if __name__ == "__main__":
    main()
