"""快速冒烟测试：验证 recognizer 模块的别名解析和文件名检测。"""
from pathlib import Path
from recognizer.alias_detector import AliasDetector
from recognizer.filename_detector import FilenameDetector

entities_dir = Path(__file__).resolve().parent.parent / "entities"

# 1. 别名检测器
print("=== AliasDetector ===")
detector = AliasDetector(entities_dir)
print(f"Loaded {detector.entity_count} entities")

for text in ["爱莉希雅", "Elysia", "爱莉", "粉色妖精小姐", "elysia_(honkai_impact)", "琪亚娜", "芙宁娜", "雷电将军"]:
    candidates = detector.resolve_alias(text)
    if candidates:
        c = candidates[0]
        print(f'  "{text}" -> {c.entity} (score={c.score}, source={c.source})')
    else:
        print(f'  "{text}" -> NO MATCH')

# 2. 文件名检测器
print("\n=== FilenameDetector ===")
fd = FilenameDetector(detector)

for fname in [
    "147081776_p0-爱莉爱2-AI生成,美少女,崩坏3,爱莉希雅,婚纱.png",
    "Elysia_01.jpg",
    "芙宁娜-水神.png",
    "random_image.png",
]:
    cands = fd.detect(fname)
    if cands:
        for c in cands:
            print(f'  "{fname}" -> {c.entity} score={c.score} evidence={c.evidence}')
    else:
        print(f'  "{fname}" -> NO MATCH')

print("\n=== All tests passed ===")
