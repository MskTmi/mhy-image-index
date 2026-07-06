"""加载仓库 entities/*.json，构建 WD14 覆盖层使用的映射表。

danbooru character tag → entity 的映射规则：
  1. 从 danbooru_tag_map.json 加载 tag_overrides（手动覆盖，用于无法推导的 case）
  2. 从 entity 的 id + games 自动生成候选 tag（按 game_suffixes 拼后缀）
  3. entity JSON 文件不再需要 danbooru_tags 字段

display_name / aliases 是中文，没法直接和英文的 WD14 character tag 对齐，
所以依靠 id（英文名）和 games（游戏归属）自动推导。

工具本身与游戏无关 —— 任何在 entities/ 里登记的角色都可以被这套流程识别；
游戏归属完全由 entity 的 games 字段决定。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

_TOOLS_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _TOOLS_DIR / "danbooru_tag_map.json"

# danbooru character tag 的游戏后缀 → 仓库 games 标识 映射表。
# WD14 输出的 tag 形如 furina_(genshin_impact)、elysia_(honkai_impact)，
# 括号内就是 danbooru 的 copyright/category 后缀，按此表映射到我们的 game id。
DANBOORU_GAME_MAP: Dict[str, str] = {
    "genshin_impact": "genshin",
    "honkai_impact": "bh3",
    "honkai_impact_3rd": "bh3",
    "honkai_star_rail": "hsr",
    "zenless_zone_zero": "zzz",
}

# 匹配 danbooru character tag 的 "角色名_(游戏)" 后缀模式
_TAG_SUFFIX_RE = re.compile(r"^(.+?)_\(([^()]+)\)$")

# camelCase / PascalCase → snake_case 转换
_CAMEL_TO_SNAKE_RE = re.compile(r"(?<=[a-z0-9])([A-Z])|(?<=[A-Z])([A-Z][a-z])")


def _camel_to_snake(name: str) -> str:
    """FuHua → fu_hua, March7th → march7th, RaidenShogun → raiden_shogun"""
    result = _CAMEL_TO_SNAKE_RE.sub(r"_\1\2", name).lower()
    # 合并连续下划线
    return re.sub(r"_+", "_", result)


def _load_tag_config() -> dict:
    """加载 danbooru_tag_map.json 配置。"""
    if not _CONFIG_PATH.is_file():
        return {"game_suffixes": {}, "tag_overrides": {}}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_danbooru_tag(tag: str) -> Tuple[str, List[str]]:
    """把 danbooru character tag 拆成 (角色 id, [games])。

    "furina_(genshin_impact)" → ("furina", ["genshin"])
    "kiana_kaslana"          → ("kiana_kaslana", ["unknown"])
    """
    tag = tag.strip()
    m = _TAG_SUFFIX_RE.match(tag)
    if not m:
        return tag, ["unknown"]

    character = m.group(1)
    suffix = m.group(2).lower().replace(" ", "_")
    game = DANBOORU_GAME_MAP.get(suffix)
    if game:
        return character, [game]
    else:
        # 有后缀但不在我们映射表里（比如 azur_lane、vocaloid 等），整 tag 作 id
        return tag, ["unknown"]


def _generate_candidate_tags(entity_id: str, games: List[str], game_suffixes: Dict[str, List[str]]) -> List[str]:
    """从 entity 的 id + games 自动生成所有可能的 danbooru character tag。

    对 id 的每个变体（原样小写 + snake_case），与每个 game 的后缀组合：
      Furina + genshin → furina, furina_(genshin_impact)
      FuHua  + bh3    → fuhua, fu_hua, fuhua_(honkai_impact_3rd), fu_hua_(honkai_impact), ...
    """
    names: List[str] = [entity_id.lower()]
    snake = _camel_to_snake(entity_id)
    if snake != names[0]:
        names.append(snake)

    tags: List[str] = list(names)  # 裸 tag 直接加入

    for game in games:
        suffixes = game_suffixes.get(game, [])
        for suffix in suffixes:
            for name in names:
                tags.append(f"{name}_({suffix})")

    return sorted(set(tags))


class EntityLookup:
    """以 danbooru character tag 为键的实体查找表。"""

    def __init__(self) -> None:
        # danbooru tag -> 该 tag 命中的全部 canonical id（一个 tag 可能指向多个角色）
        self.tag_to_ids: Dict[str, List[str]] = {}
        # canonical id -> games 列表（写 meta 的 games 字段时取并集用）
        self.id_to_games: Dict[str, List[str]] = {}
        # canonical id -> display_name，仅用于日志更可读
        self.id_to_name: Dict[str, str] = {}

    @property
    def is_empty(self) -> bool:
        return not self.tag_to_ids

    def add(self, entity_id: str, display_name: str, games: List[str], tags: List[str]) -> None:
        """注册一个 entity 及其对应的 danbooru tag 列表。"""
        self.id_to_games[entity_id] = sorted(set(games))
        self.id_to_name[entity_id] = display_name or entity_id

        for tag in tags:
            normalized = tag.strip().lower()
            if not normalized:
                continue

            bucket = self.tag_to_ids.setdefault(normalized, [])
            if entity_id not in bucket:
                bucket.append(entity_id)

    def add_auto(self, danbooru_tag: str, entities_dir: Path) -> str:
        """为未登记的 danbooru character tag 自动创建占位 entity 文件。

        利用 danbooru tag 的「角色名_(游戏)」命名规则自动提取 id 和 games：
          furina_(genshin_impact) → id="furina"  games=["genshin"]
          kiana_kaslana           → id="kiana_kaslana"  games=["unknown"]

        display_name / aliases 留空待人工填写。
        不再写入 danbooru_tags 字段（映射由代码自动生成）。
        返回新 entity 的 canonical id。
        """
        entity_id, games = parse_danbooru_tag(danbooru_tag)
        if not entity_id:
            return ""

        config = _load_tag_config()
        game_suffixes = config.get("game_suffixes", {})

        entity_path = entities_dir / f"{entity_id}.json"
        if entity_path.exists():
            tags = _generate_candidate_tags(entity_id, games, game_suffixes)
            self.add(entity_id, "", games, tags)
            return entity_id

        stub = {
            "id": entity_id,
            "display_name": "",
            "games": games,
            "aliases": [],
        }
        entity_path.write_text(json.dumps(stub, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tags = _generate_candidate_tags(entity_id, games, game_suffixes)
        self.add(entity_id, "", games, tags)
        return entity_id

    def resolve(self, character_tags: List[str]) -> Tuple[List[str], List[str]]:
        """把 WD14 返回的 character tag 列表解析成 canonical id 集合。

        返回 (matched_ids, unmatched_tags)：未命中/未登记 tag 会带回给上层，
        便于人工审计一下「这一帧到底没识别到谁」。
        """

        matched: List[str] = []
        matched_set = set()
        unmatched: List[str] = []

        for tag in character_tags:
            normalized = tag.strip().lower()
            if not normalized:
                continue

            ids = self.tag_to_ids.get(normalized)
            if not ids:
                unmatched.append(tag)
                continue

            for entity_id in ids:
                if entity_id in matched_set:
                    continue
                matched.append(entity_id)
                matched_set.add(entity_id)

        return sorted(matched), unmatched

    def games_for(self, entity_ids: List[str]) -> List[str]:
        """把若干角色的 games 字段去重排序后返回，供 meta.games 使用。"""

        collected: List[str] = []
        seen = set()

        for entity_id in entity_ids:
            for game in self.id_to_games.get(entity_id, []):
                if game not in seen:
                    collected.append(game)
                    seen.add(game)

        return sorted(collected)


def load_entity_db(entities_dir: Path) -> EntityLookup:
    """读取 entities/*.json 并构建 EntityLookup。

    danbooru tag 映射来源（优先级从高到低）：
      1. danbooru_tag_map.json 中的 tag_overrides（手动覆盖）
      2. 从 entity 的 id + games 自动生成的候选 tag（基于 game_suffixes）
    """

    config = _load_tag_config()
    game_suffixes: Dict[str, List[str]] = config.get("game_suffixes", {})
    tag_overrides: Dict[str, str] = config.get("tag_overrides", {})

    lookup = EntityLookup()
    if not entities_dir.is_dir():
        return lookup

    for file_path in sorted(entities_dir.glob("*.json")):
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{file_path.name}: 不是合法的 JSON ({exc.msg})") from exc

        if not isinstance(raw, dict) or "id" not in raw:
            raise ValueError(f"{file_path.name}: 缺少 id 字段")

        entity_id = str(raw["id"]).strip()
        if not entity_id:
            raise ValueError(f"{file_path.name}: id 不能为空")

        display_name = str(raw.get("display_name") or "").strip()
        games_raw = raw.get("games") or []
        if not isinstance(games_raw, list):
            raise ValueError(f"{file_path.name}: games 必须是数组")

        games = [str(g).strip() for g in games_raw if str(g).strip()]

        # 从 id + games 自动生成 danbooru tag 候选
        tags = _generate_candidate_tags(entity_id, games, game_suffixes)
        lookup.add(entity_id, display_name, games, tags)

    # 叠加手动覆盖映射（用于 id+games 无法推导的特殊 tag）
    for tag, entity_id in tag_overrides.items():
        normalized = tag.strip().lower()
        if not normalized:
            continue
        bucket = lookup.tag_to_ids.setdefault(normalized, [])
        if entity_id not in bucket:
            bucket.append(entity_id)

    return lookup