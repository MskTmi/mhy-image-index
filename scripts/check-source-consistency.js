/**
 * 校验 meta 图片的 sources 与所引用 entity 的 sources 是否一致。
 *
 * 规则：对每个 meta，遍历其 entities，要求该 meta 的 sources
 *   至少与该 entity 的 sources 有一个交集。若完全无交集，则判定为
 *   不一致（可能是图片打错来源、或实体 sources 漏填）。
 *
 * 用法: node scripts/check-source-consistency.js
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const META_DIR = path.join(ROOT, 'meta');
const ENTITIES_DIR = path.join(ROOT, 'entities');

let errors = 0;
let warnings = 0;

function err(msg) {
    console.error('  ✗ ' + msg);
    errors += 1;
}
function warn(msg) {
    console.warn('  ⚠ ' + msg);
    warnings += 1;
}

// ---- 加载 entities/ ----
console.log('[1/3] 加载 entities/...');
const entityMap = new Map(); // id → { display_name, sources: Set }
const entityFiles = fs.readdirSync(ENTITIES_DIR).filter(f => f.endsWith('.json'));
for (const fileName of entityFiles) {
    try {
        const raw = JSON.parse(fs.readFileSync(path.join(ENTITIES_DIR, fileName), 'utf8'));
        if (!raw.id) {
            warn(`entities/${fileName}: 缺少 id 字段，跳过`);
            continue;
        }
        if (!Array.isArray(raw.sources)) {
            warn(`entities/${fileName}: sources 不是数组，按空集处理`);
            raw.sources = [];
        }
        entityMap.set(raw.id, {
            display_name: raw.display_name || raw.id,
            sources: new Set(raw.sources),
        });
    } catch (e) {
        warn(`entities/${fileName}: JSON 解析失败 (${e.message})`);
    }
}
console.log(`  共加载 ${entityMap.size} 个实体`);

// ---- 检查 meta × entity sources 一致性 ----
console.log('[2/3] 检查 meta.sources 与 entity.sources 一致性...');
const metaFiles = fs.readdirSync(META_DIR).filter(f => f.endsWith('.json'));

for (const fileName of metaFiles) {
    let raw;
    try {
        raw = JSON.parse(fs.readFileSync(path.join(META_DIR, fileName), 'utf8'));
    } catch (e) {
        warn(`meta/${fileName}: JSON 解析失败，跳过`);
        continue;
    }

    if (!Array.isArray(raw.sources) || raw.sources.length === 0) {
        warn(`meta/${fileName}: sources 缺失或为空`);
    }

    if (!Array.isArray(raw.entities) || raw.entities.length === 0) {
        // 无实体引用，无法比对，跳过
        continue;
    }

    const imageSources = new Set(raw.sources || []);
    if (imageSources.size === 0) continue;

    for (const eid of raw.entities) {
        if (!entityMap.has(eid)) {
            // 未知实体由 check-consistency.js 负责，这里不重复报
            continue;
        }
        const entity = entityMap.get(eid);
        if (entity.sources.size === 0) {
            // 实体没声明来源，无法比对，跳过
            continue;
        }

        // 求交集
        let hasIntersection = false;
        for (const s of imageSources) {
            if (entity.sources.has(s)) {
                hasIntersection = true;
                break;
            }
        }

        if (!hasIntersection) {
            err(
                `meta/${fileName}: 图片 sources [${[...imageSources].join(', ')}] ` +
                `与实体 ${eid}(${entity.display_name}) 的 sources [${[...entity.sources].join(', ')}] 无交集`
            );
        }
    }
}

// ---- 汇总 ----
console.log(`[3/3] 汇总: meta ${metaFiles.length} 个, entities ${entityMap.size} 个`);

if (errors === 0 && warnings === 0) {
    console.log('\n✓ sources 全部一致，没有问题。');
    process.exit(0);
} else {
    if (errors) console.error(`\n✗ 发现 ${errors} 个不一致。`);
    if (warnings) console.warn(`\n⚠ 另有 ${warnings} 个警告。`);
    process.exit(errors === 0 ? 0 : 1);
}