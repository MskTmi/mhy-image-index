const fs = require('node:fs/promises');
const path = require('node:path');

const ENTITY_DIR = path.resolve(__dirname, '../entities');
const ALIAS_MAP_DIST_FILE = path.resolve(__dirname, '../dist/alias-map.json');

function compareText(left, right) {
    return left.localeCompare(right, 'en');
}

function normalizeText(value) {
    return typeof value === 'string' ? value.trim() : '';
}

function ensureOptionalStringList(value, fieldName, canonicalId) {
    if (value == null) {
        return [];
    }

    if (!Array.isArray(value)) {
        throw new Error(`entities/${canonicalId}.json: ${fieldName} must be an array of strings.`);
    }

    const list = value
        .map((item) => normalizeText(item))
        .filter(Boolean);

    return Array.from(new Set(list)).sort(compareText);
}

function buildAliasMap(entities) {
    const aliasMap = new Map();

    for (const [canonicalId, entry] of Object.entries(entities)) {
        const candidates = [canonicalId, entry.display_name, ...entry.aliases].filter(Boolean);

        for (const candidate of candidates) {
            const normalized = normalizeText(candidate);
            const existing = aliasMap.get(normalized);

            if (existing && existing !== canonicalId) {
                throw new Error(`entities: alias "${normalized}" conflicts between ${existing} and ${canonicalId}.`);
            }

            aliasMap.set(normalized, canonicalId);
        }
    }

    return aliasMap;
}

function ensureGamesList(value, fileName, id) {
    if (!Array.isArray(value) || value.length === 0) {
        throw new Error(`${fileName}: games must be a non-empty array of strings.`);
    }

    const list = value
        .map((item) => normalizeText(item))
        .filter(Boolean);

    if (list.length === 0) {
        throw new Error(`${fileName}: games must contain at least one non-empty string.`);
    }

    return Array.from(new Set(list)).sort(compareText);
}

function normalizeEntityEntry(raw, fileName) {
    if (raw == null || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new Error(`${fileName}: entity definition must be an object.`);
    }

    const id = normalizeText(raw.id);

    if (!id) {
        throw new Error(`${fileName}: id must be a non-empty string.`);
    }

    const displayName = normalizeText(raw.display_name);
    const games = ensureGamesList(raw.games, fileName, id);
    const aliases = ensureOptionalStringList(raw.aliases, 'aliases', id)
        .filter((alias) => alias !== id && alias !== displayName);
    return {
        id,
        entry: {
            display_name: displayName || id,
            games,
            aliases
        }
    };
}

function isJsonFile(name) {
    return name.toLowerCase().endsWith('.json');
}

function normalizeEntityEntries(rawEntries) {
    const entities = {};

    for (const rawEntry of rawEntries) {
        if (entities[rawEntry.id]) {
            throw new Error(`entities: duplicate canonical id ${rawEntry.id}.`);
        }

        entities[rawEntry.id] = rawEntry.entry;
    }

    buildAliasMap(entities);
    return entities;
}

async function loadEntityIndex() {
    try {
        const directoryEntries = await fs.readdir(ENTITY_DIR, { withFileTypes: true });
        const files = directoryEntries
            .filter((entry) => entry.isFile() && isJsonFile(entry.name))
            .map((entry) => entry.name)
            .sort(compareText);
        const rawEntries = [];

        for (const fileName of files) {
            const filePath = path.join(ENTITY_DIR, fileName);
            const parsed = JSON.parse(await fs.readFile(filePath, 'utf8'));
            const normalized = normalizeEntityEntry(parsed, fileName);

            rawEntries.push(normalized);
        }

        const entities = normalizeEntityEntries(rawEntries);

        return {
            sourcePath: ENTITY_DIR,
            exists: files.length > 0,
            entities,
            aliasMap: buildAliasMap(entities)
        };
    } catch (error) {
        if (error && error.code === 'ENOENT') {
            return {
                sourcePath: ENTITY_DIR,
                exists: false,
                entities: {},
                aliasMap: new Map()
            };
        }

        throw error;
    }
}

async function loadAliasMap() {
    try {
        const raw = JSON.parse(await fs.readFile(ALIAS_MAP_DIST_FILE, 'utf8'));

        if (raw == null || typeof raw !== 'object' || Array.isArray(raw)) {
            throw new Error('dist/alias-map.json: root value must be an object.');
        }

        return new Map(
            Object.entries(raw)
                .map(([alias, canonicalId]) => [normalizeText(alias), normalizeText(canonicalId)])
                .filter(([alias, canonicalId]) => alias && canonicalId)
        );
    } catch (error) {
        if (error && error.code === 'ENOENT') {
            const entityIndex = await loadEntityIndex();
            return entityIndex.aliasMap;
        }

        throw error;
    }
}

function resolveEntities(inputEntities, entityIndex) {
    // 弱自动化模式：已知别名解析为 canonical id，未知名称原样保留，
    // 由 process-issue 后续自动创建角色占位文件并交由 PR 人工审核。
    if (!entityIndex || !entityIndex.exists) {
        const entities = Array.from(new Set(inputEntities.map((item) => normalizeText(item)).filter(Boolean))).sort(compareText);
        return { entities, unresolved: [] };
    }

    const resolved = [];
    const unresolved = [];

    for (const entity of inputEntities) {
        const normalized = normalizeText(entity);

        if (!normalized) {
            continue;
        }

        const canonicalId = entityIndex.aliasMap.get(normalized);

        if (canonicalId) {
            resolved.push(canonicalId);
        } else {
            unresolved.push(normalized);
        }
    }

    return {
        entities: Array.from(new Set(resolved)).sort(compareText),
        unresolved: Array.from(new Set(unresolved)).sort(compareText)
    };
}

module.exports = {
    ALIAS_MAP_DIST_FILE,
    ENTITY_DIR,
    compareText,
    loadAliasMap,
    loadEntityIndex,
    resolveEntities
};
