const fs = require('node:fs/promises');
const path = require('node:path');

const URL_PATTERN = /https?:\/\/[^\s)]+/gi;

// 负责把 Issue 模板中的结构化文本转成脚本可消费的数据对象。
// 这里的解析规则越清晰，后续流程越容易作为模板扩展。

function extractSection(body, heading) {
    // 兼容 ## 与 ### 标题，方便调整 Issue 模板层级时保持脚本可用。
    const lines = body.split(/\r?\n/);
    const startIndex = lines.findIndex((line) => {
        const normalizedLine = line.trim().toLowerCase();
        return normalizedLine === `## ${heading}`.toLowerCase() || normalizedLine === `### ${heading}`.toLowerCase();
    });

    if (startIndex === -1) {
        return '';
    }

    const sectionLines = [];

    for (let index = startIndex + 1; index < lines.length; index += 1) {
        const currentLine = lines[index];

        if (/^#{2,6}\s+/.test(currentLine.trim())) {
            break;
        }

        sectionLines.push(currentLine);
    }

    return sectionLines.join('\n').trim();
}

function stripCodeFences(section) {
    // render: text 会让 GitHub 将 textarea 内容包裹在 ```text ... ``` 中，
    // 这里移除首尾的代码围栏标记，避免它们被当成有效内容。
    const codeFencePattern = /^```[\s\S]*$/;
    return section
        .split(/\r?\n/)
        .filter((line) => !codeFencePattern.test(line.trim()))
        .join('\n')
        .trim();
}

function normalizeList(section) {
    // 将 Markdown 列表归一化成字符串数组。
    return stripCodeFences(section)
        .split(/\r?\n/)
        .map((line) => line.replace(/^[-*+]\s*/, '').trim())
        .filter(Boolean);
}

function extractUrls(section) {
    return Array.from(new Set((section.match(URL_PATTERN) || []).map((item) => item.trim())));
}

function parseIssueBody(body) {
    // 优先解析模板分段；若 Images 分段缺失链接，则回退到全文抓取 URL。
    const sourceSection = extractSection(body, 'Source');
    const entitiesSection = extractSection(body, 'Entities') || extractSection(body, 'Entity');
    const imagesSection = extractSection(body, 'Images');
    const aliasesSection = extractSection(body, 'Aliases') || extractSection(body, 'Alias');
    const aliases = aliasesSection ? normalizeList(aliasesSection) : [];

    const imageUrls = extractUrls(imagesSection).length > 0 ? extractUrls(imagesSection) : extractUrls(body);
    const entities = Array.from(new Set(normalizeList(entitiesSection)));
    const sourcesText = sourceSection.split(/\r?\n/).map((line) => line.trim()).find(Boolean) || '';
    // 支持逗号（中英文）、空格、换行分隔多个作品标识
    const sources = sourcesText
        .split(/[,，\s]+/)
        .map((item) => item.trim())
        .filter(Boolean);

    if (sources.length === 0 && !aliasesSection) {
        throw new Error('Issue is missing the Source section.');
    }

    if (entities.length === 0) {
        throw new Error('Issue is missing the Entities section.');
    }

    if (imageUrls.length === 0 && imagesSection) {
        throw new Error('Issue does not contain any downloadable image URLs.');
    }

    return {
        sources,
        entities,
        imageUrls,
        aliases
    };
}

function parseArgs(argv) {
    // 保持与其他脚本一致的命令行接口风格。
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

if (require.main === module) {
    const args = parseArgs(process.argv.slice(2));

    if (!args.issue) {
        console.error('Usage: node scripts/issue-parser.js --issue <issue.json>');
        process.exitCode = 1;
    } else {
        fs.readFile(path.resolve(process.cwd(), args.issue), 'utf8')
            .then((content) => {
                const issue = JSON.parse(content);
                console.log(JSON.stringify(parseIssueBody(issue.body || ''), null, 2));
            })
            .catch((error) => {
                console.error(error.message);
                process.exitCode = 1;
            });
    }
}

module.exports = {
    parseIssueBody
};
