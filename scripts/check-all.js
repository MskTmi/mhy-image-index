/**
 * 统一检查入口：依次执行所有 check-*.js 校验脚本，聚合结果。
 *
 * 行为：
 *   - 默认将每次执行的完整输出写入 logs/check-<时间戳>.log
 *   - 同时仍打印到 stdout/stderr，便于直接查看
 *   - 任一 check 失败则最终退出码为 1，方便 CI / git pre-push 使用
 *
 * 用法:
 *   node scripts/check-all.js           # 跑全部 check，并写日志
 *   node scripts/check-all.js --no-log # 不写日志，只打 stdout
 */

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const LOGS_DIR = path.join(ROOT, 'logs');

// 按执行顺序列出要跑的 check 脚本（相对 scripts/ 的文件名）
const CHECKS = [
    'check-consistency.js',
    'check-duplicates.js',
    'check-source-consistency.js',
];

// ---- 解析参数 ----
const argv = process.argv.slice(2);
const writeLog = !argv.includes('--no-log');

// ---- 准备日志文件 ----
let logStream = null;
let logFilePath = null;
if (writeLog) {
    fs.mkdirSync(LOGS_DIR, { recursive: true });
    const ts = new Date().toISOString().replace(/[:T]/g, '').slice(0, 15); // YYYYMMDDHHmmss
    logFilePath = path.join(LOGS_DIR, `check-${ts}.log`);
    logStream = fs.createWriteStream(logFilePath, { encoding: 'utf8' });
}

function logLine(line) {
    console.log(line);
    if (logStream) logStream.write(line + '\n');
}
function logErr(line) {
    console.error(line);
    if (logStream) logStream.write(line + '\n');
}

if (writeLog) {
    logLine(`# check-all 日志: ${new Date().toISOString()}`);
    logLine(`# 日志文件: ${path.relative(ROOT, logFilePath)}`);
    logLine('');
}

// ---- 依次执行 ----
let totalFailed = 0;
const summary = [];

for (const script of CHECKS) {
    const scriptPath = path.join(__dirname, script);
    if (!fs.existsSync(scriptPath)) {
        logErr(`⚠ 跳过: ${script} 不存在`);
        summary.push({ script, status: 'missing', code: -1 });
        totalFailed += 1;
        continue;
    }

    logLine(`──────── ${script} ────────`);

    const result = spawnSync(process.execPath, [scriptPath], {
        cwd: ROOT,
        encoding: 'utf8',
    });

    if (result.stdout) {
        process.stdout.write(result.stdout);
        if (logStream) logStream.write(result.stdout);
    }
    if (result.stderr) {
        process.stderr.write(result.stderr);
        if (logStream) logStream.write(result.stderr);
    }

    const code = result.status ?? 1;
    if (result.error) {
        logErr(`✗ ${script} 启动失败: ${result.error.message}`);
        summary.push({ script, status: 'error', code });
        totalFailed += 1;
    } else if (code === 0) {
        logLine(`✓ ${script} 通过\n`);
        summary.push({ script, status: 'pass', code });
    } else {
        logErr(`✗ ${script} 失败 (exit ${code})\n`);
        summary.push({ script, status: 'fail', code });
        totalFailed += 1;
    }
}

// ---- 汇总 ----
logLine('──────── 汇总 ────────');
for (const item of summary) {
    const mark = item.status === 'pass' ? '✓' :
        item.status === 'fail' ? '✗' :
            item.status === 'error' ? '✗' : '⚠';
    logLine(`  ${mark} ${item.script} — ${item.status} (exit ${item.code})`);
}
logLine('');

if (totalFailed === 0) {
    logLine('✓ 全部通过。');
} else {
    logErr(`✗ 共 ${totalFailed} 项失败。`);
}

const exitCode = totalFailed === 0 ? 0 : 1;

if (logStream) {
    // createWriteStream 是异步的，必须等 'finish' 回调触发（缓冲已刷盘）
    // 之后才能 process.exit，否则进程提前退出会导致日志文件为空或缺失
    logStream.end(() => {
        console.log(`\n日志已写入: ${path.relative(ROOT, logFilePath)}`);
        process.exit(exitCode);
    });
} else {
    process.exit(exitCode);
}