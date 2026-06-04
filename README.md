# image-index-template

基于 GitHub Repository 的图片索引仓库模板，提供一套轻量、可审阅、可追踪的图片收录流程：投稿者通过 Issue 提交图片或别名，工作流自动下载、压缩、生成 metadata、发起 PR，并在合并后重建聚合索引

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
data/        优化后的图片文件（.jpg）
meta/        单图 metadata（.json）
entities/    角色词典源文件（.json）
dist/        聚合索引（自动生成）
scripts/     构建与自动化脚本
.github/     Issue 模板与 Actions 工作流
```

## 投稿与自动流程

### 如何投稿

使用仓库内置的 Issue Form 提交，核心字段：

- **Game**：游戏标识
- **Entities**：每行一个角色名，直接填写常用称呼即可（如"琪亚娜""芽衣"），无需了解 canonical id 或别名系统
- **Images**：上传一个或多个图片文件
- **Notes**：可选补充说明

### 提交后发生了什么

1. 工作流将角色名通过 `alias-map.json` 解析为 canonical id
2. 下载原图 → 压缩转 JPEG → 写入 `data/`
3. 生成 `meta/` 文件
4. 自动创建包含新增文件的 Pull Request
5. PR 合并后自动重建 `dist/` 下的聚合索引

如果角色名尚未收录，维护者会在 PR 审核时补充角色词典。

### 别名投稿

可通过「别名投稿」Issue 模板为角色补充别名

- **Entity**：输入角色名（显示名、别名、id 均可），系统自动匹配
- **Aliases**：每行一个别名

提交后，系统会将新别名合并到 `entities/` 对应角色文件的 `aliases` 中，重复别名会自动跳过，PR 合并后 `alias-map.json` 也会自动更新。


## 数据格式

### meta/*.json — 单图元数据

每张图片一个文件，以随机生成的 8 位 ID 命名

```json
{
  "id": "0dd6d465",
  "image": "data/0dd6d465.jpg",
  "games": ["bh3"],
  "entities": ["Kiana", "Mei"]
}
```

| 字段       | 说明                                     |
| ---------- | ---------------------------------------- |
| `id`       | 图片唯一标识                             |
| `image`    | 对应的图片文件路径                       |
| `games`    | 所属游戏，数组（支持联动等多归属场景）   |
| `entities` | 图中角色的 canonical id 列表，不包含别名 |

### entities/*.json — 角色词典

每个角色一个文件，以 canonical id 命名。`aliases` 既用于 Issue 投稿时的名称解析，也开放给下游应用——例如看图识人小游戏中，玩家可以通过别名来识别角色

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

- **看图识人小游戏** — 随机展示图片，玩家输入角色名或别名作答。用 `alias-map.json` 统一判定（如"草履虫""琪亚娜"都匹配 Kiana），用 `entity-index.json` 获取提示
- **角色图鉴 / 画廊** — 按游戏或角色筛选图片，生成图片墙。`image-index.json` 内置的倒排索引可直接按维度过滤，无需遍历
- **随机图片** — 按游戏、角色或随机返回图片，供 Bot、网页、小程序调用 
- **社区 Bot 插件** — 在 QQ / Discord / Telegram Bot 中接入，用户发送"来张芽衣"即可返回对应角色的随机图片
