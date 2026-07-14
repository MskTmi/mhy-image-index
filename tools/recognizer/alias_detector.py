"""
Alias 检测器 —— 在 EntityLookup 基础上扩展别名解析能力。

核心功能：
- 加载 entities/*.json，不仅索引 danbooru tag，还索引 display_name 和 aliases
- resolve_alias(text) 把任意文本（中文名、日文名、昵称等）解析成 Candidate 列表

使用方式：
    detector = AliasDetector(entities_dir)
    candidates = detector.resolve_alias("爱莉希雅")   # → [Candidate(entity="Elysia", ...)]

与 WD14 / filename / CLIP 等检测器配合，作为它们输出到 entity id 的统一转换层。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .candidate import Candidate

# 复用 entities_db 中的 danbooru tag 生成逻辑
from entities_db import (
    EntityLookup,
    _camel_to_snake,
    _generate_candidate_tags,
    _load_tag_config,
)


class AliasDetector:
    """别名解析器：把任何文本形式（中文名、英文名、日文名、昵称、danbooru tag）映射到 entity id。"""

    def __init__(self, entities_dir: Path) -> None:
        self.entities_dir = entities_dir
        # 底层 danbooru tag 查找表（复用现有 EntityLookup）
        self._tag_lookup = EntityLookup()
        # 别名 → entity id（优先级低于 tag_lookup，用于模糊匹配）
        self._alias_to_id: Dict[str, str] = {}
        # entity id → display_name（日志用）
        self._id_to_display: Dict[str, str] = {}
        # entity id → aliases（反向查询用）
        self._id_to_aliases: Dict[str, List[str]] = {}

        self._load_entities()

    # ---- 加载 ----

    def _load_entities(self) -> None:
        """从 entities/*.json 构建 tag 索引和别名索引。"""
        config = _load_tag_config()
        source_suffixes: Dict[str, List[str]] = config.get("source_suffixes", {})
        tag_overrides: Dict[str, str] = config.get("tag_overrides", {})

        if not self.entities_dir.is_dir():
            return

        for file_path in sorted(self.entities_dir.glob("*.json")):
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue

            if not isinstance(raw, dict) or "id" not in raw:
                continue

            entity_id = str(raw["id"]).strip()
            if not entity_id:
                continue

            display_name = str(raw.get("display_name") or "").strip()
            sources_raw = raw.get("sources") or []
            if not isinstance(sources_raw, list):
                sources_raw = []
            sources = [str(s).strip() for s in sources_raw if str(s).strip()]
            aliases_raw = raw.get("aliases") or []
            if not isinstance(aliases_raw, list):
                aliases_raw = []
            aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]

            # 注册 danbooru tag → entity
            tags = _generate_candidate_tags(entity_id, sources, source_suffixes)
            self._tag_lookup.add(entity_id, display_name, sources, tags)

            # 注册别名 → entity
            self._id_to_display[entity_id] = display_name or entity_id
            self._id_to_aliases[entity_id] = aliases

            # 索引 display_name（中文名）
            if display_name:
                self._alias_to_id[display_name.lower()] = entity_id

            # 索引 entity id 本身（英文名）
            self._alias_to_id[entity_id.lower()] = entity_id

            # 索引 snake_case 变体
            snake = _camel_to_snake(entity_id)
            if snake != entity_id.lower():
                self._alias_to_id[snake] = entity_id

            # 索引所有别名
            for alias in aliases:
                key = alias.lower()
                if key not in self._alias_to_id:
                    self._alias_to_id[key] = entity_id

        # 叠加手动覆盖映射
        for tag, entity_id in tag_overrides.items():
            normalized = tag.strip().lower()
            if not normalized:
                continue
            bucket = self._tag_lookup.tag_to_ids.setdefault(normalized, [])
            if entity_id not in bucket:
                bucket.append(entity_id)

    # ---- Tag 解析（WD14 等用） ----

    def resolve_tag(self, danbooru_tag: str) -> List[Candidate]:
        """把单个 danbooru character tag 解析成 Candidate 列表。

        "elysia_(honkai_impact)" → [Candidate(entity="Elysia", score=1.0, source="alias", ...)]
        """
        normalized = danbooru_tag.strip().lower()
        if not normalized:
            return []

        ids = self._tag_lookup.tag_to_ids.get(normalized, [])
        return [
            Candidate(
                entity=eid,
                score=1.0,
                source="alias",
                evidence=danbooru_tag,
            )
            for eid in ids
        ]

    def resolve_tags(self, tags: List[str]) -> List[Candidate]:
        """批量解析 danbooru character tag 列表。

        返回去重后的 Candidate 列表。
        """
        seen: set = set()
        result: List[Candidate] = []
        for tag in tags:
            for c in self.resolve_tag(tag):
                if c.entity not in seen:
                    seen.add(c.entity)
                    result.append(c)
        return result

    # ---- 别名解析（文件名等用） ----

    def resolve_alias(self, text: str) -> List[Candidate]:
        """把任意文本解析成 Candidate 列表。

        尝试顺序：
        1. 精确匹配 danbooru tag（如 "elysia_(honkai_impact)"）
        2. 精确匹配别名索引（如 "爱莉希雅"、"爱莉"、"Elysia"）
        3. 在别名中进行子串匹配（如 "爱莉希雅2" → "爱莉希雅"）

        "爱莉希雅" → [Candidate(entity="Elysia", score=1.0, source="alias", evidence="爱莉希雅")]
        """
        text = text.strip()
        if not text:
            return []

        key = text.lower()

        # 1. 精确匹配 danbooru tag
        tag_candidates = self.resolve_tag(text)
        if tag_candidates:
            return tag_candidates

        # 2. 精确匹配别名索引
        if key in self._alias_to_id:
            eid = self._alias_to_id[key]
            display = self._id_to_display.get(eid, eid)
            return [
                Candidate(
                    entity=eid,
                    score=1.0,
                    source="alias",
                    evidence=f"{text} → {display}",
                )
            ]

        # 3. 子串匹配（仅在 text 较长且 alias 足够长时尝试，避免短词误匹配）
        #   - alias 长度必须 ≥ 3，避免 "少女" 误匹配 "美少女"
        #   - text 长度不能超过 alias 的 2.5 倍，避免长文本误匹配
        if len(key) >= 2:
            for alias_key, eid in self._alias_to_id.items():
                if len(alias_key) >= 3 and alias_key in key and len(key) <= len(alias_key) * 2.5:
                    display = self._id_to_display.get(eid, eid)
                    return [
                        Candidate(
                            entity=eid,
                            score=0.85,  # 子串匹配降权
                            source="alias",
                            evidence=f"{text} ⊃ {alias_key} → {display}",
                        )
                    ]

        return []

    # ---- 工具 ----

    @property
    def entity_count(self) -> int:
        return len(self._id_to_display)

    def get_display_name(self, entity_id: str) -> str:
        return self._id_to_display.get(entity_id, entity_id)

    def get_entity_ids(self) -> List[str]:
        return sorted(self._id_to_display.keys())
