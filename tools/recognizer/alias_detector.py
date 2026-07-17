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

from zhconv import convert as _zh_convert

from lang_utils import detect_entity_lang
from .candidate import Candidate

# 复用 entities_db 中的 danbooru tag 生成逻辑
from entities_db import (
    EntityLookup,
    _camel_to_snake,
    _generate_candidate_tags,
    _load_tag_config,
)


def _normalize_cjk(text: str) -> str:
    """繁简归一化：转简体并 lowercase。

    用于 alias 索引与查询，使繁体文本也能统一命中。
    纯 ASCII / 假名 / 韩文文本原样返回（zhconv 对其无副作用），无开销顾虑。
    """
    if not text:
        return ""
    return _zh_convert(text, "zh-hans").lower()


class AliasDetector:
    """别名解析器：把任何文本形式（中文名、英文名、日文名、昵称、danbooru tag）映射到 entity id。"""

    def __init__(self, entities_dir: Path, *, allowed_langs: Optional[List[str]] = None) -> None:
        self.entities_dir = entities_dir
        self.allowed_langs = allowed_langs  # None 表示不过滤
        # 底层 danbooru tag 查找表（复用现有 EntityLookup）
        self._tag_lookup = EntityLookup()
        # 别名 → entity id（优先级低于 tag_lookup，用于模糊匹配）
        self._alias_to_id: Dict[str, str] = {}
        # entity id → display_name（日志用）
        self._id_to_display: Dict[str, str] = {}
        # entity id → aliases（反向查询用）
        self._id_to_aliases: Dict[str, List[str]] = {}
        # entity id → sources（易混淆对检测用）
        self._id_to_sources: Dict[str, List[str]] = {}
        # entity id → raw config dict（confusable_with 等扩展字段）
        self._entity_raw: Dict[str, dict] = {}

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

            # 语言过滤：跳过不在 allowed_langs 中的 entity
            if self.allowed_langs is not None:
                lang = detect_entity_lang(display_name, entity_id)
                if lang is not None and lang not in self.allowed_langs:
                    continue

            # 注册 danbooru tag → entity
            tags = _generate_candidate_tags(entity_id, sources, source_suffixes)
            self._tag_lookup.add(entity_id, display_name, sources, tags)

            # 注册别名 → entity
            self._id_to_display[entity_id] = display_name or entity_id
            self._id_to_aliases[entity_id] = aliases
            self._id_to_sources[entity_id] = sources
            self._entity_raw[entity_id] = raw

            # 索引 display_name（中文名）—— 同时索引繁简归一化形式
            if display_name:
                self._alias_to_id[display_name.lower()] = entity_id
                norm = _normalize_cjk(display_name)
                if norm and norm != display_name.lower():
                    self._alias_to_id.setdefault(norm, entity_id)

            # 索引 entity id 本身（英文名）
            self._alias_to_id[entity_id.lower()] = entity_id

            # 索引 snake_case 变体
            snake = _camel_to_snake(entity_id)
            if snake != entity_id.lower():
                self._alias_to_id[snake] = entity_id

            # 索引所有别名 —— 同时索引繁简归一化形式
            for alias in aliases:
                key = alias.lower()
                if key not in self._alias_to_id:
                    self._alias_to_id[key] = entity_id
                norm = _normalize_cjk(alias)
                if norm and norm != key and norm not in self._alias_to_id:
                    self._alias_to_id[norm] = entity_id

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
        # 繁简归一化键（zhconv → 简体 + lowercase）
        norm_key = _normalize_cjk(text)
        # 去重：归一化后若与原文小写相同，只查询一次
        lookup_keys = dict.fromkeys([key, norm_key])

        # 1. 精确匹配 danbooru tag（全 ASCII，与繁简无关）
        tag_candidates = self.resolve_tag(text)
        if tag_candidates:
            return tag_candidates

        # 2. 精确匹配别名索引（先原文小写，再归一化形式）
        for k in lookup_keys:
            if k in self._alias_to_id:
                eid = self._alias_to_id[k]
                display = self._id_to_display.get(eid, eid)
                return [
                    Candidate(
                        entity=eid,
                        score=1.0,
                        source="alias",
                        evidence=f"{text} → {display}",
                    )
                ]

        # 3. 子串匹配（用归一化后键查询，命中繁简混合别名也能通配）
        #   - alias 长度必须 ≥ 3，避免 "少女" 误匹配 "美少女"
        #   - text 长度不能超过 alias 的 2.5 倍，避免长文本误匹配
        if len(norm_key) >= 2:
            for alias_key, eid in self._alias_to_id.items():
                if len(alias_key) >= 3 and alias_key in norm_key and len(norm_key) <= len(alias_key) * 2.5:
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

    def get_entity_sources(self, entity_id: str) -> List[str]:
        """返回 entity 的 sources 列表（如 ['bh3']），不存在则返回空列表。"""
        return self._id_to_sources.get(entity_id, [])

    def get_confusable_pairs(self) -> List[tuple]:
        """返回易混淆角色对列表，用于 CLIP 消歧。

        自动检测：不同 source 下 display_name 相同的 entity（如同名 BH3/HSR 角色）。
        对于不同名称的易混淆对（如 Elysia↔Cyrene），需通过外部配置文件指定。

        返回 [(entity_a, entity_b), ...]，每个 tuple 已排序保证确定性顺序。
        """
        pairs: List[tuple] = []
        seen: set = set()

        # 按 display_name（小写）分组，找出同名但不同 source 的 entity
        name_to_ids: Dict[str, List[str]] = {}
        for eid, display in self._id_to_display.items():
            key = display.lower()
            name_to_ids.setdefault(key, []).append(eid)

        for eids in name_to_ids.values():
            if len(eids) < 2:
                continue
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    si = set(self._id_to_sources.get(eids[i], []))
                    sj = set(self._id_to_sources.get(eids[j], []))
                    if not si.intersection(sj) and si and sj:
                        pair = tuple(sorted([eids[i], eids[j]]))
                        if pair not in seen:
                            seen.add(pair)
                            pairs.append(pair)

        return pairs
