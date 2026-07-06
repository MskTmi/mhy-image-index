# mhy-image-index

米哈游角色图片索引仓库收录崩坏3 / 原神 / 星穹铁道 / 绝区零等游戏的同人图片，提供轻量、可审阅、可追踪的图片收录流程

## 仓库结构

```txt
data/              优化后的图片文件（.jpg）
meta/              单图 metadata（.json）
entities/          角色实体词典（.json），107 个实体
dist/              聚合索引（自动生成）
scripts/           构建与自动化脚本
tools/             本地工具
  ├── manual/      手动分类图片（按角色名创建子文件夹）
  ├── inbox/       tag_image.py 待处理输入目录
  ├── processed/   tag_image.py 处理归档
  ├── process_tmp.py   手动分类图片导入脚本
  ├── tag_image.py     WD14 自动识别 + 分类脚本
  ├── download_models.py  下载 WD14 模型
  └── entities_db.py     实体/标签映射
.github/           Issue 模板与 Actions 工作流
```

## 图片收录方式

### 方式一：手动分类（推荐）

适合已有人工筛选的图片

1. 在 `tools/manual/` 下创建以角色名命名的文件夹（如 `tools/manual/流萤/`）
2. 将对应图片放入文件夹
3. 确保 `entities/` 中有对应实体（文件夹名匹配 `display_name` 或 `id`）
4. 运行导入脚本：

```bash
python tools/process_tmp.py --dry-run    # 预览
python tools/process_tmp.py              # 正式导入
```

支持多角色文件夹：
- `菈乌玛×奈芙尔` → 同时标记两个角色
- `仪玄 星见雅 叶瞬光` → 同时标记三个角色

### 方式二：WD14 自动识别

适合批量未分类的图片，由 AI 自动识别角色并打标签

1. 将图片放入 `tools/inbox/`
2. 运行识别脚本：

```bash
python tools/tag_image.py
```

3. 识别结果自动归档到 `tools/processed/`：
   - `recognized/` — 已成功导入 data/ + meta/
   - `pending_review/` — 检测到角色但 entity 未登记
   - `unrecognized/` — 未检测到角色，需人工分类
   - `duplicate/` — 与已有图片重复

### 方式三：Issue 投稿

使用仓库内置的 Issue Form 在线提交工作流自动下载、压缩、生成 metadata、发起 PR，合并后重建索引

## 数据格式

### meta/*.json — 单图元数据

```json
{
  "id": "0dd6d465",
  "image": "data/0dd6d465.jpg",
  "hash": "sha256...",
  "width": 1920,
  "height": 1080,
  "games": ["bh3"],
  "entities": ["Kiana", "Mei"]
}
```

| 字段       | 说明                                   |
| ---------- | -------------------------------------- |
| `id`       | 图片唯一标识（8 位随机字符）           |
| `image`    | 对应的图片文件路径                     |
| `hash`     | SHA-256 哈希，用于查重                 |
| `width`    | 图片宽度                               |
| `height`   | 图片高度                               |
| `games`    | 所属游戏，数组（支持联动等多归属场景） |
| `entities` | 图中角色的 canonical id 列表           |

### entities/*.json — 角色实体

```json
{
  "id": "Kiana",
  "display_name": "琪亚娜",
  "games": ["bh3"],
  "aliases": ["草履虫", "虫虫"]
}
```

| 字段           | 说明                                              |
| -------------- | ------------------------------------------------- |
| `id`           | canonical id，角色在系统内的唯一标识              |
| `display_name` | 默认展示名                                        |
| `games`        | 所属游戏，数组（支持跨游戏角色）                  |
| `aliases`      | 别名/昵称列表，用于输入解析、搜索匹配、答题判定等 |

### dist/ — 聚合索引产物

三个 JSON 文件均由 `build-index.js` 自动生成，下游应用直接通过 Raw URL 或 CDN 读取即可

#### image-index.json

图片主索引，包含全量资产数据和游戏、角色维度的倒排索引

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-22T01:28:10.629Z",
  "assets": {
    "0dd6d465": {
      "image": "data/0dd6d465.jpg",
      "games": ["bh3"],
      "entities": ["Kiana", "Mei"],
      "last_updated": "2026-05-22T01:28:10.647Z"
    }
  },
  "games": {
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
| `games`          | 按游戏维度的倒排索引（游戏 ID → 图片 ID 列表）      |
| `entities`       | 按角色维度的倒排索引（canonical id → 图片 ID 列表） |

#### entity-index.json

角色词典聚合，合并 `entities/` 目录下所有角色文件

```json
{
  "Kiana": {
    "display_name": "琪亚娜",
    "games": ["bh3"],
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

## 使用案例
> 推荐 clone 到本地再使用，通过读取 dist/ 下的 JSON 文件完成查询与索引构建

基于这套图片索引仓库，你可以快速搭建以下应用，只需读取 `dist/` 下的 JSON 即可，无需自建后端：

- **看图识人小游戏** — 随机展示图片，玩家输入角色名或别名作答用 `alias-map.json` 统一判定（如"草履虫""琪亚娜"都匹配 Kiana），用 `entity-index.json` 获取提示
- **角色图鉴 / 画廊** — 按游戏或角色筛选图片，生成图片墙`image-index.json` 内置的倒排索引可直接按维度过滤，无需遍历
- **随机图片** — 按游戏、角色或随机返回图片，供 Bot、网页、小程序调用 
- **社区 Bot 插件** — 在 QQ / Discord / Telegram Bot 中接入，用户发送"来张芽衣"即可返回对应角色的随机图片
