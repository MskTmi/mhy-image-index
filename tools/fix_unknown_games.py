"""一次性脚本：把 entities/ 和 meta/ 中 games=["unknown"] 的条目按 danbooru tag 映射修复。

用法:
    .venv\Scripts\python.exe fix_unknown_games.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entities_db import parse_danbooru_tag, load_entity_db

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTITIES_DIR = REPO_ROOT / "entities"
META_DIR = REPO_ROOT / "meta"


def fix_entities() -> int:
    fixed = 0
    for fp in sorted(ENTITIES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        games = data.get("games", [])
        if games != ["unknown"]:
            continue

        # 尝试从 entity id 推导 games（id 可能含 danbooru 游戏后缀）
        new_games = set()
        _, parsed_games = parse_danbooru_tag(data.get("id", ""))
        for g in parsed_games:
            if g != "unknown":
                new_games.add(g)

        if not new_games:
            print(f"  [skip] {fp.name}: 无法从 id 推导游戏")
            continue

        data["games"] = sorted(new_games)
        fp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  [fix] {fp.name}: games={data['games']}")
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

        games = data.get("games", [])
        if games != ["unknown"]:
            continue

        entities = data.get("entities", [])
        if not entities:
            continue

        entity_games = lookup.games_for(entities)
        if not entity_games or entity_games == ["unknown"]:
            continue

        data["games"] = entity_games
        fp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  [fix] meta/{fp.name}: games={entity_games} entities={entities}")
        fixed += 1
    return fixed


def main() -> int:
    print("=== 修复 entities ===")
    ef = fix_entities()
    print(f"\n=== 修复 meta ===")
    mf = fix_metas()
    print(f"\n完成：entity 修复 {ef} 个，meta 修复 {mf} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())