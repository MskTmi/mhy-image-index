const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');

// 将任意常见图片格式统一压缩输出为 JPEG。
// 这个脚本既服务于自动导入流程，也可以单独当图片预处理模板使用。

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

async function optimizeImage({ inputPath, outputPath, quality = 92 }) {
    // rotate() 会根据 EXIF 自动修正朝向，再在输出时移除这些元数据。
    if (!inputPath || !outputPath) {
        throw new Error('Both inputPath and outputPath are required.');
    }

    await fs.mkdir(path.dirname(outputPath), { recursive: true });

    await sharp(inputPath)
        .rotate()
        .jpeg({
            mozjpeg: true,
            quality
        })
        .toFile(outputPath);

    return outputPath;
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