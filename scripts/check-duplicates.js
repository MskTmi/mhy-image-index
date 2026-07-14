// 扫描 entities/ 目录，检测：
//   1. 重复 id
//   2. 重复 display_name(大小写不敏感)
//   3. 文件名 stem 与 id 不一致
//   4. 仅大小写不同的 id 变体
// 5. 同一 alias 被多个实体使用（真正的"重复实体"信号）
const fs = require('node:fs');
const path = require('node:path');

const dir = path.resolve(__dirname, '../entities');
const files = fs.readdirSync(dir).filter(f => f.endsWith('.json'));

const idToFile = new Map();
const nameToId = new Map();
const idLC = new Map();
const aliasOwners = new Map();
const dups = [];

for (const f of files) {
    let raw;
    try {
        raw = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
    } catch (e) {
        dups.push(`JSON 解析失败: ${f} - ${e.message}`);
        continue;
    }
    const id = raw.id;
    const dn = raw.display_name;

    // 1. 重复 id
    if (idToFile.has(id)) {
        dups.push(`重复 id: "${id}" 出现在 ${f} 与 ${idToFile.get(id)}`);
    } else {
        idToFile.set(id, f);
    }

    // 2. 重复 display_name
    const key = (dn || '').toLowerCase();
    if (nameToId.has(key) && nameToId.get(key) !== id) {
        dups.push(`重复 display_name: "${dn}" 在 ${id} 与 ${nameToId.get(key)}`);
    } else {
        nameToId.set(key, id);
    }

    // 3. 文件名与 id 不一致
    const stem = f.replace(/\.json$/, '');
    if (stem !== id) {
        dups.push(`文件名与 id 不一致: ${f} (id=${id})`);
    }

    // 4. 仅大小写不同的 id
    const lk = id.toLowerCase();
    if (idLC.has(lk) && idLC.get(lk) !== id) {
        dups.push(`仅大小写不同 id: ${id} 与 ${idLC.get(lk)}`);
    } else {
        idLC.set(lk, id);
    }

    // 5. alias 被多个实体共用的情况(纯别名共享,不一定是重复,信息列出供人工判断)
    for (const a of (raw.aliases || [])) {
        const ak = a.toLowerCase();
        if (!aliasOwners.has(ak)) aliasOwners.set(ak, new Set());
        aliasOwners.get(ak).add(id);
    }
}

// 把同一 alias 被多实体占用的也列出来
const sharedAliases = [];
for (const [alias, owners] of aliasOwners) {
    if (owners.size > 1) {
        sharedAliases.push(`共享 alias "${alias}" 被 ${[...owners].join(', ')} 使用`);
    }
}

console.log('=== id / display_name / 文件名 检测 ===');
if (dups.length === 0) {
    console.log('无重复 id、重复 display_name 或文件名不一致。');
} else {
    for (const d of dups) console.log(' - ' + d);
}

console.log('\n=== 被多实体共享的 alias ===');
if (sharedAliases.length === 0) {
    console.log('无共享 alias。');
} else {
    for (const s of sharedAliases) console.log(' - ' + s);
}

console.log(`\n实体文件总数: ${files.length}`);
console.log(`实体 id 总数: ${idToFile.size}`);
console.log(`共享 alias 条数: ${sharedAliases.length}`);