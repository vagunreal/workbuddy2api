# 更新说明（CHANGELOG）

本项目使用 `V主版本.次版本` 标记版本，每个版本对应一个 git 标签：

| 版本 | git 标签 | 提交 | 状态 |
|------|----------|------|------|
| V1.1 | `V1.1` | 当前 `main` | **当前版本**，含工作区指令透传修复 |
| V1 | `V1` | `14171ae` | 初始版本（与 origin/main 同步） |

运行时可用 `GET /health` 查看当前版本，`version` 字段即标签名。

查看/切换某个版本：

```bash
git tag -l                        # 列出所有版本
git rev-parse V1.1                # 看某版本指向哪个提交
git show V1                       # 看某版本说明
git checkout V1                   # 切回初始版本（看完用 git checkout main 回来）
```

---

## V1.1

修复代理会把工作区指令（AGENTS.md / CLAUDE.md）删掉的问题。

### 问题

开启 `--desensitize` 时，`core/desensitize.py` 把所有含 `<system-reminder>` 的 user 消息
判定为"可压缩的 harness 运行时上下文"，**整条**替换成一句占位符：

```
Repository instructions and environment context are provided. Follow repository guidance
while answering the user's actual request.
```

但 ZCode CLI 与 Claude Code 恰好把**工作区指令**放在这类消息里（AGENTS.md、CLAUDE.md、
记忆索引）。实测 ZCode 发往 `/v1/chat/completions` 的请求中，14007 字符的
"两份 AGENTS.md + MEMORY.md 索引"被替换成 131 字符。

**症状**：经本代理接入的 agent 读不到项目规则 —— 不遵守 AGENTS.md，Windows 上使用
`powershell` 5.1 或 `cmd` 而不是 `pwsh`；排查时模型甚至会得出"AGENTS.md 没有注入到我的
上下文"的结论（这个结论是对的）。

**为什么 `--no-compact` 不管用**：`<system-reminder>` 同时也登记在
`_RUNTIME_BLOCK_REPLACEMENTS` 里，该模式会把它整块换成 52 字符的
"Runtime reminder context is provided by the harness."，指令照样丢。

**影响范围**：仅 `--desensitize` 开启时；走 `/v1/responses` 的 Codex CLI 路径另有一套
投影逻辑（`core/responses_projection.py`），本版未改动。

### 改动

`core/desensitize.py`：

1. `_HARNESS_USER_MARKERS` 移除 `<system-reminder>` 与 `# claudeMd` —— 这两个标记承载的是
   指令，不是可丢弃的运行时元数据。
2. `_RUNTIME_BLOCK_REPLACEMENTS` 移除 `<system-reminder>` 块替换。
3. 新增 `_INSTRUCTION_MARKERS`（`# agentsMd`、`# claudeMd`、
   `IMPORTANT: These instructions OVERRIDE`）作为兜底守卫：带这些标记的消息在两条压缩
   路径上都不允许被整条替换，防止以后新增 harness 标记时又误伤指令。
4. `_compact_harness_message` 的 user 分支恢复原措辞，仅对**不带**指令标记的 Codex /
   Claude Code 运行时上下文生效。

另外：

- `converter.py`：新增 `PROJECT_VERSION`，`/health` 与启动预检输出中展示版本号。
- `tests/test_desensitize_harness.py`（新增）：7 项回归断言，锁死"工作区指令在压缩模式与
  `--no-compact` 下都逐字保留"，同时确认 system 脱敏、Codex 运行时块压缩、tools 描述剥离
  三项原有行为没有回归。
- `README.md`：补充脱敏范围说明。

### 行为对照

| 项目 | V1 | V1.1 |
|------|----|------|
| `<system-reminder>` 里的 AGENTS.md / CLAUDE.md | 整条替换成占位符 | 原样透传 |
| system / developer 消息零宽脱敏 | 是 | 是（未变） |
| Codex `<environment_context>` 等运行时块压缩 | 是 | 是（措辞与 V1 逐字一致） |
| tools `description` 剥离（参数 schema 保留） | 是 | 是（未变） |
| `GET /health` 显示版本号 | 无 | `"version": "V1.1"` |

### 验证

- **真实请求回归**：用抓取到的实际请求（`~/.zcode/cli/rollout/model-io-sess_075c1888*.jsonl`）
  回归，修复后 msg[4] 与 ZCode 发出的原文**逐字相同**（14007 = 14007），两份 AGENTS.md 与
  记忆索引均在；system 消息仍含零宽字符。
- **端到端 A/B**：把常量藏在 `<system-reminder>` 里发往 8787 —— 修复前模型答不出该常量
  （只回 `MARKER_CODE`），修复后正确复述其值。
- **行为验收**：原样重放当时那条"调用一下ps"请求（6 条消息 + 53 个工具完全一致）——
  修复前输出 `ps` → `tasklist` → `powershell -NoProfile`；修复后输出
  `pwsh -NoProfile -Command "..."`。
- **测试套件**：`tests/` 全绿（desensitize_harness 7、responses_adapter 15、
  anthropic_adapter 13、panel_js 通过）。

### 升级步骤

```bash
cd ~/workbuddy2api
git fetch --tags && git checkout V1.1
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart workbuddy2api
curl -s http://127.0.0.1:8787/health | grep -o '"version":"[^"]*"'   # 应输出 "version":"V1.1"
```

> 改了 `core/desensitize.py` 必须重启服务，否则进程里跑的还是旧代码。
> 已经开着的 ZCode 会话下一轮就会带上 AGENTS.md，但该模型此前已得出过"规则没注入"的结论，
> 建议新开会话观察。

### 回滚

```bash
git checkout V1
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart workbuddy2api
```

### 已知限制（V1.1 未处理）

1. **tools 描述仍被清空**：`--desensitize` 会剥离全部工具的 `description`（参数 schema 保留）。
   这是有意为之 —— Bash 工具描述内含 `detection evasion for malicious purposes`，命中敏感词表。
   如需保留，把调用处的 `strip_tool_metadata` 传 `False`。
2. **敏感词表缺词边界，会误伤 `skill`**：`kill` 命中 `skill`/`SKILL`，技能提醒消息一次请求
   被插入 59 处零宽字符（可见内容不变，但多耗 token）。不能简单加 `\b` —— CJK 也属于 `\w`，
   加了之后 `DoS攻击` 这类连写会匹配不到；若要修需改成只看左侧的 `(?<![A-Za-z])`，
   但会让 `cyberattacks` 之类漏网，属取舍。
3. **`core/responses_projection.py` 有同类标记列表**（`<system-reminder>`、`# claudeMd`），
   影响 `/v1/responses`（Codex CLI）路径。ZCode 走 `chat/completions`，不受影响。

---

## V1

初始版本。CodeBuddy → OpenAI 兼容转换器，支持 OpenAI Chat / OpenAI Responses /
Anthropic Messages 三协议，多账号池 failover，自带可视化面板（`/panel`）与每日签到。
`v2/chat/completions` 等 `/v2` 前缀作为别名路由兼容习惯用法。

已知问题见 V1.1 的「问题」一节。
