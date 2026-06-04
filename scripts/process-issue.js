const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const sharp = require('sharp');
const { customAlphabet } = require('nanoid');
const { ENTITY_DIR, loadAliasMap, loadEntityIndex, resolveEntities } = require('./entity-index.js');
const { parseIssueBody } = require('./issue-parser.js');
const { optimizeImage } = require('./optimize-image.js');

// 将单个 GitHub Issue 转换为仓库内的图片与 metadata 文件。
// 这个脚本是 Issue 自动导入流程的核心入口，适合作为模板复用。

const DATA_DIR = path.resolve(__dirname, '../data');
const META_DIR = path.resolve(__dirname, '../meta');
const ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz';
const ID_SIZE = 8;
const generateId = customAlphabet(ID_ALPHABET, ID_SIZE);

function parseArgs(argv) {
    // 复用在多个脚本中的轻量参数解析，只处理 --key value 形式。
    const args = {};

    for (let index = 0; index < argv.length; index += 1) {
        const token = argv[index];

        if (!token.startsWith('--')) {
            continue;
        }

        args[token.slice(2)] = argv[index + 1];
        index += 1;
    }

    return args;
}

async function fileExists(target) {
    try {
        await fs.access(target);
        return true;
    } catch {
        return false;
    }
}

async function createId() {
    // 同时检查 data 与 meta，避免只写入一半时出现 ID 冲突。
    while (true) {
        const id = generateId();
        const imagePath = path.join(DATA_DIR, `${id}.jpg`);
        const metaPath = path.join(META_DIR, `${id}.json`);

        if (!(await fileExists(imagePath)) && !(await fileExists(metaPath))) {
            return id;
        }
    }
}

async function downloadImage(url, tempDir, fileName) {
    // 先下载到临时目录，再交给 sharp 做统一编码，避免脏文件留在仓库目录。
    const response = await fetch(url);

    if (!response.ok) {
        throw new Error(`Failed to download ${url}: ${response.status} ${response.statusText}`);
    }

    const contentType = response.headers.get('content-type') || '';
    if (!contentType.startsWith('image/')) {
        throw new Error(`URL is not an image: ${url}`);
    }

    const buffer = Buffer.from(await response.arrayBuffer());
    const tempPath = path.join(tempDir, fileName);
    await fs.writeFile(tempPath, buffer);
    return tempPath;
}

async function computeHash(filePath) {
    const buffer = await fs.readFile(filePath);
    return crypto.createHash('sha256').update(buffer).digest('hex');
}

async function findDuplicateByHash(hash) {
    // 扫描已有 meta 文件，检查是否存在相同 hash 的图片。
    let entries;
    try {
        entries = await fs.readdir(META_DIR, { withFileTypes: true });
    } catch {
        return null;
    }

    for (const entry of entries) {
        if (!entry.isFile() || !entry.name.endsWith('.json')) {
            continue;
        }

        const filePath = path.join(META_DIR, entry.name);
        const meta = JSON.parse(await fs.readFile(filePath, 'utf8'));

        if (meta.hash === hash) {
            return meta.image;
        }
    }

    return null;
}

async function writeMetadata({ id, games, entities, hash, width, height }) {
    const metadata = {
        id,
        image: `data/${id}.jpg`,
        hash,
        width,
        height,
        games,
        entities
    };

    const metaFile = path.join(META_DIR, `${id}.json`);
    await fs.writeFile(metaFile, `${JSON.stringify(metadata, null, 2)}\n`, 'utf8');
    return metadata;
}

async function processIssue(issue) {
    // 弱自动化流程：已知角色解析为 canonical id 后直接导入；
    // 未知名称自动创建角色占位文件，一并进入 PR 等待人工审核。
    const parsed = parseIssueBody(issue.body || '');
    const aliasMap = await loadAliasMap();
    const entityIndex = await loadEntityIndex();
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'image-index-'));
    const { entities: resolvedEntities, unresolved } = resolveEntities(parsed.entities, {
        exists: aliasMap.size > 0,
        aliasMap
    });

    // 对未能匹配到已知角色的名称，自动创建 entities/{name}.json 占位文件。
    const newEntityIds = [];
    const now = new Date().toISOString();

    for (const name of unresolved) {
        const entityFilePath = path.join(ENTITY_DIR, `${name}.json`);

        if (!(await fileExists(entityFilePath))) {
            await fs.mkdir(ENTITY_DIR, { recursive: true });

            const entityEntry = {
                id: name,
                display_name: name,
                games: parsed.games,
                aliases: [],
                last_updated: now
            };

            await fs.writeFile(entityFilePath, `${JSON.stringify(entityEntry, null, 2)}\n`, 'utf8');
        }

        newEntityIds.push(name);
    }

    const canonicalEntities = [...resolvedEntities, ...newEntityIds];

    await fs.mkdir(DATA_DIR, { recursive: true });
    await fs.mkdir(META_DIR, { recursive: true });

    const created = [];
    const duplicates = [];

    try {
        for (let index = 0; index < parsed.imageUrls.length; index += 1) {
            const id = await createId();
            const tempSource = await downloadImage(parsed.imageUrls[index], tempDir, `${id}-source`);
            const finalImage = path.join(DATA_DIR, `${id}.jpg`);

            // 所有输入格式最终都统一输出为 JPEG，便于仓库存储和后续分发。
            await optimizeImage({ inputPath: tempSource, outputPath: finalImage });

            // 计算 hash 并获取尺寸。
            const hash = await computeHash(finalImage);
            const metadata = await sharp(finalImage).metadata();
            const width = metadata.width;
            const height = metadata.height;

            // 检查是否与已有图片重复。
            const duplicateImage = await findDuplicateByHash(hash);

            if (duplicateImage) {
                await fs.unlink(finalImage);
                duplicates.push({
                    hash,
                    duplicate_of: duplicateImage
                });
                continue;
            }

            await writeMetadata({
                id,
                games: parsed.games,
                entities: canonicalEntities,
                hash,
                width,
                height
            });

            created.push(id);
        }
    } finally {
        await fs.rm(tempDir, { recursive: true, force: true });
    }

    return {
        issue_number: issue.number,
        games: parsed.games,
        entities: canonicalEntities,
        created,
        duplicates,
        resolved_entities: resolvedEntities,
        new_entities: newEntityIds
    };
}

async function main() {
    // CLI 模式用于 GitHub Actions，也方便本地手工调试单个 Issue。
    const args = parseArgs(process.argv.slice(2));

    if (!args.issue) {
        throw new Error('Usage: node scripts/process-issue.js --issue <issue.json> [--output <result.json>]');
    }

    const issue = JSON.parse(await fs.readFile(path.resolve(process.cwd(), args.issue), 'utf8'));
    const result = await processIssue(issue);

    if (args.output) {
        await fs.writeFile(path.resolve(process.cwd(), args.output), `${JSON.stringify(result, null, 2)}\n`, 'utf8');
    } else {
        console.log(JSON.stringify(result, null, 2));
    }
}

if (require.main === module) {
    main().catch((error) => {
        console.error(error.message);
        process.exitCode = 1;
    });
}

module.exports = {
    processIssue
};
