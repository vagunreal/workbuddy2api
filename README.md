# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的登录凭据，转成本机可直接使用的 **OpenAI / Anthropic 兼容 API**，并内置：

- **多账号池**：多个账号聚合为一个服务，额度用尽自动切换下一个
- **可视化面板**：浏览器查看每个账号的积分额度、签到状态，支持手动切换当前账号
- **每日自动签到**：启动时自动为账号池内所有账号签到领积分

适用客户端：Codex CLI（`/v1/responses`）、Claude Code / CC Switch（`/v1/messages`）、Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI 等（`/v1/chat/completions`）。

> 来源：基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 扩展多账号池与面板能力。上游接口（`copilot.tencent.com` / `codebuddy.cn` 的 `/v2/*`）属非公开逆向接口，无稳定性承诺，仅供个人学习研究，请自担风险。

---

## 功能总览

| 功能 | 说明 |
|------|------|
| 多账号池 | 自动扫描本机所有凭据文件（WSL + Windows 挂载目录），按账号 uid 去重后入池 |
| 粘性调度 | 正常时一直使用当前账号；某账号失败才切换，成功后粘住 |
| 故障自动切换 | 遇 HTTP 401/402/403/429 或错误文本含"额度/余额/配额/quota"等关键词时，该账号冷却 30 分钟，自动切到下一个健康账号 |
| 手动切换 | 面板点击"设为当前使用"，或调用 `POST /v1/account-switch` |
| 兜底硬试 | 所有账号都在冷却时仍按顺序重试（额度可能已恢复），失败才透传错误 |
| 可视化面板 | `GET /panel`：账号卡片、积分资源包进度条、签到状态、30s 自动刷新 |
| 每日签到 | `checkin.py` 遍历所有账号签到领积分，结果写入 `checkin_state.json` 供面板展示 |
| token 自动刷新 | 每个账号临近过期自动调 `/v2/plugin/auth/token/refresh` 刷新并原子回写凭据文件 |
| 三协议兼容 | OpenAI Chat / OpenAI Responses / Anthropic Messages，均支持流式与工具调用 |
| 脱敏 | `--desensitize` 对 system 提示词做零宽字符脱敏 + 压缩，缓解后端内容审核误拦；**不含工作区指令**——`<system-reminder>` 里的 AGENTS.md / CLAUDE.md 原样透传 |
| 模型管理 | 自动从上游发现可用模型；每个模型可编辑参数规格(上下文窗口/最大输出/输入输出类型)；自定义模型与别名；一键可用性探测 |

---

## 快速开始

> 项目可解压/克隆到**任意目录**运行：所有脚本（`start.sh` / `login.sh`）均以脚本自身位置定位，
> 运行时文件（`checkin_state.json`、日志）也生成在项目目录内，不依赖固定安装路径。
> 唯二的例外是"凭据目录"（由 WorkBuddy 桌面端决定，可用 `CODEBUDDY_AUTH_DIR` 改）和
> systemd 的 `ExecStart`（systemd 要求绝对路径，见下文模板注释）。

### 1. 安装依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖仅三个：`fastapi`、`uvicorn[standard]`、`httpx`（Python ≥ 3.10）。

### 2. 登录账号（生成凭据）

```bash
./login.sh
```

浏览器打开脚本输出的授权链接，用 QQ / 微信 / 手机号完成登录后回车。凭据会以 `{uid}.info` 保存到本机凭据目录。

**多账号 = 重复执行 `./login.sh` 登录不同账号即可**，每个账号一个文件，互不覆盖。
也可以直接使用 WorkBuddy / CodeBuddy 桌面端的登录态（脚本会自动探测桌面端凭据目录）。

### 3. 启动服务

```bash
./start.sh --desensitize
# 或直接：
.venv/bin/python converter.py --port 8787 --desensitize
```

启动时会先执行一轮全账号签到，再打印预检信息（账号池里有哪些账号、token 是否过期）。

### 4. 打开面板

浏览器访问：<http://127.0.0.1:8787/panel>

- 每个账号一张卡片：当前使用/备用/冷却状态、昵称、token 到期时间、今日签到状态
- 每个积分资源包一条进度条：剩余/总量 credits、周期截止日期（已耗尽的包自动隐藏）
- 顶部统计：账号总数、总剩余 credits、今日签到进度；30 秒自动刷新
- 非当前账号卡片右上角有 **"设为当前使用"** 按钮，点击即手动切换

### 5. 客户端接入

```
Base URL: http://127.0.0.1:8787/v1
API Key:  未设置 --api-key 时留空即可
```

#### Codex CLI

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

#### Claude Code / CC Switch

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

> Codex CLI 与 Claude Code 均建议开启 `--desensitize`；若仍被审核拦截，`/v1/responses` 会自动以压缩模式重试一次。

---

## 凭据目录（自动扫描）

服务按以下顺序扫描所有存在的目录，并按账号 uid 去重：

1. 环境变量 `CODEBUDDY_AUTH_DIR` 指定的目录
2. Linux/WSL：`~/.local/share/CodeBuddyExtension/Data/Public/auth`
3. WSL 下自动探测 Windows 宿主机：`/mnt/c/Users/*/AppData/Local/CodeBuddyExtension/Data/Public/auth`
4. macOS：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth`
5. Windows 原生：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth`

凭据文件为 JSON（`.info` 后缀），结构：

```json
{
  "account": { "uid": "...", "nickname": "...", "enterpriseId": "" },
  "auth": { "accessToken": "...", "refreshToken": "...", "expiresAt": 0, "domain": "www.codebuddy.cn" }
}
```

> ⚠️ 凭据文件等同账号密码，请勿提交到版本库或分享给他人。本项目已通过 `.gitignore` 排除 `*.token` / `*.key` / `secrets.*` / `checkin_state.json`。

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat 兼容（原生 tools/tool_calls，流式/非流式） |
| POST | `/v1/responses` | OpenAI Responses 兼容（Codex CLI） |
| POST | `/v1/messages` | Anthropic Messages 兼容（Claude Code / CC Switch） |
| POST | `/v1/messages/count_tokens` | Anthropic token 计数（stub） |
| GET  | `/v1/models` | 模型列表 |
| GET  | `/health` | 健康检查 + 账号池概览 |
| GET  | `/panel` | 可视化面板：账号池额度 + 模型管理 |
| GET  | `/v1/account-status` | 面板数据接口：账号状态 + credits 额度 + 签到状态 |
| POST | `/v1/account-switch` | 手动切换当前账号，请求体 `{"uid": "<账号uid>"}` |
| GET  | `/v1/models-info` | 面板模型管理数据：模型列表(含参数规格) + 探测进度 + API 接入信息 |
| POST | `/v1/models/custom` | 添加自定义模型，`{"name", "alias"?, "specs"?}` |
| POST | `/v1/models/specs` | 更新模型参数规格，`{"name", "specs": {context_length, max_output_tokens, input, output}}` |
| POST | `/v1/models/delete` | 删除自定义模型 / 禁用⇄恢复内置模型，`{"name"}` |
| POST | `/v1/models/probe` | 可用性探测单个模型，`{"model"}` |
| POST | `/v1/models/probe-all` | 后台顺序探测全部模型(进度见 `/v1/models-info`) |

### 账号池调度细节

- 候选顺序：当前账号 → 其他健康账号 → 冷却中账号（兜底硬试）
- 触发切换的状态码：`401 / 402 / 403 / 429`；或响应文本含 `额度 / 余额 / 积分不足 / 配额 / quota / insufficient / exceeded / 限流 / 频率`
- 切换只发生在**尚未向客户端输出任何字节之前**——客户端不会收到半截流再断开
- 失败账号冷却 30 分钟后恢复候选；成功账号会被"粘住"

---

## 启动参数与环境变量

```
--host            监听地址（默认 127.0.0.1）
--port            监听端口（默认 8787）
--api-key         要求客户端携带的 API key（默认不校验；也可用环境变量 CODEBUDDY2OPENAI_KEY）
--log PATH        请求/响应日志写入该文件（默认关闭）
--desensitize     启用零宽脱敏 + system 压缩（缓解内容审核误拦，建议开启）
--no-compact      配合 --desensitize：跳过压缩只做脱敏，保留完整 system 提示词
--skip-check      跳过启动预检
```

> **脱敏范围（重要）**：`--desensitize` 只重写 system / developer 消息，以及 Codex CLI 注入的运行时块
> （`<environment_context>` / `<permissions instructions>` / `<collaboration_mode>` / `<skills_instructions>`）。
> ZCode CLI 与 Claude Code 放在 `<system-reminder>` 里的**工作区指令**（AGENTS.md、CLAUDE.md、记忆索引）
> **原样透传**——早先版本会把这整条消息替换成一句占位符，模型因此完全看不到规则（典型症状：代理后面的
> agent 不遵守 AGENTS.md，Windows 上不用 `pwsh` 而用 `powershell`）。如需连 system 提示词也保持原样，
> 用 `--no-compact`。另外 `--desensitize` 会剥离 tools 的 `description` 字段（参数 schema 保留）。

| 环境变量 | 说明 |
|----------|------|
| `CODEBUDDY_AUTH_DIR` | 额外指定凭据目录（优先扫描） |
| `CODEBUDDY2OPENAI_KEY` | 等效 `--api-key` |
| `CODEBUDDY2OPENAI_LOG` | 等效 `--log` |

---

## systemd 常驻部署（Linux / WSL2）

`~/.config/systemd/user/workbuddy2api.service`：

```ini
[Unit]
Description=WorkBuddy/CodeBuddy OpenAI Reverse Proxy & Auto Checkin
After=network.target

[Service]
Type=simple
# 下面两行的路径改成你实际解压项目的目录（systemd 要求绝对路径，%h 代表用户 home）
WorkingDirectory=%h/workbuddy2api
ExecStart=%h/workbuddy2api/start.sh --desensitize
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now workbuddy2api
journalctl --user -u workbuddy2api -f     # 查看日志
```

## Docker 部署（可选）

```bash
# 先在宿主机完成登录（或把凭据目录挂进容器），再：
docker compose up -d
```

`docker-compose.yml` 默认挂载 macOS 凭据路径，其他平台请修改 volumes 中的 auth 目录。容器内多账号同理——挂载的目录里有几个 `.info` 文件就有几个账号。

---

## 签到脚本单独使用

```bash
.venv/bin/python scripts/checkin.py
```

自动遍历凭据目录中所有账号：token 临近过期先刷新 → 调用每日签到接口 → 结果打印并写入 `checkin_state.json`（按 uid 去重，同账号多份凭据只签一次）。

---

## 测试

```bash
# 账号池端到端测试（内置本地 mock 后端，34 项断言，不依赖真实账号）
.venv/bin/python tests/test_account_pool.py

# 协议适配器测试
.venv/bin/python tests/test_anthropic_adapter.py
.venv/bin/python tests/test_responses_adapter.py
```

`tests/test_account_pool.py` 覆盖：uid 去重、粘性/冷却/切换顺序、failover 状态码与文本判定、非流式与流式端到端切换、全部账号失败时的错误透传、面板数据接口。

---

## 与 CLIProxyAPI 集成（可选）

本服务可作为 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的一个 OpenAI 兼容渠道，把账号池暴露进统一网关：

```yaml
openai-compatibility:
  - name: "workbuddy"
    base-url: "http://127.0.0.1:8787/v1"
    api-key-entries:
      - api-key: "any-non-empty-string"   # 本服务未设 --api-key 时不校验
        proxy-url: "direct"               # 本机回环必须直连，避免走全局代理
    models:
      - name: "glm-5.2"
        alias: "glm-5.2"
      # ...其余模型同理
```

---

## 目录结构

```
workbuddy2api/
├── converter.py               # 主服务入口：三协议转换 + 账号池 + 可视化面板（FastAPI）
├── core/                      # 核心模块
│   ├── responses_adapter.py   #   Responses API ↔ Chat 转换
│   ├── responses_projection.py#   Responses 请求投影/压缩
│   ├── anthropic_adapter.py   #   Anthropic Messages ↔ Chat 转换
│   └── desensitize.py         #   内容审核脱敏（零宽字符 + system 压缩）
├── scripts/                   # 工具脚本
│   ├── checkin.py             #   每日签到（多账号遍历 + 状态记录）
│   └── oauth_login.py         #   OAuth 设备流登录，生成 {uid}.info 凭据
├── tests/                     # 测试（账号池端到端 / 协议适配器 / 面板 JS 质量门）
├── start.sh                   # 启动脚本（先签到再起服务）
├── login.sh                   # 登录脚本
├── requirements.txt           # fastapi / uvicorn / httpx
├── Dockerfile / docker-compose.yml
└── *.json                     # 运行时状态（签到/模型注册表/API Keys，自动生成，不入库）
```

## 来源与致谢

本项目是 **[HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai)** 的二次开发,参考并继承了其核心思路与基础实现(CodeBuddy/WorkBuddy 登录凭据读取、直连后端协议转换、脱敏模块),在此感谢原作者。

在原项目基础上,本仓库扩展了:

- 多账号池:额度用尽自动切换、手动切换、粘性调度与冷却
- 每日自动签到(多账号遍历)
- 可视化额度面板(`/panel`,credits 资源包展示)
- OpenAI Responses / Anthropic Messages 协议端点的补强

原项目的单账号部署方式、协议设计等文档以 [上游仓库](https://github.com/HanHan666666/codebuddy2openai) 为准。

---

<sub>
Keywords: codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>
