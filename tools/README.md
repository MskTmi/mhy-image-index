# 图片收录工具集（离线批量入库 + 自动识图）

本目录是仓库主流程（GitHub Issue 投稿 → Actions 建索引）之外的**本地离线补充入口**，面向维护者：

- 手上有一大批已筛好的同人图、想批量塞进仓库；
- 手上有一堆没分类的图、希望 AI 帮你识别角色后再批量入库；
- 或者需要在批量入库前先盘点新角色，再走 Issue 投稿。

与任何具体作品（游戏 / 动漫 / 影视 / VTuber / 舰船 …）**完全无关**。作品归属由 `entities/<id>.json` 的 `sources` 字段 + `tools/danbooru_tag_map.json` 决定，工具不预设任何角色或作品知识。


## 目录结构

```text
tools/
├── .venv/           Python 虚拟环境（.gitignore 已排除）
├── .hf-cache/       WD14 模型权重（.gitignore 已排除，体积大）
├── workspace/       运行时工作目录（源图输入 + 处理后归档）
│   ├── inbox/           丢待识别图的地方（跑完会被清空）
│   ├── manual/          手动分角色入库：下面建子目录放图
│   └── processed/       源图处理后按结果归档到四个子目录：
│       ├── recognized/      识别成功 → 已写入 data/ + meta/
│       ├── pending_review/  检测到角色但 entity 未登记 → 留待人工审核
│       ├── unrecognized/    未检测到任何角色 → 需人工分类
│       └── duplicate/       与已有图 sha256 重复 → 已跳过
├── utils/                   辅助/一次性脚本
│   └── fix_unknown_sources.py   一次性清理 sources: ["unknown"] 残留
├── entities_db.py            加载 entities/ 构建 danbooru tag → canonical id 映射
├── tag_image.py              主工具：基于 WD14 自动识图入库
├── process_tmp.py            手动分类图片导入
├── download_models.py       下载 WD14 模型权重（仅初次运行）
├── danbooru_tag_map.json     ← 你要配的作品后缀表 / tag 覆盖表
├── requirements.txt          Python 依赖
└── README.md                 本文档
```

## 脚本职责速查

| 脚本                           | 职责                                                                |
| ------------------------------ | ------------------------------------------------------------------- |
| `download_models.py`           | 下载 WD14 模型权重到 `.hf-cache/`，仅初次运行                       |
| `tag_image.py`                 | AI 自动识图入库（WD14 → entity 匹配 → data/meta + 归档源图）        |
| `process_tmp.py`               | 手动分类入库（按 `workspace/manual/<角色名>/` 文件夹名匹配 entity） |
| `utils/fix_unknown_sources.py` | 一次性修复历史 `sources: ["unknown"]` 残留                          |
| `build-index.js`               | 重建 `dist/` 聚合索引（仓库根的 Node 脚本，非 tools 命令）          |
| `check-consistency.js`         | 校验 data/ ↔ meta/ ↔ entities/ 三方一致性（仓库根的 Node 脚本）     |

---

## 快速开始

第一次使用照下面 6 步执行即可跑通。细节原理见后续章节。

### 1. 安装依赖

```powershell
cd tools
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> Linux/macOS 激活 venv 用 `source .venv/bin/activate`，下文 PowerShell 命令换成对应路径。

**CPU 方案**（无 NVIDIA 显卡 / 不在意速度）：编辑 `requirements.txt`，把 `onnxruntime-gpu` 和 `nvidia-*` 注释掉、`onnxruntime` 取消注释，再 `pip install`。

### 2. 下载 WD14 模型

```powershell
.venv\Scripts\python.exe download_models.py
```

默认下 `EVA02_Large`（1.26GB，精度最高）。日常快模型用 `--model SwinV2_v3`（467MB）。模型清单详见 [模型速查](#模型速查)。

### 3. 配 entities 与 source_suffixes

至少在 `entities/` 下放一个角色 JSON：

```json
// entities/Kiana.json
{
  "id": "Kiana",
  "display_name": "琪亚娜",
  "sources": ["bh3"],
  "aliases": ["草履虫", "虫虫"]
}
```

并在 `tools/danbooru_tag_map.json` 里为该作品配 danbooru 后缀：

```json
{
  "source_suffixes": {
    "bh3": ["honkai_impact_3rd"]
  },
  "tag_overrides": {}
}
```

> **必须前后对齐**：entity 的 `sources` 写什么，`source_suffixes` 就必须有同名 key。详见 [4. 配 `danbooru_tag_map.json`](#4-配-danbooru_tag_mapjson)。

### 4. 把图丢进 `workspace/inbox/`

```text
tools/workspace/inbox/
  IMG_0001.jpg
  IMG_0002.png
```

### 5. 跑识别

```powershell
.venv\Scripts\python.exe tag_image.py
```

跑完源图按结果归档到 `tools/workspace/processed/` 的四个子目录（`recognized/` 成功、`pending_review/` 待补 entity、`unrecognized/` 没识别到、`duplicate/` 重复）。

### 6. 重建聚合索引

```powershell
# 仓库根目录
node scripts/build-index.js
```

会重写 `dist/{image-index,entity-index,alias-map}.json`，下游消费方读这套 JSON。

---

## 数据结构

### entities — 角色字典

`entities/<canonical_id>.json`，每个角色一个文件：

```json
{
  "id": "Kiana",
  "display_name": "琪亚娜",
  "sources": ["bh3"],
  "aliases": ["草履虫", "虫虫"]
}
```

| 字段           | 说明                                                                                                                                                                                           |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`           | canonical id，**强烈建议 PascalCase 英文名**（如 `Kiana` / `Mei`）。下文 `_camel_to_snake` 会把它拆出 `kiana` / `mei` 之类的 danbooru 写法去自动拼 tag 候选；中文 id 拆不出 roman 字符的 tag。 |
| `display_name` | 人类可读名（中文/原文都行），用于显示和手动入库时匹配文件夹名。                                                                                                                                |
| `sources`      | **内部 source id 数组，名字完全由你定**（如 `"bh3"`）。必须非空，且与 `source_suffixes` key 逐字符对齐。可填多作品。                                                                           |
| `aliases`      | 别名数组，供 Issue 投稿流程的 alias-map 匹配。**自动识图不读 aliases**（中文别名没法和英文字符 tag 对齐），别往这儿放英文 tag。                                                                |

### meta — 单图元数据

`meta/<8 位 id>.json`，由入库脚本自动写：

```json
{
  "id": "0dd6d465",
  "image": "data/0dd6d465.jpg",
  "hash": "68a1...da02",
  "width": 1402,
  "height": 1122,
  "sources": ["bh3"],
  "entities": ["Kiana"]
}
```

`sources` = 匹配到的 entity 的 `sources` 并集；`entities` 写 canonical id 数组。

### danbooru_tag_map — 作品后缀 / tag 覆盖表

`tools/danbooru_tag_map.json`，只有两个字段：`source_suffixes`（作品后缀表，详见 [4.1](#41-source_suffixes--作品后缀表)）和 `tag_overrides`（特例覆盖表，详见 [4.2](#42-tag_overrides--边跑边补的特例表)）。默认全空，由用户自填。

---

## 4. 配 `danbooru_tag_map.json`

这是把"你的仓库世界观"和"Danbooru/WD14 的外部命名"对接起来的**唯一配置文件**。可以从以下途径获取：

1. **手动填写**（见下方 4.1/4.2）；
2. **让 AI 根据现有数据自动生成**：把 `entities/` 下的角色文件喂给 AI，让它帮你反查 Danbooru 后缀并填充 `source_suffixes` 和 `tag_overrides`；
3. **自动上网获取**：根据 `entities/*.json` 中的 `sources` 列表，让 AI 获取 Danbooru wiki 或 API 自动拉取 copyright tag 映射。

### 4.1 `source_suffixes` — 作品后缀表

```json
{
  "source_suffixes": {
    "<内部 source id>": ["<danbooru copyright 后缀 1>", "<后缀 2>", ...]
  }
}
```

- **左侧 key**：你**自定义的内部 source id**，就是 `entities/*.json` 的 `sources` 字段里写的字符串。完全由你命名，工具不预设。游戏、动漫、电影、电视剧、VTuber 都行。
- **右侧 value 数组**：Danbooru 的 **copyright tag 标准命名**，也就是 WD14 输出的 character tag 里括号后缀那一段。WD14 输出形如 `kiana_(honkai_impact_3rd)`，括号里的 `honkai_impact_3rd` 就是这里要填的后缀。

#### 值从哪来 —— 三种途径

- **途径 A：Danbooru 官网直接查**（最准）。在 [danbooru.donmai.us](https://danbooru.donmai.us) 搜一个已知角色的 character tag，看完整 `_(...)` 后缀。例如搜 `kiana` 看到 `kiana_(honkai_impact_3rd)`，括号里 `honkai_impact_3rd` 就是后缀。
- **途径 B：`--dump-tags` 试跑**（最直接）：
  ```powershell
  .venv\Scripts\python.exe tag_image.py --dump-tags 某张该作品的图.jpg
  ```
  WD14 吐出所有 character tag，自带 `(后缀)`。
- **途径 C：同一作品多写法都填**。Danbooru 上同一作品可能有多个等价 copyright 后缀（命名演化、重命名都会产生旧后缀），**全部加进 value 数组**。例如崩坏3 同时有 `honkai_impact_3rd`（新）和 `honkai_impact`（旧），两条都要写。

#### 填充示例（多作品类型）

```json
{
  "source_suffixes": {
    "bh3": ["honkai_impact_3rd", "honkai_impact"],
    "genshin": ["genshin_impact"],
    "star_rail": ["honkai:_star_rail"]
  },
  "tag_overrides": {}
}
```

注意：

- Danbooru copyright 后缀里的特殊字符（如 `:`）原样保留，不转换；
- key 不要带空格、最好全小写（`star_rail` 不要写 `Star Rail`）。

> 详见 [5. 候选 tag 生成原理](#候选-tag-生成原理) 里对 `source_suffixes` 的实际用法。

### 4.2 `tag_overrides` — 边跑边补的特例表

```json
{
  "tag_overrides": {
    "<danbooru character tag 原样>": "<已登记的 entity canonical id>"
  }
}
```

当自动推导候选 tag 都没覆盖到 WD14 真实输出的某个 tag 时，用它兜底，直接把那个 tag 挂到指定 canonical id 上。

#### 怎么发现自己需要加

发现某张图明明画的是已登记的角色，却进了 `workspace/processed/pending_review/`，或终端日志写 `unmatched character tag:`：

1. 看日志或 `workspace/processed/pending_review/` 里的图，认出角色；
2. 单图再 `--dump-tags` 确认 WD14 实际输出的 character tag 字符串；
3. 去 Danbooru 搜一下，确认是标准 character tag 写法（不是 outfit/costume tag）；
4. 决定它该挂到哪个已登记 entity id；
5. 在 `tag_overrides` 里填一行，下一轮重跑即可命中。

#### 典型 case

| case                           | danbooru tag                    | → entity id | 原因                                              |
| ------------------------------ | ------------------------------- | ----------- | ------------------------------------------------- |
| Danbooru 用全名，entity 用简称 | `raiden_mei`                    | `Mei`       | `_camel_to_snake(Mei)=mei`，候选不含 `raiden_mei` |
| 同角色换装 tag 指向同 entity   | `herrscher_of_thunder_(bh3)`    | `Mei`       | 雷之律者仍是芽衣                                  |
| WD14 用旧后缀，source 已更新   | `kiana_kaslana_(honkai_impact)` | `Kiana`     | 旧后缀，需 override 指向新 id                     |

```json
{
  "tag_overrides": {
    "raiden_mei": "Mei",
    "herrscher_of_thunder_(bh3)": "Mei",
    "kiana_kaslana_(honkai_impact)": "Kiana"
  }
}
```

> [!WARNING]
> `tag_overrides` 的 value **必须是已存在于 `entities/` 的 canonical id**，否则 `lookup.resolve` 命中后会把不存在的 id 写进 meta，下一步 `build-index.js` 校验会报 `must use canonical ids only`。

---

## 候选 tag 生成原理

`entities_db._generate_candidate_tags(entity_id, sources, source_suffixes)` 对每个 entity 算出它可能被 WD14 输出的所有 character tag 集合。

#### 两种 id 变体

- **原样小写**：`Kiana` → `kiana`
- **camelCase / PascalCase → snake_case**：`Kiana` → `kiana`（单名不变），`Mei` → `mei`（单名不变）

两种都加入候选（如不重复）。

#### × 每个后缀组合

对 entity `sources` 里每个 source id，从 `source_suffixes` 取它的后缀数组，每个后缀和每个 id 变体拼一次：

```text
Kiana + ["bh3"]  →  kiana, kiana_(honkai_impact_3rd), kiana_(honkai_impact)
Mei   + ["bh3"]  →  mei, mei_(honkai_impact_3rd), mei_(honkai_impact)
```

> **这就是为什么 `sources` 与 `source_suffixes` key 必须前后对齐**：
> 若 `entities/Kiana.json` 写 `"sources": ["bh3"]` 但 `source_suffixes` 里没有 `bh3`，候选就只剩裸 `kiana`，WD14 输出 `kiana_(honkai_impact_3rd)` 时识别不到、图被丢进 `workspace/processed/pending_review/`。

#### 自动创建占位 entity

`tag_image.py` 看到 WD14 输出了未登记的 character tag 时调用 `EntityLookup.add_auto`：

- 用 `parse_danbooru_tag` 把 tag 拆成 `(character, [sources])`：
  - `kiana_(honkai_impact_3rd)` → `("kiana", ["bh3"])`（前提：`source_suffixes` 配了 `bh3` → `honkai_impact_3rd`）
  - `unknown_character` → `("unknown_character", ["unknown"])`（无后缀或后缀未登记 → 整 tag 作 id，sources 落 `"unknown"`）
- 在 `entities/` 下创建占位 JSON（`display_name` 留空，待补）；
- 终端汇总会列出新建的占位 entity 路径，让你去补 `display_name` / `aliases`、必要时修 `sources`。

> [!WARNING]
> 出现 `sources: ["unknown"]` 时
>
  `source_suffixes` 还没配该作品。先在 §4.1 补上后缀，再修 entity 的 `sources` 为正确 id；批量修复历史残留可跑 `utils/fix_unknown_sources.py`。

---

## 识图与诊断

### `--dump-tags`：看模型实际输出

在动真格批量入库前，先用它看 character tag 决定要不要补 `tag_overrides`：

```powershell
# 单图诊断
.venv\Scripts\python.exe tag_image.py --dump-tags 那张图.jpg

# 放进 workspace/inbox/ 后批量诊断（递归扫整个目录）
.venv\Scripts\python.exe tag_image.py --dump-tags --character-threshold 0.5
```

不做匹配、不写 data/meta、不移动文件，只打印每张图的 character tag 及分数降序：

```text
[>] sample.jpg （共 3 个 character tag）
    ★ 已登记 entity：
      kiana_(honkai_impact_3rd) = 0.9523  →  Kiana
    ? 未登记但高置信（≥0.5）：
      mei_(honkai_impact_3rd) = 0.6234
```

- `★` 已命中 —— 你的 `source_suffixes` + entity 已覆盖它；
- `?` 未命中但分数挺高 —— 这就是你下一次该补的 entity 或 `tag_overrides`。

### 批量入库

```powershell
.venv\Scripts\python.exe tag_image.py
```

**智能压缩默认启用**（调色板量化 + Huffman 优化，二次元图体积减少 50-80%），
无需额外参数。若需原始质量：`--no-compress`。

可选参数：

| 参数                    | 默认                    | 说明                                                                |
| ----------------------- | ----------------------- | ------------------------------------------------------------------- |
| `--model`               | `EVA02_Large`           | WD14 模型名，见 [模型速查](#模型速查)                               |
| `--character-threshold` | `0.7`                   | WD14 character tag 置信度阈值；图多但模型不熟时降到 0.5             |
| `--general-threshold`   | `0.35`                  | 通用 tag 阈值，**不影响角色识别**，只影响模型内部裁剪               |
| `--fusion-threshold`    | `0.30`                  | 多源融合后最低置信度，低于此值归档到 unrecognized 或 pending_review |
| `--quality`             | `92`                    | 写入 `data/` 的 JPEG 质量 1-100                                     |
| `--no-compress`         | —                       | 禁用智能压缩，保留原始颜色（文件更大）                              |
| `--use-clip`            | —                       | 启用 CLIP 二次验证（需 `pip install open-clip-torch torch`）        |
| `--input`               | `tools/workspace/inbox` | 自定义输入目录                                                      |
| `--repo`                | 脚本上级目录            | 仓库根目录（`data/` `meta/` 写到该目录下）                          |
| `--dump-tags`           | —                       | 诊断模式，见下                                                      |

支持图片格式 `.jpg/.jpeg/.png/.webp/.bmp/.gif`，子目录递归扫描（结构镜像到 `workspace/processed/<category>/`）。

### 识别流程（多源融合）

```
                       图片
                         │
         ┌───────────────┼────────────────┐
         │               │                │
         ▼               ▼                ▼
       WD14          文件名解析        CLIP(可选)
    模型推理         拆 token         图文匹配
    输出 tag         查别名            语义验证
         │               │                │
         └───────────────┼────────────────┘
                         ▼
                AliasDetector 统一
            （tag/名称/昵称 → entity id）
                         │
                         ▼
                  Merger 加权融合
              wd14×1.0 + filename×0.9 + clip×0.7
                         │
                         ▼
                   最终 Entity
```

- **WD14**：ONNX 模型推理输出 danbooru character tag，是主要识别来源
- **文件名**：解析 Pixiv/Danbooru 风格文件名中的角色名（如 `爱莉希雅`、`Elysia`），是 WD14 失败时的兜底
- **别名统一**：`display_name`、`aliases`、`entity id`、danbooru tag 全部映射到同一个 canonical id
- **CLIP**（可选）：图文语义匹配，即使 WD14 只打出 `girl, pink_hair` 也能认出角色
- **融合**：多条证据互相印证时置信度更高，单源也能独立过关

主循环决策：

```
recognize_with_alternatives() → 候选列表
         │
         ├─ confidence ≥ 0.30  →  recognized（写入 data/meta）
         │   └─ 多角色？→ entities 数组写入多个
         │
         ├─ 有候选但 < 0.30    →  pending_review
         │
         ├─ 无候选 + WD14 有 tag →  自动创建 entity → 重新识别
         │
         └─ 完全无结果          →  unrecognized
```

### 运行日志

每次运行完整的控制台输出会自动保存到 `logs/tag-image-YYYYMMDD_HHMMSS.txt`，
UTF-8 编码，包含初始化信息、每张图的识别结果（含压缩比）、最终汇总。

### pending_review 回炉流程

1. `tag_image.py` 跑完后汇总：
   ```text
   自动创建了 3 个占位实体（display_name / aliases 待填写）：
     [NEW] entities/kiana.json
     [NEW] entities/mei.json
     [NEW] entities/bronya.json
   ```
2. 逐个打开这些占位文件，补字段：
   - 改 `display_name` 为人类可读名（之前空串）；
   - 补 `aliases` 给 Issue 投稿流程的 alias-map 用；
   - 修 `sources`：自动创建时若 `source_suffixes` 没配该后缀会落 `["unknown"]`，参考 §4.1 改对，或用 [`utils/fix_unknown_sources.py`](#清理-unknown-残留) 批量修；
   - 改 `id`：WD14 拆出的名字不理想时，重命名成干净 canonical id，并在 §4.2 配 `tag_overrides` 让原 tag 仍指向新 id。
3. 把 `workspace/processed/pending_review/` 下相关图片**移回 `workspace/inbox/`** 重跑 `tag_image.py`。

> 之所以要回炉而不是原地修，是因为 meta 文件上一轮可能已经用旧 id 写过了。回炉会以新 entity 重写一遍 meta + 重新归档。

## 手动分批入库

已经人工按角色分好图的，不必走 WD14 —— 更快、零误差，也是 WD14 完全没识别为角色图时的兜底入口。

### 在 `workspace/manual/` 下建角色名子目录

```text
tools/workspace/manual/
  琪亚娜/
    IMG_0001.JPG
  芽衣/
    IMG_0003.JPG
  琪亚娜×芽衣/        ← 多角色用全角 × 分隔，两人都入库
    IMG_0004.JPG
```

文件夹名按以下优先级匹配角色：

1. 直接命中某 entity 的 `display_name`；
2. 直接命中某 entity `id`（不区分大小写）；
3. 用 `×` 或两空格或单空格切片后，多段组合贪心匹配（最多 3 段合并为一角色）。

运行：

```powershell
.venv\Scripts\python.exe process_tmp.py --dry-run
.venv\Scripts\python.exe process_tmp.py
```

成功导入后会**自动调用 `node scripts/build-index.js`** 做 sanity check。

可选参数：`--input <目录>`（默认 `tools/workspace/manual/`）、`--repo`、`--quality`、`--dry-run`。

## 清理 unknown 残留

```powershell
.venv\Scripts\python.exe utils\fix_unknown_sources.py
```

对两类文件做兜底推导：

- `entities/*.json`：用 `parse_danbooru_tag(id)` 看 id 能不能拆出后缀反查到 source id；
- `meta/*.json`：用该 meta 的 `entities` 字段反查对应 entity 的 `sources` 并集。

跑完仍 `"unknown"` 的条目说明 id 不理想，需手改 entity id（见 [pending_review 回炉流程](#pending_review-回炉流程)）。

---

## 最终重建聚合索引

入库完（不管自动还是手动）后跑一次：

```powershell
# 仓库根目录
node scripts/build-index.js
```

`process_tmp.py` 已自动调它；其它入库流程（如 `tag_image.py`）只动 `data/` 和 `meta/`，需要你手动重建 `dist/`。这一步会全量重写：

- `dist/image-index.json` —— 主索引 + `sources` / `entities` 倒排表；
- `dist/entity-index.json` —— 角色字典正交视图；
- `dist/alias-map.json` —— 扁平 alias → canonical id 映射。

`build-index.js` 报 `must use canonical ids only` → 你有 meta 的 `entities` 字段里写了不存在于 `entities/` 的 id（多半是 §4.2 提示的 "override value 写错" 或 "占位 entity 被删了但 meta 还在引用它"）。回到 `workspace/processed/pending_review/` 把 entity 补齐再重建。

### 一致性校验

```powershell
# 仓库根目录
node scripts/check-consistency.js
```

独立于 `build-index.js` 的轻量校验脚本，检查三项：

1. **meta → data**：meta 引用的图片文件是否存在
2. **data → meta**：每张图片是否有对应 meta（孤儿图片检测）
3. **entities 合法性**：meta 中的 entities 是否都在 `entities/` 中注册

全部通过输出 `✓ 全部一致` 并以 exit 0 退出，发现问题以 exit 1 退出，适合 CI 流程。`build-index.js` 合入前跑一遍更安全。

---

## FAQ

#### 为什么图片进了 `workspace/processed/pending_review/`？

说明 WD14 检测到了角色 tag，但所有检测器融合后仍未达到置信度阈值（默认 0.30），
或者 tag 命中了未登记的 entity。处理流程见 [pending_review 回炉流程](#pending_review-回炉流程)。

1. 编辑占位 entity 补 `display_name` / `aliases` / 修正 `sources`；
2. 如还命中不到，确认 WD14 输出 tag 后在 `tag_overrides` 里加一条映射；
3. 把图移回 `workspace/inbox/` 重跑。

#### 为什么图片进了 `workspace/processed/unrecognized/`？

**所有检测器**（WD14 + 文件名 + 别名）都没有识别到任何已登记角色。可能原因：

- WD14 没打出 character tag，文件名也没有角色名 → 走[手动分批入库](#手动分批入库)；
- 角色在图中但不在 `entities/` 中 → 创建 entity 文件后重跑；
- 降 `--character-threshold 0.5` 或更低后 `--dump-tags` 诊断；
- 换模型（如 `--model SwinV2_v3`）；
- 启用 CLIP：`--use-clip`（需要额外依赖，对无 tag 的图有救场能力）。

#### 为什么 `sources` 落成 `["unknown"]`？

`source_suffixes` 还没配该作品的 danbooru copyright 后缀。先补 §4.1，再修 entity 的 `sources` 为正确 id，或跑 `utils/fix_unknown_sources.py` 批量修。

#### 为什么 `tag_overrides` 的 value 命中后 `build-index.js` 报错？

那个 value 必须是已登记 canonical id。否则 meta 写了不存在的 id、`build-index.js` 校验失败。

#### 控制台中文乱码（仅 Windows）

`tag_image.py` / `process_tmp.py` 启动时已自动 `SetConsoleCP(65001)`，正常情况无需手动处理。极端情况下被某些 shell 套娃会破，追加：

```powershell
$env:PYTHONIOENCODING="utf-8"
chcp 65001
```

## 模型速查

WD14 是 SmilingWolf 在 HuggingFace 托管的一组 anime character tagger，本套工具已绕开 `huggingface_hub` 的网络坑，用 `requests` 流式 + 断点续传直下到 `.hf-cache/`。

| 模型                                                         | 大小    | 速度 | 精度   | 适用                         |
| ------------------------------------------------------------ | ------- | ---- | ------ | ---------------------------- |
| **EVA02_Large** ⭐                                            | 1.26 GB | 较慢 | 最高   | 追求识别率，有独显           |
| SwinV2_v3                                                    | 467 MB  | 快   | 良好   | 日常使用，入门首选           |
| PixAI_v09                                                    | 659 MB  | 中   | 通用强 | 对杂物画风容忍度好           |
| ConvNext_v3 / ViT_v3                                         | —       | 快   | 良好   | EVA02_Large 之外的小 v3 备选 |
| 其它（SwinV2 / ConvNext / ConvNextV2 / ViT / MOAT 等 v2 系） | —       | —    | —      | 旧版兼容，一般不选           |

`download_models.py` 完整 `--model` 选择：`EVA02_Large` / `SwinV2_v3` / `ConvNext_v3` / `ViT_v3` / `PixAI_v09` / `SwinV2` / `ConvNext` / `ConvNextV2` / `ViT` / `MOAT`。

下载完成后 `tools/.hf-cache/<模型名小写>/` 下应有 `model.onnx` + `selected_tags.csv`，v2 系还会多一个 `inv.npz`。

| 你想做的事                              | 推荐组合                                                             |
| --------------------------------------- | -------------------------------------------------------------------- |
| 日常批量入库（有独显）                  | `tag_image.py`（默认压缩 + EVA02_Large）                             |
| 日常批量入库（无独显）                  | `tag_image.py --model SwinV2_v3`                                     |
| 保留原始画质                            | `tag_image.py --no-compress`                                         |
| 启用 CLIP 救场无 tag 图                 | `tag_image.py --use-clip`                                            |
| 图多但模型不熟该 fandom                 | 加 `--character-threshold 0.5` 降阈值                                |
| 只想要高置信入库、宁可多 pending_review | 加 `--fusion-threshold 0.5`                                          |
| 看 WD14 实际输出调整 `tag_overrides`    | `tag_image.py --dump-tags`（单图或扫 workspace/inbox）               |
| 配合 build-index 校验                   | `tag_image.py` 之后必须 `node scripts/build-index.js`                |
| 手动已分好角色、不走 AI                 | `process_tmp.py --dry-run` 然后 `process_tmp.py`（内置 build-index） |

阈值默认 0.7 是经验值：SmilingWolf v3 系模型的 character 分支在这个阈值上准确率与召回比较平衡，按浮现的 `pending_review` 噪音情况微调。
