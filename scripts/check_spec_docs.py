"""交付文档终检驱动：Mermaid 语法校验 + 机械 lint。

存在的理由：这两项检查的脚本都住在 skills 目录里，而文档在**中文路径**下。
中文路径不要经 shell 传参（Windows 会转码乱码），所以统一由本脚本用
`subprocess.run([...])` 传 argv，并显式指定 `encoding='utf-8'`。

    # 校验交付目录下所有 V* 文档（默认行为）
    python scripts/check_spec_docs.py

    # 只校验指定文件
    python scripts/check_spec_docs.py D12性能优化与稳定性加固实施报告_V1.1.0.md

退出码 0 = 全部通过；1 = 有失败（逐份打印 stderr 便于定位）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PY = r"C:\Users\zzz\.workbuddy\binaries\python\versions\3.13.12\python.exe"
NODE = r"C:\Users\zzz\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
MERMAID = r"C:\Users\zzz\.workbuddy\skills\mermaid-validate\scripts\check.mjs"
LINT = r"C:\Users\zzz\.workbuddy\skills\spec-doc-audit\scripts\lint_md.py"

DOC_DIR = Path(r"D:\新建文件夹\blogs\双人对话播客自动生成系统")


def run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout or "", p.stderr or ""


def main(argv: list[str]) -> int:
    if argv:
        files = [Path(a) if Path(a).is_absolute() else DOC_DIR / a for a in argv]
    else:
        # 只校验「当前版」（交付目录顶层的 *.md）；archive/ 里的历史版不必重复校验
        files = sorted(DOC_DIR.glob("*.md"))

    if not files:
        print("没有找到待校验文档")
        return 1

    failed: list[str] = []
    for f in files:
        if not f.exists():
            print(f"[skip] {f.name}: 不存在")
            continue
        print(f"\n=== {f.name} ===")
        rc_m, out_m, err_m = run([NODE, MERMAID, str(f)])
        print(f"  mermaid: {'PASS' if rc_m == 0 else 'FAIL'}")
        if out_m.strip():
            print("    " + out_m.strip().replace("\n", "\n    "))
        if rc_m != 0 and err_m.strip():
            print("    " + err_m.strip().replace("\n", "\n    "))
        rc_l, out_l, err_l = run([PY, LINT, str(f)])
        print(f"  lint   : {'PASS' if rc_l == 0 else 'FAIL'}")
        if out_l.strip():
            print("    " + out_l.strip().replace("\n", "\n    "))
        if rc_l != 0 and err_l.strip():
            print("    " + err_l.strip().replace("\n", "\n    "))
        if rc_m != 0 or rc_l != 0:
            failed.append(f.name)

    print("\n" + "=" * 48)
    if failed:
        print(f"结果：{len(failed)} 份未通过 -> {failed}")
        return 1
    print(f"结果：{len(files)} 份全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
