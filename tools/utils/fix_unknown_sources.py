"""一次性脚本：把 entities/ 和 meta/ 中 sources=["unknown"] 的条目按 danbooru tag 映射修复。

用法:
    .venv\\Scripts\\python.exe utils\\fix_unknown_sources.py
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from entities_db import parse_danbooru_tag, load_entity_db

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTITIES_DIR = REPO_ROOT / "entities"
META_DIR = REPO_ROOT / "meta"
TOOLS_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = TOOLS_DIR / "logs"


def fix_entities() -> int:
    fixed = 0
    for fp in sorted(ENTITIES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        sources = data.get("sources", [])
        if sources != ["unknown"]:
            continue

        # 尝试从 entity id 推导 sources（id 可能含 danbooru 作品后缀）
        new_sources = set()
        _, parsed_sources = parse_danbooru_tag(data.get("id", ""))
        for s in parsed_sources:
            if s != "unknown":
                new_sources.add(s)

        if not new_sources:
            print(f"  [skip] {fp.name}: 无法从 id 推导作品")
            continue

        data["sources"] = sorted(new_sources)
        fp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  [fix] {fp.name}: sources={data['sources']}")
        fixed += 1
    return fixed


def fix_metas() -> int:
    lookup = load_entity_db(ENTITIES_DIR)
    fixed = 0
    for fp in sorted(META_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        sources = data.get("sources", [])
        if sources != ["unknown"]:
            continue

        entities = data.get("entities", [])
        if not entities:
            continue

        entity_sources = lookup.sources_for(entities)
        if not entity_sources or entity_sources == ["unknown"]:
            continue

        data["sources"] = entity_sources
        fp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  [fix] meta/{fp.name}: sources={entity_sources} entities={entities}")
        fixed += 1
    return fixed


def main() -> int:
    print("=== 修复 entities ===")
    ef = fix_entities()
    print(f"\n=== 修复 meta ===")
    mf = fix_metas()
    print(f"\n完成：entity 修复 {ef} 个，meta 修复 {mf} 个")

    # 将修复结果写入日志文件，方便事后查看。
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "fix-unknown-sources.txt"
    log_lines = [
        f"=== fix_unknown_sources 完成 ===",
        f"时间: {datetime.now(timezone.utc).isoformat()}",
        f"Entity 修复: {ef} 个",
        f"Meta 修复: {mf} 个",
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())