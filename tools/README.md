# 图片收录工具集

两种工作方式互补：**手动分类**（精准，推荐）和 **WD14 自动识别**（批量，适合未筛选图片）

## 目录结构

```
tools/
  .venv/           虚拟环境（.gitignore 已排除）
  .hf-cache/       下载的模型权重（.gitignore 已排除）
  manual/          手动分类图片（按角色名创建子文件夹）
  inbox/           把要识别的图丢进这里
  processed/       源图处理后按结果归档：
    recognized/     识别成功 → 已写入 data/ + meta/
    pending_review/ 检测到角色但 entity 未登记 → 留待人工审核
    unrecognized/   未检测到任何角色 → 需人工分类
    duplicate/      与已有图 sha256 重复 → 已跳过
  process_tmp.py           手动分类图片导入脚本
  tag_image.py             主工具：WD14 自动识别
  entities_db.py           加载 entities/ 构建 tag → ID 映射
  download_models.py       下载模型（仅初次运行）
  requirements.txt         Python 依赖
```

---

## 方式一：手动分类导入（推荐）

适合已有人工筛选的图片，精准无遗漏

### 第一步：创建角色文件夹

在 `tools/manual/` 下创建以角色名命名的文件夹，放入对应图片：

```
tools/manual/
  流萤/
    IMG_0001.JPG
    IMG_0002.PNG
  阮梅/
    IMG_0003.JPG
  菈乌玛×奈芙尔/     ← 多角色用 × 分隔
    IMG_0004.JPG
```

文件夹名匹配 `entities/` 中的 `display_name` 或 `id`

### 第二步：创建实体（如需）

如果角色不在 `entities/` 中，新建 JSON 文件：

```json
{
  "id": "Firefly",
  "display_name": "流萤",
  "games": ["hsr"],
  "aliases": ["萨姆"]
}
```

### 第三步：运行导入

```powershell
# 预览（不实际写入）
python tools/process_tmp.py --dry-run

# 正式导入
python tools/process_tmp.py
```

> 也支持 `--input <目录>` 指定其他输入目录

---

## 方式二：WD14 自动识别

适合批量未分类图片，由 AI 自动识别角色

### 第一步：创建虚拟环境

```powershell
cd tools
python -m venv .venv
```

### 第二步：安装依赖

根据你的显卡选择：

**🎮 GPU 方案**（NVIDIA GTX 10 系及以上，速度提升 30-50%）

直接安装，`tag_image.py` 启动时会自动发现 CUDA 运行时 DLL（pip 版，无需装 CUDA Toolkit）：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**🖥️ CPU 方案**（无 NVIDIA 显卡或不在意速度）

编辑 `requirements.txt`，把 `onnxruntime-gpu` 和 `nvidia-*` 注释掉，换上 `onnxruntime`：

```
# onnxruntime-gpu>=1.17.0          ← 注释掉
# nvidia-cublas-cu12               ← 注释掉
# nvidia-cudnn-cu12                ← 注释掉
# nvidia-cuda-runtime-cu12         ← 注释掉
onnxruntime>=1.17.0                ← 取消注释
```

然后安装：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 第三步：下载模型（仅初次）

推荐两个模型，按需选一个：

| 模型              | 大小    | 速度 | 精度 | 适用场景           |
| ----------------- | ------- | ---- | ---- | ------------------ |
| **EVA02_Large** ⭐ | 1.26 GB | 较慢 | 最高 | 追求识别率，有独显 |
| SwinV2_v3         | 467 MB  | 快   | 良好 | 日常使用，入门首选 |

```powershell
# 下载推荐模型（默认 EVA02_Large）
.venv\Scripts\python.exe download_models.py

# 或指定快速模型
.venv\Scripts\python.exe download_models.py --model SwinV2_v3
```

### 第四步：运行识别

把待分类的图片丢进 `inbox/`，然后：

```powershell
.venv\Scripts\python.exe tag_image.py
```

可选参数：

| 参数                    | 默认值        | 说明                                       |
| ----------------------- | ------------- | ------------------------------------------ |
| `--model`               | `EVA02_Large` | 模型名（推荐 `EVA02_Large` / `SwinV2_v3`） |
| `--character-threshold` | `0.7`         | 角色标签置信度阈值                         |
| `--quality`             | `92`          | 输出 JPEG 质量 (1-100)                     |
| `--input`               | `tools/inbox` | 自定义输入目录                             |
| `--dump-tags`           | —             | 仅输出模型原始标签，不做匹配（诊断用）     |

### 第五步：查看结果

| 目录                               | 含义                                                          |
| ---------------------------------- | ------------------------------------------------------------- |
| `data/{id}.jpg` + `meta/{id}.json` | 识别成功，已入库                                              |
| `processed/pending_review/`        | 模型检测到角色但 entity 未登记，补全 entity 后放回 inbox 重跑 |
| `processed/unrecognized/`          | 模型完全未检测到角色（非角色图/截图/表情包等）                |
| `processed/duplicate/`             | 与已有图重复，自动跳过                                        |

## 扩展可识别角色

新建或编辑 `entities/<角色>.json`：

```json
{
  "id": "YaeMiko",
  "display_name": "八重神子",
  "games": ["genshin"],
  "aliases": ["狐狸精"]
}
```

`id` 和 `games` 由 `entities_db.py` 自动拼成 Danbooru character tag 候选（如 `yae_miko_(genshin_impact)`），无需手动填写 `danbooru_tags`如需覆盖自动生成的 tag，可在 `danbooru_tag_map.json` 中添加映射

## 依赖

| 组件                  | PyPI 名           | 说明                                             |
| --------------------- | ----------------- | ------------------------------------------------ |
| dghs-imgutils         | `dghs-imgutils`   | deepghs/imgutils（非 PyPI 上 9KB 的 `imgutils`） |
| Pillow                | `Pillow`          | 图片读取与 JPEG 压缩                             |
| onnxruntime-gpu       | `onnxruntime-gpu` | GPU 推理（CPU 用 `onnxruntime`）                 |
| nvidia-cublas-cu12 等 | pip 包            | CUDA 12 运行时（无需装 Toolkit）                 |

## 常见问题

### Q: 下载模型时报代理错误 / SSL 错误

设置系统代理后运行下载命令：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:6789"
$env:HTTPS_PROXY="http://127.0.0.1:6789"
.venv\Scripts\python.exe download_models.py
```

模型较大（500MB-1.2GB），下载时请保持代理稳定若反复中断，可直接用浏览器下载 `model.onnx` 和 `selected_tags.csv` 手动放入 `.hf-cache/<模型名>/` 目录

### Q: 控制台输出中文乱码

在运行 `tag_image.py` 前设置：

```powershell
$env:PYTHONIOENCODING="utf-8"
```

或在 PowerShell 中运行 `chcp 65001` 切换到 UTF-8 代码页

### Q: 提示 `cublasLt64_12.dll missing`

`onnxruntime-gpu` 依赖 CUDA 12 运行时`requirements.txt` 已包含 `nvidia-cublas-cu12` 等 pip 包，`tag_image.py` 启动时会自动发现这些 DLL若仍报错，确认已正确安装 GPU 方案的依赖

纯 CPU 环境请改用 `onnxruntime`（见上方 CPU 方案）

### Q: 识别率太低

1. 先用 `--dump-tags` 诊断模型是否检测到了角色：
   ```powershell
   .venv\Scripts\python.exe tag_image.py --dump-tags --character-threshold 0.5
   ```
2. 如果完全没有 character tag 输出 → 图片可能不是动漫角色插画（截图/表情包/真人照片等），WD14 系列模型无法处理
3. 如果模型检测到了但无 entity 匹配 → 归档在 `pending_review/`，补全该角色 entity 后放回 inbox 重跑
4. 试试更强的 EVA02_Large 模型

### Q: imgutils 版本兼容性

`imgutils 0.19` 的 `get_wd14_tags` 写死期望旧版双输出格式，与当前 v3 模型不兼容本工具已自带本地 ONNX 推理绕开此问题下载模型时也用 `requests` 流式下载替代 `hf_hub_download`，避免代理丢头导致下载失败
