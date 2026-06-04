const fs = require('node:fs/promises');
const path = require('node:path');
const { ENTITY_DIR, loadEntityIndex } = require('./entity-index.js');
const { parseIssueBody } = require('./issue-parser.js');

// 处理别名投稿：将用户提交的别名合并到已有角色文件中，创建 PR 等待审核。

function parseArgs(argv) {
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

function findCanonicalId(entityIndex, input) {
    // 支持通过 display_name、alias 或 id 匹配角色。
    const aliasMap = entityIndex.aliasMap;

    if (aliasMap.has(input)) {
        return aliasMap.get(input);
    }

    // 直接匹配 canonical id（大小写不敏感兜底）。
    const lowerInput = input.toLowerCase();

    for (const [canonicalId] of Object.entries(entityIndex.entities)) {
        if (canonicalId.toLowerCase() === lowerInput) {
            return canonicalId;
        }
    }

    return null;
}

async function processAliasIssue(issue) {
    const parsed = parseIssueBody(issue.body || '');
    const entityIndex = await loadEntityIndex();
    const now = new Date().toISOString();

    const entityInput = parsed.entities[0];
    const aliases = parsed.aliases;

    if (!entityInput) {
        throw new Error('Alias submission is missing the Entity field.');
    }

    if (aliases.length === 0) {
        throw new Error('Alias submission is missing aliases.');
    }

    const canonicalId = findCanonicalId(entityIndex, entityInput);

    if (!canonicalId) {
        return {
            issue_number: issue.number,
            canonical_id: null,
            entity_display_name: null,
            added: [],
            skipped: [],
            unmatched: [entityInput]
        };
    }

    const entityFilePath = path.join(ENTITY_DIR, `${canonicalId}.json`);
    let entityEntry;

    if (await fileExists(entityFilePath)) {
        entityEntry = JSON.parse(await fs.readFile(entityFilePath, 'utf8'));
    } else {
        entityEntry = entityIndex.entities[canonicalId];
    }

    const existingAliases = new Set(entityEntry.aliases || []);
    const added = [];
    const skipped = [];

    for (const alias of aliases) {
        if (alias === entityEntry.id || alias === entityEntry.display_name) {
            skipped.push(alias);
        } else if (existingAliases.has(alias)) {
            skipped.push(alias);
        } else {
            existingAliases.add(alias);
            added.push(alias);
        }
    }

    if (added.length > 0) {
        entityEntry.aliases = Array.from(existingAliases).sort();
        entityEntry.last_updated = now;
        await fs.mkdir(ENTITY_DIR, { recursive: true });
        await fs.writeFile(entityFilePath, `${JSON.stringify(entityEntry, null, 2)}\n`, 'utf8');
    }

    return {
        issue_number: issue.number,
        canonical_id: canonicalId,
        entity_display_name: entityEntry.display_name,
        added,
        skipped,
        unmatched: []
    };
}

async function main() {
    const args = parseArgs(process.argv.slice(2));

    if (!args.issue) {
        throw new Error('Usage: node scripts/process-alias.js --issue <issue.json> [--output <result.json>]');
    }

    const issue = JSON.parse(await fs.readFile(path.resolve(process.cwd(), args.issue), 'utf8'));
    const result = await processAliasIssue(issue);

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
    processAliasIssue
};
