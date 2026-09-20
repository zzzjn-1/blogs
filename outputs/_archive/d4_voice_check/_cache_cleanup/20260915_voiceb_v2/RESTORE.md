# 缓存清理还原说明

本批次由 D4 前置检查（voice_b 第 2 版建档）产生，**仅搬移未删除**。

## 分组口径

| 组 | 说明 |
| --- | --- |
| R_测试残留 | 键非 40 位 sha1，由验证脚本的自定义 text_hash 写入（70 条）|
| S_陈旧或错标 | 键为 sha1，但 speaker 为 B（voice_b 已换代）或为 "1"（曾被静默当 A）（19 条）|
| L_旧公式遗留 | 键为 sha1 但属旧 cache_key 公式；[PATCH-CACHE-01] 后已不可命中（14 条）|

## 还原

```bash
cd "D:\podcast-ai\outputs\d4_voice_check\_cache_cleanup\20260915_voiceb_v2"
python - <<'PY'
import json, shutil
from pathlib import Path
batch = Path(r'D:\podcast-ai\outputs\d4_voice_check\_cache_cleanup\20260915_voiceb_v2')
root = Path(json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))['cache_path'])
for item in json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))['moved']:
    for ext in ('.wav', '.json'):
        src = batch / item['group'] / Path(item['rel']).with_suffix(ext)
        if src.is_file():
            dst = root / Path(item['rel']).with_suffix(ext)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
print('restored')
PY
```
