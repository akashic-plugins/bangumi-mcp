# 插件 V2 → V3 迁移方案

- 状态：方案已确认，待实现
- 目标版本：`0.6.0`
- 日期：2026-09-16
- 已确认决策：D1 = 先停用自动投递、保留数据与手动工具；D2 = 进程内 `TOOLS` 注册、去掉 MCP 层；D3 = Token 走凭据 broker；D4 = 版本 `0.6.0`、ID 仍为 `bangumi@github`；D5 = 接受 `configure.py --upgrade` 显式升级
- 依据：Akashic `main@b3953f0`（2026-09-15）的 `_handbook/plugins-tutorial.md`、`docs/design/plugin-v3-capabilities.md`、`docs/design/plugin-resource-providers.md`、`docs/decisions/0071-plugin-composition-and-whole-runtime-updates.md`，以及本仓库现状

## 1. 结论摘要

Akashic 插件底座已经换成 **V3 组合模型**：仓库根目录 `plugin.py` 不再导出 `Plugin` 子类，只保留三个顶层字面量身份和唯一入口 `apply(ctx)`；配置、凭据、Skill、MCP、工具、定时任务全部改成在 `apply(ctx)` 里通过 ServiceKey 注册。本插件当前的 `plugin.py` 是 V2（`api_version = 2` + `class BangumiPlugin(Plugin)`），**在新底座上不会被加载**（loader AST 直接拒绝），因此必须重写。

重写后插件表面基本不变：仍然是「Bangumi 收藏查询 / 确认后写入 / 放送提醒」三块能力，工具名和 Skill 文案大体保留。真正变化的是**接入方式**和**凭据存储**，以及**放送提醒的投递链路在当前上游底座里还没有对应的公开能力**（已确认先停用自动投递，见第 6.2、7 节）。

## 2. 现状与本仓库需要改动的资产

| 资产 | 现状 | V3 处理 |
|---|---|---|
| `plugin.py` | V2 类式插件，`api_version = 2`，`skill_roots()` / `mcp_servers()` / `proactive_sources()` | 整体重写为 `apply(ctx)` 模块 |
| `mcp/`（`run_mcp.py` + `src/`） | 独立 stdio MCP server：HTTP client、收藏查询、写入确认、AniList、放送事件库、后台刷新 | 见第 5 节：推荐工具改为进程内注册（`TOOLS`），`mcp/src` 领域代码复用 |
| `mcp/requirements.txt` | `mcp` + `requests` | 方案 A 去掉 MCP 后删除 `mcp/`，新增根目录 `requirements.txt`（环境约定见第 5 节末） |
| `skills/bangumi/` | Skill 目录，V2 靠 `skill_roots()` | 改为 `INSTALLED_ASSETS.register(ctx, "skills", "skills")` |
| `tests/test_plugin_contract_surface.py` | 断言 `api_version == 2`、类式结构、`proactive_sources` 文本 | 重写为 V3 契约测试（顶层字面量 + `api_version == 3` + 能力注册） |
| `README.md` | 安装后教用户手写 `plugin-data/<owner>/config.local.toml` | 改为 `main.py setup` + `configure.py` 流程；Token 说明改为凭据 broker |
| `skills/bangumi/SKILL.md` | 「凭据」一节教用户写 `config.local.toml` | 同步改为新配置流程 |
| `docs/design/anime-update-push.md` | 基于 `proactive_v2` 的放送提醒设计 | 标注为待 V3 投递能力确定后修订 |

## 3. V3 插件的最小形状（已核实）

```python
from agent.plugin_composition import Context

api_version = 3          # 必须是顶层单次字面量，loader 用 AST 读取，不 import
name = "bangumi"
version = "0.6.0"

inject = (...)           # 根 Fiber 硬依赖的 ServiceKey 序列

async def apply(ctx: Context) -> None:
    ...
```

已核实的关键约束：

- 身份只由 `plugin.py` 顶层的 `name` / `version` / `api_version` 三个**单次字面量**赋值提供，计算表达式、导入值、条件分支、重复赋值都会被拒绝；`api_version != 3` 直接报错。
- `apply` 只接收**一个位置参数** `ctx`；可以同步或异步。（当前 calendar-mcp 的 `apply(ctx, config)` 已经是旧写法。）
- 模块级 `Config` / `skill_roots` / `drift_skill_roots` **都不再被读取**；`desc` / `author` 被接收但没有读取方，可保留但不要依赖。
- `inject`、`workspace_roots`、`workspace_files` 仍然有效；`workspace_roots` 不能声明 Core 保留目录（`plugin-data`、`runtime`）。
- 配置从 `ctx.config` 读取（`Mapping[str, object]`，是本次组合的冻结副本），插件自己用 Pydantic/普通类解析，底座不解释字段。
- 持久数据写 `ctx.data_root`（= `<workspace>/plugin-data/bangumi-github`）。

## 4. 配置与凭据迁移（最大行为变化）

### 4.1 新存储

- 插件配置：`plugin-data/<owner>/config.input.json`，由 `agent.plugin_composition.config_input.save_config(data_dir, mapping)` 写入。
- 凭据：`save_credential(data_dir, value) -> CredentialRef`，用户明文不进入 `config.input.json`，而是进入 `<workspace>/.plugin-credentials/<owner>/`（0700/0600，不可变版本）。
- 运行时读取：`ctx.config` 里只有 `CredentialRef`；插件用
  ```python
  async with ctx.require(CREDENTIALS).open(ctx, {"access_token": cfg.access_token}) as creds:
      token = creds.credential(cfg.access_token)
  ```
- **重要**：如果 `plugin-data/<owner>/config.local.toml` 仍然存在，`load_config` / 运行时检查会直接 `RuntimeError("发现 config.local.toml；须离线显式升级插件配置")`。迁移不是可有可无的，必须显式升级。

### 4.2 交付物

1. 新增根目录 `configure.py`（插件自己的配置程序，只在 `main.py setup` 时运行）：
   - 普通模式：交互/参数式收集 `access_token`（必填）、`user_agent`、`anime_push.*`、`media_id_overrides`，调用 `save_credential` + `save_config`。
   - `--upgrade` 模式：读取旧 `config.local.toml`，调用 `upgrade_config(data_dir, convert)`，`convert` 里把明文 Token 转成 `save_credential` 返回的引用，其余字段转普通值。旧文件保留在 `.plugin-credentials/<owner>/upgrades/<id>/` 的 `original/` + `retired/`。
   - 环境变量约定：`AKASHIC_PLUGIN_DATA_DIR`（必需）、`AKASHIC_SETUP_CONFIG_PATH`、`AKASHIC_PLUGIN_ROOT`、`AKASHIC_WORKSPACE`。
2. 根目录 `requirements.txt`：`configure.py` 只有存在**根目录** `requirements.txt` 环境时才会被 setup 执行；当前仓库没有根级文件，需要补一个（内容至少含 `pydantic`）。
3. `plugin.py` 里 `apply(ctx)` 解析 `ctx.config`，在需要凭据的实际调用点打开 broker 租约（不要在 `apply` 顶层长期持有明文）。
4. `apply(ctx)` 本身运行在宿主解释器里，HTTP 库（`requests` / `httpx`）与 `pydantic` 直接复用宿主依赖即可，**不**因此进入插件环境；根 `requirements.txt` 只服务 `configure.py` 的导入。
5. 数据文件 `anime_updates.db` 继续放 `ctx.data_root`，不迁移、不删除。

### 4.3 升级路径（用户可见）

```bash
# 1) 重装新制品（V2 目录布局/源码在 V3 底座上会被拒绝，必须显式重装）
python main.py plugin-install --source https://github.com/akashic-plugins/bangumi-mcp.git --marketplace github
# 2) 显式升级旧配置
python main.py setup            # 选择 bangumi；跑 configure.py --upgrade
python main.py plugin-doctor bangumi@github
```

`plugin-uninstall` 会保留 `plugin-data/`，所以重装后数据与旧配置仍在，升级是一次显式动作。

## 5. 工具能力迁移（核心决策点）

当前 11 个 MCP 工具：`get_collection_status`、`list_collections`、`continue_collection_query`、`count_collections`、`prepare_collection_query`、`execute_prepared_collection_query`、`prepare_collection_status_update`、`prepare_anime_progress_update`、`commit_prepared_update`、`get_anime_update_alerts`、`acknowledge_anime_update_alerts`。

V3 里有两条路，**推荐方案 A**：

### 方案 A（已采用）：工具进程内注册，去掉 MCP 层

- 在 `apply(ctx)` 中：`await ctx.require(TOOLS).declare_group(ctx, description=...)`，然后对每个工具 `await ctx.require(TOOLS).register(ctx, name=..., description=..., parameters=<JSON Schema>, open=<async context manager 返回 BoundTool>, idempotent=..., risk=...)`。
- `TOOLS = ServiceKey[ToolCatalog]("tools.v1")` 不在 `agent.plugin_composition.__all__` 里，是 provider 插件 `plugins/tools` 拥有的服务；按官方约定，消费者**自己声明同结构窄 Protocol + 本地同名 key**，不 import 兄弟插件实现（calendar-mcp 的 `_tool_contract.py` 就是这么做的）。本插件新增一个 `bangumi/_tool_contract.py` 承担这个角色。
- `TOOLS` 注册校验：工具名匹配 `^[a-z][a-z0-9_]{0,63}$`，`parameters` 必须是 `{"type": "object", ...}` 的 JSON Schema；`declare_group` 必须先于该 owner 的 `register`。11 个工具名都合法。
- 现有 `mcp/src/*` 的领域代码（client、query、confirmation、service、anilist、anime_updates）整理为不加 `mcp` 前缀的包（建议 `bangumi/`），由 `plugin.py` import；返回值从「JSON 字符串」改为 `Result(outcome, (ContentPart("text", json_text),))`。
- HTTP：`requests` 是同步库，进程内调用必须走 `await asyncio.to_thread(...)` 或等价封装，禁止直接阻塞事件循环；若改用 `httpx` 则直接 async。库由宿主解释器提供，不需要进入插件环境。
- 凭据：进程内 `CREDENTIALS.open(...)` 取 Token，天然支持版本/撤销/租约，且 **candidate 校验期不会读到正式凭据**。
- 后台刷新：用 `TIMERS` + `ctx.spawn`（见第 6 节），不再依赖 MCP lifespan。
- 优点：少一层进程/协议；凭据不需要落到子进程；工具与定时任务共享同一份内存状态和 `ctx.data_root`；`mcp` 依赖可删除。
- 代价：`mcp/src` 的 server 层与部分测试要重写。

**Python 环境约定（易踩坑）**：V3 只认制品内精确名 `requirements.txt`，其**父目录拥有该环境**，命令按最近父环境解析。因此：
- `configure.py` 只在存在**根目录** `requirements.txt` 对应环境时可运行 → 必须新增根 `requirements.txt`。
- 若保留 `mcp/requirements.txt`，它会继续被安装器当成第二个环境；方案 A 去掉 MCP 后应删除 `mcp/`，只留根环境。
- `apply(ctx)` 本身运行在宿主解释器里，不需要插件环境；环境只对 `configure.py` 的导入（如 `pydantic`）和未来子进程命令有意义。

### 方案 B（备选）：保留 stdio MCP，只改注册方式

- 在 `apply(ctx)` 中 `await ctx.require(MCP_SERVERS).register(ctx, McpServerDefinition(name="bangumi", command=("python", "mcp/run_mcp.py"), required_tools=(...), candidate_read_only_tools=(...)))`，并像 calendar 那样写一个本地窄 `TOOLS` 结构合同，把 MCP 工具一一注册成带 `open` 的进程内工具。
- 已核实的硬约束：
  - `McpServerDefinition.env` / `candidate_env` 只接受**字面量字符串**，没有任何凭据引用解析；Core 不会把 `CredentialRef` 传给子进程。
  - 子进程只拿到 `AKA_PLUGIN_DATA_DIR`、`AKASHIC_PLUGIN_DATA_DIR`、`AKASHIC_WORKSPACE`、`HOME` 等固定环境，且这些名字都不可覆盖。
  - candidate/validation Root 明令禁止解析正式凭据（`candidate 验证期禁止读取正式凭据`），所以「在 `apply` 里解析 Token 再塞进 `env`」在候选期不可用、且会把明文冻结进 descriptor。
  - 现实做法只能是「插件在正式运行时把明文写入 `ctx.data_root` 下某个配置文件，MCP 子进程自己读」——这正是 calendar/steam 的现状，但**安全性低于 broker**，也是我们当前 `config.local.toml` 的老问题。
- 只有在「必须保留 MCP 进程隔离」或「工具要复用到别的客户端」时才值得选它。

> 无论 A/B，工具**不再被 Agent 直接发现为 MCP 工具**；请同步更新 `SKILL.md` 中对 `bangumi` MCP 的措辞，改为工具名直呼。

## 6. 放送提醒（anime_push）的现状与阻塞

### 6.1 已核实的事实

- V3 设计文档里的主动信息源 `PROACTIVE_COMPONENTS` / `ProactiveSourceDefinition` 以及 `BACKGROUND_JOBS`，在当前上游代码里**并不存在**：`agent/plugin_composition/` 没有对应模块，`__init__.py` 不导出这两个 key，仓库还有一条边界测试断言它们是 phantom。文档只是先行合同。
- 上游默认 profile 里也没有 `proactive` 插件；与「对外主动投递」相关的现有插件是 `eventmail`、`delivery`、`delivery_policy`、`message_push`、`scheduler`、`wake`、`programmatic`，它们都不是「外部插件登记 alert source，引擎按 tick 消费」的公开接口。
- 当前插件用 `fetch_tool` / `ack_tool` + `channels=("alert",)` 的写法在 V3 里**没有等价公开能力可注册**。
- 可用替代积木：`TIMERS.schedule(deadline)`（candidate 期禁止）、`ctx.spawn(...)`、`ctx.effect(...)`、`RUNTIME_STARTED` / `RUNTIME_STOPPING` lifecycle 事件。

### 6.2 已确认路线（D1）

**先停用自动投递，保留数据与手动工具。** 具体行为：

1. `anime_updates.db`、`AnimeUpdateStore`、AniList 计划放送抓取与映射逻辑全部保留并迁到 `ctx.data_root`，库结构与语义不变。
2. 刷新循环改为插件自有：在 `RUNTIME_STARTED` 用 `ctx.spawn` 启动一个只依赖 `TIMERS.schedule(deadline)` 的循环，在 `RUNTIME_STOPPING` / Effect cleanup 里取消并等待；candidate 期不启动（`TIMERS.schedule` 自身在 candidate 会拒绝，所以要在 `RUNTIME_STARTED` 之后才 schedule）。
3. 到期事件照常写入 DB；`get_anime_update_alerts` / `acknowledge_anime_update_alerts` 保留为手动工具，行为不变，便于用户查看和清理积压。
4. **不再有任何自动投递**：插件不注册 `channels=("alert",)`，也不做自动 ACK。文档与 Skill 明确说明「V3 版暂无自动放送提醒，仅保留查询与手动确认」。
5. 为将来的上游能力预留：把「拉取到期事件」和「ACK 事件」拆成两个纯函数/服务方法，将来接 `ProactiveSourceDefinition` 时只替换投递适配层，不动领域逻辑。

其余两条路线（programmatic 注入 / 等上游）记录为已评估但不采用。

## 7. 决策记录（已确认）

| 编号 | 决策 | 结论 |
|---|---|---|
| D1 | `anime_push` 怎么处理？ | **先停用自动投递**，保留 DB、刷新与两个手动工具；等上游公开 proactive source 能力后再接投递层（见 6.2） |
| D2 | 工具走进程内还是保留 MCP？ | **进程内 `TOOLS` 注册**，去掉 MCP 层（方案 A） |
| D3 | Token 存哪里？ | **凭据 broker**（`save_credential` + `CREDENTIALS.open`） |
| D4 | 版本与身份 | `version = "0.6.0"`，插件 ID 仍为 `bangumi@github`，`name = "bangumi"` |
| D5 | 是否接受 `configure.py --upgrade` | 接受；旧 `config.local.toml` 会被运行时拒绝，必须显式升级 |

## 8. 分阶段实施计划（决策确认后执行）

### 进度

- Phase 0：**已完成**（`plugin.py` V3 骨架、`bangumi/` 包、工具目录与 11 个工具发现合同、契约与 `apply` 接线测试、CI 改为 `plugin-v3`）。工具执行仍是明确的 `ToolExecutionError` 占位，领域接入在 Phase 2。
- Phase 1：**已完成**（`configure.py`、固定输入契约、凭据引用、旧 TOML 转换、配置文档）。`bangumi/config.py` 的旧 TOML 运行时加载器暂时保留给现有测试，Phase 2 接入工具时替换为 `bangumi/settings.py`。
- Phase 2–4：未开始。

### 固定输入的字段约定（Phase 1 定稿）

```jsonc
{
  "access_token": { "path": ["<credential-id>", "<revision>"] },
  "user_agent": "akashic-plugins/bangumi-mcp/0.6.0 (...)",
  "anime_push": {
    "enabled": false,
    "notify_before_minutes": 0,
    "display_timezone": "Asia/Shanghai",
    "anilist_token": { "path": ["<credential-id>", "<revision>"] },
    // JSON 只有字符串键；插件解析时转回 int。
    "media_id_overrides": { "501963": 123456 }
  }
}
```

- `parse_settings(ctx.config)` 拒绝明文 Token（字符串形式直接报错），只接受 `CredentialRef`。
- 旧 TOML 与普通模式都在**写入前**归一化并校验（`notify_before_minutes` 边界、IANA 时区、正整数 ID、未知字段），避免把无效值固化进固定输入。
- `configure.py` 普通模式只从环境变量取凭据，不接受命令行明文；`--upgrade` 的恢复点与旧文件退役全部由宿主 `upgrade_config` 负责。

### Phase 0 — 契约与骨架（可独立验证）

1. 重写 `plugin.py`：三个顶层字面量、`inject`、`apply(ctx)`；保留 `desc`。
2. 新增根目录 `requirements.txt`。
3. 重写 `tests/test_plugin_contract_surface.py` 为 V3 契约断言。
4. 补齐本地验证环境（`.venv` 目前没有 pytest；用 uv 安装 `pytest` / `PyYAML` / `requests`）。

### Phase 1 — 配置与凭据

5. 新增 `configure.py`（普通 + `--upgrade`）。
6. `apply(ctx)` 解析 `ctx.config`，定义插件内部 Pydantic 配置模型；Token 走 `CREDENTIALS.open`。
7. 新增配置解析 / 升级路径的单元测试（含「旧 TOML 存在时必须要求显式升级」、`repr` 不泄露 Token）。

### Phase 2 — 工具与 Skill

8. 把 `mcp/src` 领域代码整理为可进程内调用的模块（保留现有纯函数与假 HTTP 会话测试）。
9. 注册 9 个查询/写入工具到 `TOOLS`，`risk` / `idempotent` 按现有只读 / 预览 / 提交语义标注。
10. `INSTALLED_ASSETS.register(ctx, "skills", "skills")`，更新 `SKILL.md` 的凭据与工具措辞。
11. 重写 `mcp/src/server.py` 相关测试为工具注册/调用测试。

### Phase 3 — 放送提醒（按 D1：保留但不自动投递）

12. 把刷新循环迁到 `RUNTIME_STARTED` + `ctx.spawn` + `TIMERS.schedule`，`RUNTIME_STOPPING` / Effect cleanup 收束；candidate 期零调度。
13. 保留 `get_anime_update_alerts` / `acknowledge_anime_update_alerts` 两个手动工具；移除自动投递与自动 ACK 路径。
14. 把「拉取到期事件」与「ACK」拆成纯服务方法，作为将来接入上游 proactive source 的适配点；在 `plugin.py` 和设计文档注明 TODO。

### Phase 4 — 文档与验证

15. 更新 `README.md`：新安装 / `main.py setup` 配置 / `--upgrade` 升级步骤；明确「V3 版暂无自动放送提醒」。
16. 更新 `docs/design/anime-update-push.md` 的状态为「部分由 V3 支持：抓取与查询保留，自动投递待上游能力」。
17. 更新 `skills/bangumi/SKILL.md`：凭据改为 `main.py setup`，工具改为直呼工具名，放送提醒一节加 V3 限制。
18. 全量 `pytest`；`ruff check`；用上游 `main` 的 `plugin-doctor` / 真实安装链做一次静态检查。

## 9. 验证策略

- 单元测试：沿用现有「假 HTTP 会话、不访问真实账号」的做法；新增 V3 契约测试（顶层字面量、`apply` 单参数、`inject` 全为 `ServiceKey`）。
- 静态契约：`plugin.py` 顶层身份可被 AST 读取且 `api_version == 3`；无 `class ...(Plugin)`、无 `plugin.json` / `manifest.yaml` 残留。
- 安装验证：安装后 `plugin-doctor bangumi@github` 不报 broken；`config.input.json` 可通过 schema；旧 `config.local.toml` 被显式升级后不再存在。
- 不做的验证：不访问真实 Bangumi / AniList 账号，不执行真实写入。

## 10. 风险与回滚

- 主要风险：上游 V3 仍在实施中（capabilities 文档自己声明「代码提交不代表运行验收或部署完成」），`anime_push` 的投递缺口最大。
- 回滚：本仓库改在 `codex/` 分支；旧制品与旧配置保留，未推送前可整体回退；不修改 Akashic 仓库与 `.plugin-credentials`。
- 边界：不删除 `plugin-data` / `anime_updates.db`；不把 Token 写入 Git、日志、命令参数或对话。
