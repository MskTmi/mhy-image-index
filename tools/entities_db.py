"""加载仓库 entities/*.json，构建 WD14 覆盖层使用的映射表。

danbooru character tag → entity 的映射规则：
  1. 从 danbooru_tag_map.json 加载 tag_overrides（手动覆盖，用于无法推导的 case）
  2. 从 entity 的 id + sources 自动生成候选 tag（按 source_suffixes 拼后缀）

display_name / aliases 通常是中文，没法直接和英文的 WD14 character tag 对齐，
所以依靠 id（英文名）和 sources（作品归属）自动推导。

工具本身与任何具体作品（游戏 / 动漫 / 影视 / VTuber / 舰船 …）无关 —— 任何在 entities/
里登记的角色都可以被这套流程识别；作品归属完全由 entity 的 sources 字段决定，danbooru
copyright 后缀与内部 source id 之间的映射由 tools/danbooru_tag_map.json 的 source_suffixes
字段提供。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

_TOOLS_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _TOOLS_DIR / "danbooru_tag_map.json"

# 匹配 danbooru character tag 的 "角色名_(作品)" 后缀模式
_TAG_SUFFIX_RE = re.compile(r"^(.+?)_\(([^()]+)\)$")

# camelCase / PascalCase → snake_case 转换
_CAMEL_TO_SNAKE_RE = re.compile(r"(?<=[a-z0-9])([A-Z])|(?<=[A-Z])([A-Z][a-z])")


def _camel_to_snake(name: str) -> str:
    """FuHua → fu_hua, March7th → march7th, RaidenShogun → raiden_shogun"""
    result = _CAMEL_TO_SNAKE_RE.sub(r"_\1\2", name).lower()
    # 合并连续下划线
    return re.sub(r"_+", "_", result)


def _load_tag_config() -> dict:
    """加载 danbooru_tag_map.json 配置。文件缺失时返回空结构。"""
    if not _CONFIG_PATH.is_file():
        return {"source_suffixes": {}, "tag_overrides": {}}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _suffix_to_source_map() -> Dict[str, str]:
    """从 danbooru_tag_map.json 的 source_suffixes 求逆：danbooru 后缀 → 内部 source id。

    source_suffixes 形如 {"genshin": ["genshin_impact"], "bh3": ["honkai_impact_3rd", ...]}
    求逆后得到 {"genshin_impact": "genshin", "honkai_impact_3rd": "bh3", ...}，
    供 parse_danbooru_tag 把 character tag 的括号后缀反查回内部 source id。

    空配置时返回空 dict，parse_danbooru_tag 在此情况下对所有带后缀的 tag
    都回退到 ["unknown"]，与"后缀不在映射表"走同一条未知旁路。
    """
    cfg = _load_tag_config()
    source_suffixes = cfg.get("source_suffixes", {})
    rev: Dict[str, str] = {}
    for source_id, suffixes in source_suffixes.items():
        for suf in suffixes:
            key = str(suf).strip().lower().replace(" ", "_")
            if key:
                rev[key] = str(source_id)
    return rev


def parse_danbooru_tag(tag: str) -> Tuple[str, List[str]]:
    """把 danbooru character tag 拆成 (角色 id, [sources])。

    "furina_(genshin_impact)" → ("furina", ["genshin"])   （source_suffixes 已配 "genshin": [...]）
    "kiana_kaslana"           → ("kiana_kaslana", ["unknown"])
    "hatsune_miku_(vocaloid)" → ("hatsune_miku_(vocaloid)", ["unknown"])  （后缀未登记）

    内部 source id 完全取决于用户在 danbooru_tag_map.json 里给 source_suffixes 配的 key；
    没有任何作品名是硬编码的。
    """
    tag = tag.strip()
    m = _TAG_SUFFIX_RE.match(tag)
    if not m:
        return tag, ["unknown"]

    character = m.group(1)
    suffix = m.group(2).lower().replace(" ", "_")
    source = _suffix_to_source_map().get(suffix)
    if source:
        return character, [source]
    # 有后缀但不在 source_suffixes 映射里（用户尚未为该作品配置后缀），整 tag 作 id
    return tag, ["unknown"]


def _generate_candidate_tags(entity_id: str, sources: List[str], source_suffixes: Dict[str, List[str]]) -> List[str]:
    """从 entity 的 id + sources 自动生成所有可能的 danbooru character tag。

    对 id 的每个变体（原样小写 + snake_case），与每个 source 的后缀组合：
      Furina + genshin → furina, furina_(genshin_impact)
      FuHua  + bh3    → fuhua, fu_hua, fuhua_(honkai_impact_3rd), fu_hua_(honkai_impact), ...
    """
    names: List[str] = [entity_id.lower()]
    snake = _camel_to_snake(entity_id)
    if snake != names[0]:
        names.append(snake)

    tags: List[str] = list(names)  # 裸 tag 直接加入

    for source in sources:
        suffixes = source_suffixes.get(source, [])
        for suffix in suffixes:
            for name in names:
                tags.append(f"{name}_({suffix})")

    return sorted(set(tags))


class EntityLookup:
    """以 danbooru character tag 为键的实体查找表。"""

    def __init__(self) -> None:
        # danbooru tag -> 该 tag 命中的全部 canonical id（一个 tag 可能指向多个角色）
        self.tag_to_ids: Dict[str, List[str]] = {}
        # canonical id -> sources 列表（写 meta 的 sources 字段时取并集用）
        self.id_to_sources: Dict[str, List[str]] = {}
        # canonical id -> display_name，仅用于日志更可读
        self.id_to_name: Dict[str, str] = {}

    @property
    def is_empty(self) -> bool:
        return not self.tag_to_ids

    def add(self, entity_id: str, display_name: str, sources: List[str], tags: List[str]) -> None:
        """注册一个 entity 及其对应的 danbooru tag 列表。"""
        self.id_to_sources[entity_id] = sorted(set(sources))
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

        利用 danbooru tag 的「角色名_(作品)」命名规则自动提取 id 和 sources：
          furina_(genshin_impact) → id="furina"  sources=["genshin"]   （需 source_suffixes 已配）
          kiana_kaslana           → id="kiana_kaslana"  sources=["unknown"]

        display_name / aliases 留空待人工填写。
        返回新 entity 的 canonical id。
        """
        entity_id, sources = parse_danbooru_tag(danbooru_tag)
        if not entity_id:
            return ""

        config = _load_tag_config()
        source_suffixes = config.get("source_suffixes", {})

        entity_path = entities_dir / f"{entity_id}.json"
        if entity_path.exists():
            tags = _generate_candidate_tags(entity_id, sources, source_suffixes)
            self.add(entity_id, "", sources, tags)
            return entity_id

        stub = {
            "id": entity_id,
            "display_name": "",
            "sources": sources,
            "aliases": [],
        }
        entity_path.write_text(json.dumps(stub, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tags = _generate_candidate_tags(entity_id, sources, source_suffixes)
        self.add(entity_id, "", sources, tags)
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

    def sources_for(self, entity_ids: List[str]) -> List[str]:
        """把若干角色的 sources 字段去重排序后返回，供 meta.sources 使用。"""

        collected: List[str] = []
        seen = set()

        for entity_id in entity_ids:
            for source in self.id_to_sources.get(entity_id, []):
                if source not in seen:
                    collected.append(source)
                    seen.add(source)

        return sorted(collected)


def load_entity_db(entities_dir: Path) -> EntityLookup:
    """读取 entities/*.json 并构建 EntityLookup。

    danbooru tag 映射来源（优先级从高到低）：
      1. danbooru_tag_map.json 中的 tag_overrides（手动覆盖）
      2. 从 entity 的 id + sources 自动生成的候选 tag（基于 source_suffixes）
    """

    config = _load_tag_config()
    source_suffixes: Dict[str, List[str]] = config.get("source_suffixes", {})
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
        sources_raw = raw.get("sources") or []
        if not isinstance(sources_raw, list):
            raise ValueError(f"{file_path.name}: sources 必须是数组")

        sources = [str(s).strip() for s in sources_raw if str(s).strip()]

        # 从 id + sources 自动生成 danbooru tag 候选
        tags = _generate_candidate_tags(entity_id, sources, source_suffixes)
        lookup.add(entity_id, display_name, sources, tags)

    # 叠加手动覆盖映射（用于 id+sources 无法推导的特殊 tag）
    for tag, entity_id in tag_overrides.items():
        normalized = tag.strip().lower()
        if not normalized:
            continue
        bucket = lookup.tag_to_ids.setdefault(normalized, [])
        if entity_id not in bucket:
            bucket.append(entity_id)

    return lookup
