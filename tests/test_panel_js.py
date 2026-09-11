#!/usr/bin/env python3
"""面板 JS 质量门:语法检查 + DOM stub 运行时冒烟。

需要系统安装 node(apt install nodejs)。用途:每次改动 PANEL_HTML 后运行,
防止 Python 转义把 JS 写坏(单引号串断裂等)导致整页按钮失效。
运行: .venv/bin/python test_panel_js.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STUB = """
const els = {};
const mkEl = () => ({ innerHTML:'', textContent:'', value:'', style:{}, disabled:false,
  classList:{toggle(){},add(){},remove(){}}, dataset:{}, querySelectorAll:()=>[], options:[],
  addEventListener(){} });
global.document = {
  getElementById: id => els[id] || (els[id] = mkEl()),
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.window = global;
global.localStorage = { getItem:()=>null, setItem:()=>{} };
global.fetch = async () => ({ json: async () => ({accounts:[], models:[], probe_state:{}, api:{base_url:'x',auth_enabled:false}}) });
global.navigator = { clipboard: { writeText(){} } };
global.alert = ()=>{}; global.setInterval = ()=>{}; global.setTimeout = ()=>{};
const src = require('fs').readFileSync(process.argv[2],'utf8');
eval(src);
for (const fn of ['showPage','renderApi','loadModels','refreshModels','render','load','copyTxt']) {
  if (typeof global[fn] !== 'function' && typeof eval('globalThis.' + fn) !== 'function' && typeof eval(fn) !== 'function')
    throw new Error('函数缺失: ' + fn);
}
showPage('models'); showPage('api'); renderApi();
console.log('SMOKE_OK');
"""


def main():
    import converter
    from fastapi.testclient import TestClient

    converter.CONFIG["pool"] = None   # 面板静态部分不依赖凭据
    html = TestClient(converter.app).get("/panel").text

    # 结构断言
    assert "showPage(" in html, "页签绑定缺失"
    assert 'class="ver"' not in html, "不应显示版本号"

    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "未找到 script"
    js = m.group(1)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        jf = f.name

    # 1) 语法检查
    r = subprocess.run(["node", "--check", jf], capture_output=True, text=True)
    if r.returncode != 0:
        print("❌ JS 语法错误:\n", r.stderr)
        sys.exit(1)
    print("✅ node --check 语法通过")

    # 2) DOM stub 运行时冒烟
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(STUB)
        sf = f.name
    r = subprocess.run(["node", sf, jf], capture_output=True, text=True)
    if r.returncode != 0 or "SMOKE_OK" not in r.stdout:
        print("❌ 运行时冒烟失败:\n", r.stderr, r.stdout)
        sys.exit(1)
    print("✅ 运行时冒烟通过(页签/接口渲染/全部按钮函数就绪)")
    print("🎉 面板 JS 质量门通过")


if __name__ == "__main__":
    main()
