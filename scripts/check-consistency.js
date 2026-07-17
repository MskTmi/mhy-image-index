/**
 * 校验 data/ 与 meta/ 的一致性：
 *   1. meta 引用的图片文件是否存在
 *   2. data/ 中的图片是否有对应 meta
 *   3. meta 中 entities 是否都存在于 entities/ 中
 *
 * 用法: node scripts/check-consistency.js
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'data');
const META_DIR = path.join(ROOT, 'meta');
const ENTITIES_DIR = path.join(ROOT, 'entities');

let errors = 0;

function err(msg) {
    console.error('  ✗ ' + msg);
    errors += 1;
}

// ---- 加载 entities/ ----
const entityIds = new Set();
for (const entry of fs.readdirSync(ENTITIES_DIR, { withFileTypes: true })) {
    if (entry.isFile() && entry.name.endsWith('.json')) {
        try {
            const raw = JSON.parse(fs.readFileSync(path.join(ENTITIES_DIR, entry.name), 'utf8'));
            if (raw.id) entityIds.add(raw.id);
        } catch { /* skip */ }
    }
}

// ---- 检查 meta → data ----
console.log('[1/3] meta → data: 检查 meta 引用的图片是否存在...');
const metaFiles = fs.readdirSync(META_DIR).filter(f => f.endsWith('.json'));
const referencedImages = new Set();
const metaImageMap = new Map(); // image path → meta file

for (const fileName of metaFiles) {
    const filePath = path.join(META_DIR, fileName);
    let raw;
    try {
        raw = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    } catch {
        err(`./meta/${fileName}: JSON 解析失败`);
        continue;
    }

    if (!raw.image) {
        err(`./meta/${fileName}: 缺少 image 字段`);
        continue;
    }

    const imagePath = path.join(ROOT, raw.image);
    const imageRel = `./${raw.image}`;
    referencedImages.add(imageRel);
    metaImageMap.set(imageRel, fileName);

    if (!fs.existsSync(imagePath)) {
        err(`./meta/${fileName}: 引用 ${raw.image} 但文件不存在`);
    }

    // 检查 entities 字段
    if (raw.entities && Array.isArray(raw.entities)) {
        for (const eid of raw.entities) {
            if (entityIds.size > 0 && !entityIds.has(eid)) {
                err(`./meta/${fileName}: entities 引用未知 id "${eid}"（不在 entities/ 中）`);
            }
        }
    }
}

// ---- 检查 data → meta ----
console.log('[2/3] data → meta: 检查图片是否都有对应 meta...');
const imageFiles = fs.readdirSync(DATA_DIR).filter(f => /\.(jpg|jpeg|png|webp|gif)$/i.test(f));
const orphanImages = [];

for (const imgFile of imageFiles) {
    const relPath = `./data/${imgFile}`;
    if (!referencedImages.has(relPath)) {
        orphanImages.push(relPath);
    }
}

if (orphanImages.length > 0) {
    if (orphanImages.length <= 10) {
        for (const img of orphanImages) {
            err(`${img}: 无对应 meta 文件（孤儿图片）`);
        }
    } else {
        err(`${orphanImages.length} 张图片无对应 meta 文件（孤儿图片），前 5 个：`);
        for (const img of orphanImages.slice(0, 5)) {
            console.error(`       ${img}`);
        }
    }
}

// ---- 汇总 ----
console.log(`[3/3] 汇总: meta ${metaFiles.length} 个, data ${imageFiles.length} 张, entities ${entityIds.size} 个`);

if (errors === 0) {
    console.log('\n✓ 全部一致，没有问题。');
    process.exit(0);
} else {
    console.error(`\n✗ 发现 ${errors} 个问题。`);
    process.exit(1);
}
