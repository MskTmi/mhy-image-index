# mhy-image-index

米哈游角色图片索引仓库收录崩坏3 / 原神 / 星穹铁道 / 绝区零等游戏的同人图片，提供轻量、可审阅、可追踪的图片收录流程

## 快速开始

1. 点击右上角的 **Use this template** → **Create a new repository**
2. 进入 **Settings → Actions → General**，确保以下选项已勾选：
   - **Allow GitHub Actions to create and approve pull requests**
3. 使用仓库内置的 Issue Form 提交第一批图片
4. 等待工作流自动创建 Pull Request，审核并合并
5. 合并后直接读取 `dist/` 下的 JSON 文件作为最终索引

> 仓库内置的 `data/0dd6d465.jpg` 与 `meta/0dd6d465.json` 为示例文件，用于演示目录结构和 metadata 格式；正式使用时可保留或删除

## 仓库结构

```txt
.github/     Issue 模板与 Actions 工作流
data/        优化后的图片文件（.jpg）
meta/        单图 metadata（.json）
entities/    角色词典源文件（.json）
dist/        聚合索引（自动生成）
scripts/     构建与自动化脚本
tools/       本地离线工具集（批量入库、AI 识图）
```

## 投稿与自动流程

### 如何投稿

使用仓库内置的 Issue Form 提交，核心字段：

- **Source**：作品标识（游戏 / 动漫 / 影视 / 虚拟歌手等均可）
- **Entities**：每行一个角色名，直接填写常用称呼即可（如"琪亚娜""芽衣"），无需了解 canonical id 或别名系统
- **Images**：上传一个或多个图片文件
- **Notes**：可选补充说明

### 提交后发生了什么

1. 工作流将角色名通过 `alias-map.json` 解析为 canonical id
2. 下载原图 → 压缩转 JPEG → 写入 `data/`
3. 生成 `meta/` 文件
4. 自动创建包含新增文件的 Pull Request
5. PR 合并后自动重建 `dist/` 下的聚合索引

如果角色名尚未收录，维护者会在 PR 审核时补充角色词典

### 离线批量入库

除了 Issue 投稿流程，`tools/` 目录还提供了**本地离线工具集**，适合维护者批量处理图片：

- **自动识图**（`tag_image.py`）— 把未分类的图丢进 `workspace/inbox/`，多源融合 AI 自动识别角色并写入 `data/` + `meta/`
- **手动入库**（`process_tmp.py`）— 已按角色分好类的图，按文件夹名匹配 entity 直接入库
- **模型下载**（`download_models.py`）— 自动识图前需先下载 WD14 模型权重

详见 [`tools/README.md`](tools/README.md)

### 别名投稿

可通过「别名投稿」Issue 模板为角色补充别名

- **Entity**：输入角色名（显示名、别名、id 均可），系统自动匹配
- **Aliases**：每行一个别名

提交后，系统会将新别名合并到 `entities/` 对应角色文件的 `aliases` 中，重复别名会自动跳过，PR 合并后 `alias-map.json` 也会自动更新


## 数据格式

### meta/*.json — 单图元数据

每张图片一个文件，以随机生成的 8 位 ID 命名

```json
{
  "id": "0dd6d465",
  "image": "data/0dd6d465.jpg",
  "sources": ["bh3"],
  "entities": ["Kiana", "Mei"]
}
```

| 字段       | 说明                                     |
| ---------- | ---------------------------------------- |
| `id`       | 图片唯一标识                             |
| `image`    | 对应的图片文件路径                       |
| `sources`  | 所属作品，数组（支持联动等多归属场景）   |
| `entities` | 图中角色的 canonical id 列表，不包含别名 |

### entities/*.json — 角色词典

每个角色一个文件，以 canonical id 命名`aliases` 既用于 Issue 投稿时的名称解析，也开放给下游应用——例如看图识人小游戏中，玩家可以通过别名来识别角色

```json
{
  "id": "Kiana",
  "display_name": "琪亚娜",
  "sources": ["bh3"],
  "aliases": ["草履虫", "虫虫"]
}
```

| 字段           | 说明                                              |
| -------------- | ------------------------------------------------- |
| `id`           | canonical id，角色在系统内的唯一标识              |
| `display_name` | 默认展示名                                        |
| `sources`      | 所属作品，数组（支持跨作品角色）                  |
| `aliases`      | 别名/昵称列表，用于输入解析、搜索匹配、答题判定等 |

### dist/ — 聚合索引产物

三个 JSON 文件均由 `build-index.js` 自动生成，下游应用直接通过 Raw URL 或 CDN 读取即可

#### image-index.json

图片主索引，包含全量资产数据和作品、角色维度的倒排索引

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-22T01:28:10.629Z",
  "assets": {
    "0dd6d465": {
      "image": "data/0dd6d465.jpg",
      "sources": ["bh3"],
      "entities": ["Kiana", "Mei"],
      "last_updated": "2026-05-22T01:28:10.647Z"
    }
  },
  "sources": {
    "bh3": ["0dd6d465"]
  },
  "entities": {
    "Kiana": ["0dd6d465"],
    "Mei": ["0dd6d465"]
  }
}
```

| 字段             | 说明                                                |
| ---------------- | --------------------------------------------------- |
| `schema_version` | 索引结构版本号                                      |
| `generated_at`   | 本次生成时间                                        |
| `assets`         | 以图片 ID 为 key 的主数据表                         |
| `sources`        | 按作品维度的倒排索引（作品 ID → 图片 ID 列表）      |
| `entities`       | 按角色维度的倒排索引（canonical id → 图片 ID 列表） |

#### entity-index.json

角色词典聚合，合并 `entities/` 目录下所有角色文件

```json
{
  "Kiana": {
    "display_name": "琪亚娜",
    "sources": ["bh3"],
    "aliases": ["草履虫", "虫虫"]
  }
}
```

#### alias-map.json

运行时可直接加载的别名 → canonical id 映射表，由 canonical id、display_name 和 aliases 共同生成

```json
{
  "Kiana": "Kiana",
  "琪亚娜": "Kiana",
  "草履虫": "Kiana",
  "虫虫": "Kiana"
}
```

## 脚本速查

| 脚本                          | 职责                                                                                              |
| ----------------------------- | ------------------------------------------------------------------------------------------------- |
| `build-index.js`              | 扫描 `meta/` + `entities/` 生成 `dist/` 聚合索引                                                  |
| `check-consistency.js`        | 校验 `data` ↔ `meta` ↔ `entities` 三方一致性                                                      |
| `check-duplicates.js`         | 扫描 `entities/` 检测重复 id、重复 display_name、文件名与 id 不一致、大小写重名、多实体共享 alias |
| `check-source-consistency.js` | 校验 `meta.sources` 与所引用 `entity.sources` 至少有交集，避免图片与角色作品来源不一致            |
| `check-all.js`                | 依次执行所有 `check-*.js`，默认把完整输出写入 `logs/check-<时间戳>.log`，任一失败则退出码非 0     |
| `entity-index.js`             | 加载 `entities/` 构建角色词典（build-index 子模块）                                               |
| `character-index.js`          | 聚合角色维度的图片倒排（build-index 子模块）                                                      |
| `optimize-image.js`           | sharp(mozjpeg) 压缩转 JPEG（Actions 自动流程用）                                                  |
| `issue-parser.js`             | 解析 Issue Form 提交的图片与角色信息                                                              |
| `process-issue.js`            | Issue 投稿主流程：下载 → 压缩 → 写 meta → 发起 PR                                                 |
| `process-alias.js`            | 别名投稿流程：合并 aliases 到 `entities/`                                                         |

> 一键自检：`node scripts/check-all.js`（加 `--no-log` 仅打印不写日志）

## 使用案例
> 推荐将仓库 Clone 到本地，或直接读取 `dist/` 下生成的 JSON 文件，即可完成图片查询、索引构建等功能，无需自建数据库或后端

基于本模板构建的图片索引仓库，可快速支撑以下应用：

- **看图识人小游戏** —— 随机抽取图片，支持角色名、别名等多种答案判定
- **聊天机器人插件** —— 为 QQ、Discord、Telegram、AstrBot 等平台提供随机图片、角色查询等功能
- **AI 数据集管理** —— 统一维护图片 metadata、角色词典与别名映射，作为 AI 应用的数据源

### 已有项目

本模板已应用于以下项目：

- [**mhy-image-index**](https://github.com/MskTmi/mhy-image-index)（图片索引仓库）
  - 基于本模板维护图片资源、角色词典与聚合索引
  - 为多个下游应用提供统一的数据来源

### 下游应用示例

- [**astrbot_plugin_mhy_guess**](https://github.com/MskTmi/astrbot_plugin_mhy_guess)（AstrBot 猜角色插件）
