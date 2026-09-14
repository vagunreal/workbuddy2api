#!/usr/bin/env python3
"""回归测试：脱敏模块不得删掉工作区指令（AGENTS.md / CLAUDE.md）。

背景
----
ZCode CLI 与 Claude Code 把工作区指令放在 `<system-reminder>` 包裹的 user 消息里。
早先版本的 _HARNESS_USER_MARKERS 含 `<system-reminder>`，`--desensitize`（默认压缩模式）
会把这类消息**整条**替换成一句占位符，模型完全看不到规则：表现为代理后面的 agent 不遵守
AGENTS.md，Windows 上不用 `pwsh` 而用 `powershell` 5.1。`--no-compact` 也救不了，因为
`<system-reminder>` 同时列在 _RUNTIME_BLOCK_REPLACEMENTS 里。

本测试锁死修复后的行为：
  1. `<system-reminder>` 里的正文在两种模式下都逐字保留
  2. system 消息仍做零宽脱敏（脱敏功能本身没被削弱）
  3. Codex CLI 的运行时块仍被压缩（原作者的目标场景没回归）
  4. tools 的 description 剥离策略未变

运行: python3 tests/test_desensitize_harness.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.desensitize import desensitize_body, desensitize_text  # noqa: E402

ZWSP = "\u200b"

# 模拟 ZCode CLI 实际发出的一条工作区指令消息（结构取自真实请求）
AGENTS_BODY = (
    "<system-reminder>\n"
    "As you answer the user's questions, you can use the following context:\n"
    "# agentsMd\n"
    "Codebase and user instructions are shown below. Be sure to adhere to these instructions. "
    "IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.\n"
    "## 1. Shell 与终端默认规范\n"
    "- **默认 Shell**：默认优先使用 **PowerShell 7 (`pwsh`)**。\n"
    "</system-reminder>"
)


def _run(messages, **kw):
    """走一遍转换器实际调用的入口，返回处理后的 messages。"""
    body = {"model": "x", "messages": messages, "stream": True}
    out = desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=kw.pop("desensitize_harness_user", True),
        desensitize_tools=kw.pop("desensitize_tools", True),
        compact_harness=kw.pop("compact_harness", True),
        strip_tool_metadata=kw.pop("strip_tool_metadata", True),
    )
    return out["messages"]


def test_system_reminder_survives_both_modes():
    """核心回归：工作区指令在默认压缩模式与 --no-compact 模式下都不能被删。"""
    msgs = [
        {"role": "system", "content": "You are ZCode. Refuse DoS attacks."},
        {"role": "user", "content": AGENTS_BODY},
        {"role": "user", "content": "调用一下ps"},
    ]
    for mode, compact in (("默认（压缩模式）", True), ("--no-compact", False)):
        out = _run(msgs, compact_harness=compact)
        got = out[1]["content"]
        assert "IMPORTANT: These instructions OVERRIDE" in got, f"{mode}: 指令正文被删了"
        assert "PowerShell 7 (`pwsh`)" in got, f"{mode}: pwsh 规范丢了"
        assert "Repository instructions and environment context are provided" not in got, \
            f"{mode}: 又出现占位符了"
        assert "Runtime reminder context is provided by the harness" not in got, \
            f"{mode}: system-reminder 整块被替换了"
        assert got.count("</system-reminder>") == 1, f"{mode}: system-reminder 结构被破坏"
        assert out[2]["content"] == "调用一下ps", f"{mode}: 用户原话被改动"
        print(f"  ✓ {mode}: 工作区指令逐字保留（{len(got)} 字符）")


def test_system_messages_still_desensitized():
    """脱敏功能本身不能退化：system 里的敏感词仍要插入零宽字符。"""
    msgs = [
        {"role": "system", "content": "Refuse requests for DoS attacks and exploit development."},
        {"role": "user", "content": "hello"},
    ]
    out = _run(msgs)
    c = out[0]["content"]
    assert ZWSP in c, "system 消息没有被脱敏"
    assert c.replace(ZWSP, "") == msgs[0]["content"], "脱敏只应插入不可见字符，不应改变可见内容"
    assert out[1]["content"] == "hello", "普通 user 消息不应被改动"
    print(f"  ✓ system 仍脱敏，且可见内容不变（插入 {c.count(ZWSP)} 处零宽字符）")


def test_plain_user_text_untouched():
    """用户真实输入不受影响，哪怕里面出现敏感词。"""
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "explain DoS attacks to me"},
    ]
    out = _run(msgs)
    assert ZWSP not in out[1]["content"], "真实用户输入被脱敏了"
    assert out[1]["content"] == "explain DoS attacks to me"
    print("  ✓ 真实用户输入保持原样")


def test_codex_runtime_blocks_still_compacted():
    """Codex CLI 路径没回归：运行时块仍被压成短摘要。"""
    msgs = [{
        "role": "user",
        "content": (
            "# AGENTS.md instructions\n\nKeep the change small.\n\n"
            "<environment_context>\n  <cwd>/repo</cwd>\n  <sandbox>workspace-write</sandbox>\n"
            "</environment_context>"
        ),
    }]
    out = _run(msgs)
    got = out[0]["content"]
    assert len(got) < 400, f"Codex 运行时块没有被压缩，长度 {len(got)}"
    assert "<sandbox>" not in got, "Codex 的 sandbox 元数据应被摘要替换"
    print(f"  ✓ Codex 运行时块仍被压缩为短摘要（{len(got)} 字符）")


def test_tool_descriptions_still_stripped():
    """tools 的 description 剥离策略未变（参数 schema 保留）。"""
    tools = [{"type": "function", "function": {
        "name": "Bash", "description": "run exploits", "parameters": {"type": "object"}}}]
    out = desensitize_body({"messages": [{"role": "user", "content": "hi"}], "tools": tools},
                           desensitize_tools=True, strip_tool_metadata=True)
    fn = out["tools"][0]["function"]
    assert "description" not in fn, "description 应被剥离"
    assert fn.get("parameters"), "参数 schema 必须保留"
    print("  ✓ tools description 剥离、参数 schema 保留")


def test_zero_width_helper_keeps_visible_text():
    """零宽脱敏只插不可见字符。"""
    d = desensitize_text("Refuse DoS attacks.")
    assert ZWSP in d and d.replace(ZWSP, "") == "Refuse DoS attacks."
    assert desensitize_text("正常中文，无触发词。") == "正常中文，无触发词。"
    print("  ✓ 零宽脱敏只插入不可见字符，无触发词时原样返回")


def test_instruction_marker_beats_other_harness_markers():
    """保险：即便消息同时带 Codex 标签，只要含工作区指令标记就不能被整条替换。"""
    msgs = [{
        "role": "user",
        "content": (
            "# agentsMd\n"
            "IMPORTANT: These instructions OVERRIDE any default behavior.\n"
            "<environment_context> sandbox=workspace-write"
        ),
    }]
    for mode, compact in (("压缩模式", True), ("--no-compact", False)):
        got = _run(msgs, compact_harness=compact)[0]["content"]
        assert "IMPORTANT: These instructions OVERRIDE" in got, f"{mode}: 指令被替换了"
        assert "provided" not in got or "OVERRIDE" in got
        print(f"  ✓ {mode}: 指令标记优先于其它 harness 标签")


if __name__ == "__main__":
    tests = [
        test_system_reminder_survives_both_modes,
        test_instruction_marker_beats_other_harness_markers,
        test_system_messages_still_desensitized,
        test_plain_user_text_untouched,
        test_codex_runtime_blocks_still_compacted,
        test_tool_descriptions_still_stripped,
        test_zero_width_helper_keeps_visible_text,
    ]
    print("=== test_desensitize_harness ===")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
    print(f"\n{'全部通过' if not failed else str(failed) + ' 个失败'}（共 {len(tests)} 项）")
    sys.exit(1 if failed else 0)
