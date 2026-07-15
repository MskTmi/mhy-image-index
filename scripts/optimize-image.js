const fs = require('node:fs/promises');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// 将任意常见图片格式统一压缩输出为 JPEG。
// 实现委托给 tools/optimize_image.py（PIL 量化 + 4:4:4 + 降采样 + 兜底），
// 与本地 tag_image.py 共用同一份代码，保证跨源字节级一致、hash 一致。
// 这个脚本既服务于 Issue 自动导入流程，也可以单独当图片预处理模板使用。

function parseArgs(argv) {
    // 与仓库内其他脚本统一使用简单参数解析，降低维护成本。
    const args = {};

    for (let index = 0; index < argv.length; index += 1) {
        const token = argv[index];

        if (!token.startsWith('--')) {
            continue;
        }

        const key = token.slice(2);
        const value = argv[index + 1];
        args[key] = value;
        index += 1;
    }

    return args;
}

async function optimizeImage({ inputPath, outputPath, quality = 90, compress = true }) {
    // 通过 Python 子进程调 tools/optimize_image.py，与本地 tag_image.py 共用
    // 同一份压缩实现（PIL 量化 + 4:4:4 + 降采样 + 兜底），保证字节级一致。
    // 失败时 spawnSync 会把错误写入 stderr，抛回给调用方。
    if (!inputPath || !outputPath) {
        throw new Error('Both inputPath and outputPath are required.');
    }

    await fs.mkdir(path.dirname(outputPath), { recursive: true });

    const repoRoot = path.resolve(__dirname, '..');
    const script = path.join(repoRoot, 'tools', 'optimize_image.py');
    const python = process.env.PYTHON || 'python3';
    const args = [
        script,
        '--src', inputPath,
        '--dst', outputPath,
        '--quality', String(quality)
    ];
    if (compress) {
        args.push('--compress');
    }

    const result = spawnSync(python, args, { encoding: 'utf8' });

    if (result.error) {
        throw new Error(`Failed to spawn python: ${result.error.message}`);
    }
    if (result.status !== 0) {
        throw new Error(`optimize_image.py exited ${result.status}: ${result.stderr || result.stdout}`);
    }

    // 最后一行 stdout 是 JSON：{width, height, sha256, bytes}
    const lastLine = (result.stdout || '').trim().split(/\r?\n/).pop();
    let meta;
    try {
        meta = JSON.parse(lastLine);
    } catch (error) {
        throw new Error(`optimize_image.py unexpected output: ${lastLine}`);
    }

    return meta;
}

async function main() {
    // CLI 模式用于本地验证压缩参数是否符合预期。
    const args = parseArgs(process.argv.slice(2));
    const inputPath = args.input;
    const outputPath = args.output;
    const quality = args.quality ? Number(args.quality) : 92;

    if (!inputPath || !outputPath) {
        throw new Error('Usage: node scripts/optimize-image.js --input <source> --output <target> [--quality 92]');
    }

    if (!Number.isFinite(quality) || quality < 1 || quality > 100) {
        throw new Error('quality must be a number between 1 and 100.');
    }

    await optimizeImage({ inputPath, outputPath, quality });
    console.log(JSON.stringify({ input: inputPath, output: outputPath, quality }, null, 2));
}

if (require.main === module) {
    main().catch((error) => {
        console.error(error.message);
        process.exitCode = 1;
    });
}

module.exports = {
    optimizeImage
};