# 插件契约刷新：跟随 Akashic 插件边界重构

- 状态：**已实现**（D1/D2）；`pytest` 189 passed，`ruff check` 无新增错误
- 目标版本：`0.7.0`（理由是**契约语义变了**：依赖的 ServiceKey、工具注册面、
  结果值类型全部换了一代，0.6.x 的语义无法在新宿主上成立。
  **不是因为制品目录需要新版本号**：`artifact_id = f"{plugin_version}-{source_revision[:16]}"`
  带 revision 前 16 位，代码一改 revision 就变，自然新建目录，同版本号也能装出干净制品）
- 日期：2026-10-22
- 核对基线：Akashic `e1628ddc`（2026-10-20）；本仓库 `44326da`（2026-09-17）
- 依据：Akashic `plugin_boundary.toml`、`docs/design/plugin-boundaries.md`、
  `docs/decisions/0102-plugin-owned-services-and-public-contracts.md`、
  `0108-bundles-are-composition-inputs.md`、`0109-workspace-owns-plugin-choices.md`、
  `0121-plugin-install-settles-from-disk.md`、`docs/design/plugin-v3-capabilities.md`、
  `docs/design/plugin-onboarding-projection.md`，以及两仓库实际源码与实测。
- 已确认决策：D1 = 配置改插件自有离线命令（`entrypoints`），不做 Web 设置页；
  D2 = 先写文档，再动代码。

## 1. 结论摘要

`bangumi-mcp 0.6.0` 在 Akashic `e1628ddc` 上**根本无法加载**，不是「功能退化」。
本轮不是插件出新版本，而是插件系统架构变了：宿主把「认识某个具体插件」的地方全部拆掉，
并把公共合同的所有权从 Core 移交给提供方插件（ADR-0102）。本插件当年是照
`plugin-v3-capabilities.md` 的「Core 壳」写的，那层壳现在不存在了。

已实测到的六处硬失效（全部有代码依据，见第 3 节）：

| # | 症状 | 严重度 |
|---|---|---|
| 1 | 绝对自导入 `from bangumi.…` 在宿主加载器下 `ModuleNotFoundError` | 阻断加载 |
| 2 | 四个 ServiceKey 名字全部对不上（`assets` / `timers` / `eventmail` ×2） | 阻断加载 |
| 3 | `tools.register(..., risk=…)` 已删参数，`TypeError` | 阻断注册 |
| 4 | 工具结果不是宿主的 `ContentPart`，执行时 `TypeError` | 阻断执行 |
| 5 | eventmail v2 的 `report`/`status` 变 async，且新增 `close()`；当前桥是同步的且从不释放 | 阻断投递 + 换代冲突 |
| 6 | `configure.py` 已不被 `main.py setup` 发现或执行 | 配置入口失效 |

第 1–5 条没有选择余地，必须改。第 6 条按 D1 改成插件自有离线命令。

## 2. 上游到底改了什么（与本插件相关部分）

### 2.1 公共合同归属：从 Core 壳到 provider 插件

ADR-0102 之前，`agent/plugin_contracts/*` 是 Core 里的「壳」模块，插件 `from agent.plugin_contracts.tools import TOOLS`。
现在这层壳**整块删除**（提交 `660bd189`「Tools 拥有完整公共合同，删除 Core 工具合同壳」），
合同跟着 provider 插件走：

| 能力 | 现在的位置 | 现在的 key |
|---|---|---|
| 工具目录 | `plugins/tools/contract.py` | `tools.v1` |
| 资产注册 | `plugins/assets/contract.py` | `assets.installed.v1` |
| 一次性定时 | `plugins/timer/contract.py` | `timers.v1` |
| 凭据租约 | `agent/plugin_composition/credentials.py`（Core 独占） | `core.credentials` |
| 告警邮箱 | `plugins/eventmail/contract.py` | `eventmail.alert_source.v2` |
| 上下文材料 | `plugins/eventmail/contract.py` | `eventmail.context_source.v2` |

消费方约定（`plugin_boundaries.md` 判定规则 1、2）：插件**自己声明同结构窄 Protocol + 本地同名 key**，
不 import 兄弟插件实现，只允许 import 对方的 `contract.py`。

### 2.2 Core 只独占九个中立 key

`plugin_boundary.toml` 现在只登记九个 `role = "core"` 的 key，并且有静态门
`scripts/plugin_boundary.py check` 双向核对「表里有的代码里必须有、代码里有的表里必须有」。
`core.installed_assets.v1` 和 `core.timers` 都已不在表内——它们**不是**中立能力，
是 `assets` 和 `timer` 两个普通插件提供的。

### 2.3 工具注册面收窄

`ToolCatalog.register` 现在的签名（`plugins/tools/contract.py:164`）：

```python
async def register(self, ctx, *, name, description, parameters, open,
                   capture=None, public=True, idempotent=False,
                   parallel=False, exclusive_batch=False, release=None) -> ToolRef
```

- `risk` 和 `search_hint` **删除**。旧归档 binding 里的这两个字段只在
  `_matches_saved_description` 里做兼容读取（`plugins/tools/plugin.py:92`），新注册不接受。
- 新增 `parallel`（只读工具可重叠调度）、`exclusive_batch`（整批独占）、`release`（外部资源释放）。

### 2.4 工具结果必须是真的 `ContentPart`

`plugins/tools/api.py:coerce_result` 执行边界会重新校验：

```python
if not isinstance(value, ResultLike): raise TypeError("工具结果必须提供 outcome 和 parts")
return Result(value.outcome, tuple(value.parts))
```

而 `Result.__post_init__` 要求 `isinstance(part, ContentPart)`，`ContentPart` 是
`plugins.ledger.contract` 里的 `@dataclass(frozen=True, slots=True) class ContentPart: kind: str; value: object`。
本插件的 `_TextPart(text=…, type="text")` 结构完全不同（实测 `TypeError: 工具结果必须是内容块`）。

### 2.5 eventmail 升到 v2

`eventmail.alert_source.v2` / `context_source.v2`（提交 `7fe6a24d`）相比 v1：

- `report(...)` / `status(...)` 从同步变 **async**（实测 `iscoroutinefunction` 为 True）。
- `BoundAlertSource` / `BoundContextSource` 新增 `close()`，用于释放 `source_id` 所有权。
- `bind(source_id)` 对同一 `source_id` 只能有一个 owner，重复绑定直接抛
  `RuntimeError("EventMail Alert source_id 已有 owner: …")`。

### 2.6 加载器：插件根目录不进 `sys.path`

`agent/plugins/manager.py:_import_plugin` 把插件注册成 `_akashic_input_<随机 hex>` 模块，
给 `submodule_search_locations=[plugin_root]`，然后 `exec_module`。
全仓 `agent/`、`core/`、`infra/`、`bootstrap/` 里**没有任何** `sys.path` 插入（已 grep 确认）。
所以插件内部只能用**包内相对导入**（`from .settings import …`），
绝对自导入 `from bangumi.settings import …` 找不到模块。

> 注：`plugin.py` 自身被当作 `_akashic_input_<hex>` 的 `__init__` 执行，
> 所以 `plugin.py` 里也应写 `from .bangumi.x import y`，而不是 `from bangumi.x import y`。

### 2.7 配置入口：`configure.py` 退役

`main.py setup` 现在只做 `init_workspace`；`plugin-v3-capabilities.md:56` 明确
「`main.py setup` 仅初始化 Core，不再发现或执行 `configure.py`」，
首次业务配置走插件自有 Web 页面/API + `PLUGIN_CONFIG`（`core.plugin_config.v1`）。
`onboarding` 是普通可卸载插件，只组合目录与排序，不持有业务 writer。

### 2.8 组合与安装（本插件只需理解，不需适配）

- 插件选择是三层组合：发行版 `bundles/base.toml` → `mode`（`headless`/`minimal`）→
  workspace `bundle.patch.toml`，产物是唯一运行输入 `runtime/plugin-stable.json`。
- 同 ID **整行替换**，不做 config 深合并。外置插件在 patch 里是一行
  `[rows."bangumi@github"] plugin = "bangumi@github"`。
- `~/.akashic-plugin/manifest.toml` 里的 `enabled` 已是死字段（ADR-0109）。
- 安装按磁盘指针结算（ADR-0121），制品在 `.artifacts/<version>-<revision>`，
  稳定/最新指针在 `.pointers.json`。

> **数字对账提醒**：ADR-0108 正文写「默认 base 明列当前 48 个插件」，
> 但 `e1628ddc` 的 `bundles/base.toml` 实际是 **61 行 `[rows.*]`**
> （`headless` 7 行禁用、`minimal` 61 行全禁）。差额来自插件边界重构
> 拆出的 `files` / `shell` / `skills` / `vision` / `workspace_changes` 等。
> 引用 ADR 的 48 会与现状对不上，以 TOML 实际行数为准。

## 3. 本仓库逐项失效点（含实测证据）

实测方式：用宿主自己的 `agent.plugins.importer.FreshPluginImporter` 加载本仓库
`plugin.py`，以及用 `plugins.tools.api.coerce_result` 校验本仓库的结果对象。

### 3.1 绝对自导入（阻断加载）

```text
$ FreshPluginImporter().root_spec("_akashic_input_probe", <repo>/plugin.py) → exec_module
ModuleNotFoundError: No module named 'bangumi'
```

把同样的源码在内存里换成 `from .bangumi.…` 后立刻成功（`name=bangumi version=0.6.0 api_version=3`）。

涉及文件：`plugin.py:17,29,30,31`、`bangumi/runtime.py:18`、
`bangumi/tools.py:15`、`bangumi/settings.py:16`、`bangumi/alert_source.py:23`、
`bangumi/anime_push_loop.py:17`，以及 `configure.py:29`。

### 3.2 ServiceKey 名字（阻断加载）

```text
tools        mine='tools.v1'                     host='tools.v1'                     equal=True
assets       mine='core.installed_assets.v1'     host='assets.installed.v1'          equal=False
credentials  mine='core.credentials'             host='core.credentials'             equal=True
timers       mine='core.timers'                  host='timers.v1'                    equal=False
alert        mine='eventmail.alert_source.v1'    host='eventmail.alert_source.v2'    equal=False
context      mine='eventmail.context_source.v1'  host='eventmail.context_source.v2'  equal=False
```

`ServiceKey` 是 `@dataclass(frozen=True, slots=True)`，**身份就是 `name` 字符串**
（`agent/plugin_composition/model.py:29`）。名字不一致 = 依赖永远 PENDING =
根 Fiber 永远不就绪，插件被记为未就绪而不是「报错」。

位置：`bangumi/_host_contract.py:57-60,286-295`。

### 3.3 `risk` 参数（阻断注册）

`plugin.py:105-113` 传 `risk=entry.risk`。宿主的 `register` 不接受该关键字，直接 `TypeError`。
`ToolCatalogService` 的窄 Protocol（`bangumi/_host_contract.py:144-161`）也要同步删掉 `risk`。

### 3.4 工具结果类型（阻断执行）

```text
$ coerce_result(_ToolResult("success", (_TextPart("hi"),)))
TypeError: 工具结果必须是内容块
```

`bangumi/tools.py:127-148` 的 `_TextPart` / `_ToolResult` 是结构模仿，
不是宿主的 `ContentPart(kind, value)` / `Result(outcome, parts)`。
`bangumi/runtime.py:297` 的 `invoke` 也返回同一套结构。

### 3.5 eventmail v2 的异步与释放（阻断投递 + 换代冲突）

当前实现（`bangumi/alert_source.py:42,64,78`）：

- `AlertBinding.report()` 同步调用 `self.contexts.report(...)` / `self.alerts.report(...)`；
  v2 这两个都是 coroutine → 调用只拿到未 await 的协程对象，什么都不发生（还产生
  `RuntimeWarning: coroutine was never awaited`）。
- `BangumiAlertBridge.tick()` 是同步的，并且被 `bangumi/anime_push_loop.py:77`
  放进 `asyncio.to_thread` 执行 —— 线程里没有事件循环，即使改成 async 也跑不了。
- `AlertBinding` / `BangumiAlertBridge` 从不调用 `close()`，
  `bind("bangumi")` 的所有权在换代时不会释放 → 下一次 `apply` 抛
  `EventMail Alert source_id 已有 owner: bangumi`。

### 3.6 `configure.py`（配置入口失效）

`main.py` 的 `setup` 分支只有 `init_workspace`；全仓没有任何地方引用 `configure.py`。
`configure.py:123-126` 依赖的 `AKASHIC_PLUGIN_DATA_DIR` 只在**插件子进程**环境里注入
（`agent/plugins/execution.py:111`），`main.py setup` 不会设置它，所以
「配置程序必须由 main.py setup 提供数据目录」这条断言必然失败。

## 4. 重构方案

### 4.1 导入方式：包内相对导入（对齐 fitbit-mcp）

`plugin.py` 是制品根，被当作 `_akashic_input_<hex>` 的包入口执行，插件根**不在
`sys.path`**。所以 `plugin.py` 必须用包内相对导入：

```python
from .bangumi.runtime import BangumiRuntime   # 宿主加载：正确
```

已在同组织仓库 `akashic-plugins/fitbit-mcp@5b6eef6` 上交叉验证：它的 `plugin.py`
用 `from .tools import register_tools`、`from .src.content_adapter import …`，
**没有任何 `try/except` 或 `__package__` 分支**，也没有 `configure.py` 和 `entrypoints`。

**纯相对导入会让 `import plugin` 在顶层失败**：

```text
$ cd <repo> && python -c "import plugin"
ImportError: attempted relative import with no known parent package
```

**测试侧按 fitbit 的做法解决：给仓库造一个包身份，而不是给源码加分支。**
`tests/conftest.py` 注册一个伪包，把仓库根作为它的 `__path__`：

```python
package = ModuleType("bangumi_test_plugin")
package.__path__ = [str(repo_root)]
package.__package__ = "bangumi_test_plugin"
sys.modules["bangumi_test_plugin"] = package
```

之后测试统一写 `from bangumi_test_plugin import plugin`、
`from bangumi_test_plugin.bangumi.settings import …`，`from .bangumi.…`
在测试里同样成立。`pytest -q tests/` 的常规收集与 `-k` 过滤不受影响
（fitbit 的 CI 就是这样跑的）。

**为什么不给 `plugin.py` 加 `if __package__:` 双路分支**：源码里多一条宿主永远不会走
的分支，等于把「本地测试的便利」固化进生产制品的入口文件。fitbit 的伪包方案
把测试适配留在 `tests/conftest.py`，制品保持单一路径。本仓库采纳后者。

`bangumi/` 包内部本来就是相对导入，无需改动。

### 4.2 合同刷新：`bangumi/_host_contract.py`

| 项 | 旧 | 新 |
|---|---|---|
| `INSTALLED_ASSETS_SERVICE_NAME` | `core.installed_assets.v1` | `assets.installed.v1` |
| `TIMERS_SERVICE_NAME` | `core.timers` | `timers.v1` |
| `ALERT_SOURCE` | `eventmail.alert_source.v1` | `eventmail.alert_source.v2` |
| `CONTEXT_SOURCE` | `eventmail.context_source.v1` | `eventmail.context_source.v2` |
| `TOOLS_SERVICE_NAME` | `tools.v1` | 不变 |
| `CREDENTIALS_SERVICE_NAME` | `core.credentials` | 不变 |

`InstalledAssets.register` 签名不变（`(ctx, category, relative_path)`），
`PluginTimers.schedule(deadline)` 不变，`TimerHandle` 仍是
`id/result/cancel/cleanup`（`plugins/timer/contract.py`）。

`ToolCatalogService.register` 删除 `risk` / `search_hint`，新增
`parallel` / `exclusive_batch` / `release`（本插件先只用到 `parallel`，见 4.4）。

`BoundAlertSource` / `BoundContextSource` 补 `close()`，
`report` / `status` 改成 `async def`。

> 这些都是**窄 Protocol 的本地声明**，不是 import 宿主实现；名字必须逐字一致。

### 4.3 工具结果：用宿主值类型

`bangumi/tools.py` 改为：

```python
from plugins.ledger.contract import ContentPart

@dataclass(frozen=True, slots=True)
class ToolResultValue:
    outcome: Outcome
    parts: tuple[ContentPart, ...]

def tool_error(message: str) -> ToolResultValue:
    return ToolResultValue("error", (ContentPart("text", message),))

def tool_success(payload: object) -> ToolResultValue:
    return ToolResultValue("success", (ContentPart("text", _json_text(payload)),))
```

理由：`plugins.ledger.contract` 是**允许插件 import 的公共合同**
（`plugin_boundary.py` 的 R3 只允许兄弟插件的 `contract.py`；`ledger` 是基础设施合同）。
`files`、`shell`、`skills` 三个第一方插件全部这么写（`plugins/files/files.py:112`）。

`_TextPart` / `_ToolResult` 删除。`ContentPart.value` 会被 `freeze_json` 冻结，
`_json_text` 已经返回 `str`，满足约束。

### 4.4 工具注册：去掉 `risk`，加 `parallel`

`bangumi/tools.py` 的 `ToolEntry` 保留 `risk` 字段**仅作本仓库内部元数据**
（README 与测试仍描述只读/写入语义），但 `plugin.py` 不再把它传给宿主。
只读工具（`list_collections`、`count_collections`、`get_collection_status`、
`continue_collection_query`、`get_anime_update_alerts`）标 `parallel=True`；
其余保持默认 `False`。

### 4.5 告警桥：全链路 async + 显式释放

```text
现在（坏）                                改成
────────────────────────────────────     ────────────────────────────────────
AlertBinding.report()  同步              async def report(...)
BangumiAlertBridge.tick() 同步           async def tick(now)
loop: asyncio.to_thread(alert_sync)      loop: await self.alert_sync()
bind() 后从不 close()                    child.effect(lambda: binding.close, ...)
```

要点：

1. `deploy_alert_bridge(child)` 在子 Fiber 里 `bind` 两个 source 后，
   立即用 `await child.effect(lambda: binding.close, label="bangumi-eventmail-source")`
   登记释放，随 Effect 逆序回收 —— 这样换代时 `source_id` 能被释放。
2. **`bind` 与 `effect` 之间要兜底 `close`**。`ctx.effect(...)` 本身可能失败
   （Fiber 已在卸载、Root 换代中），此时 binding 已经拿到所有权却没人释放。
   fitbit-mcp 的写法是：

   ```python
   alert_source = source_ctx.require(EVENTMAIL_ALERT_SOURCE).bind("bangumi")
   try:
       _ = await source_ctx.effect(lambda: alert_source.close, label="bangumi-alert-source-binding")
   except BaseException:
       alert_source.close()
       raise
   ```

   本插件两个 source（alert + context）都要这样处理，否则一次失败的 `apply`
   会把 `source_id` 永久占住，下一次装载直接报「已有 owner」。
3. `tick()` 里的 `store.fetch_pending` / `store.acknowledge` 是 SQLite 同步调用，
   保持 `asyncio.to_thread` 包装（**只包数据库**，不包网络与协程）。
4. `alert_sync` 的类型从 `Callable[[], object]` 变成 `Callable[[], Awaitable[object]]`，
   `AnimeUpdateLoop` 直接 `await`，不再 `to_thread`。

### 4.6 配置入口：插件自有离线命令（D1）

`configure.py` 的内容基本保留，改成 `plugin.py` 的 `entrypoints` 声明：

```python
entrypoints = {"bangumi-configure": "configure.main"}
```

`configure.main(arguments, *, workspace, config_path)` 由宿主的
`invoke_plugin_command` 在**宿主解释器进程内**直接调用（`agent/plugins/entrypoints.py:_invoke`
只做 `importlib.import_module` + 调用函数，**不 spawn 子进程**），因此：

- 命令能直接 import `agent.plugin_composition.config_input`（宿主已加载的模块）；
- 命令能直接用 `requests`（宿主 `requirements.txt` 已含 `requests>=2.32.0`），
  本仓库的 `requirements.txt` 对命令**不是必需**；
- 命令必须以 `return 0` / `return 非 0` 结束（宿主校验 `type(result) is int`）；
- 命令失败时 `main.py` 只捕获 `LookupError` / `ValueError`，其他异常直接冒栈，
  所以参数错误要抛这两类。

**`config_input` 的稳定性要说清楚，它比"能 import"复杂**：

- 这个模块**确实在公开清单里**：`scripts/plugin_boundary.py` 的
  `PLUGIN_ALLOWED_MODULES` 含 `agent.plugin_composition.config_input`，
  评审出处是 `docs/design/plugin-boundary-foundation.md` §9.37。所以它不是"偷用私有实现"。
- **但评审覆盖的符号面只有两个**：§9.37 原文写的是「`CONFIG_INPUT`、`load_config`」。
  本插件要用的是 `save_config` / `save_credential` / `upgrade_config`，**不在被评审的那一对里**。
- **而 R2 是模块粒度、不是符号粒度**：`check_plugin_deep_core` 只判
  `item.module in PLUGIN_ALLOWED_MODULES`，不校验 `from … import X` 里的 `X`。
  实测 `from agent.plugin_composition.config_input import save_config, save_credential,
  upgrade_config` 不会触发任何 R2 违规。也就是说「过了门」不等于「符号被审过」。
- 另一个事实：这个门**只扫宿主自己的 git 工作树**（`source_python_files()` 用
  `cwd=REPO_ROOT`），外置插件仓库根本不在扫描范围。对本仓库而言，
  `plugin_boundary.py check` 不会替你发现这类漂移。
- 这三个函数自 `4bf5a658`（"以固定配置输入和私有凭据版本取代插件字段脱敏"）引入后
  签名未变，但它们是宿主内部写盘实现，**没有对外兼容承诺**。

> 与 `configure.py` 旧时代的关键差别：以前是子进程 + `AKASHIC_PLUGIN_DATA_DIR`，
> 现在是同进程 + 显式 `workspace` 参数，不再有环境变量注入。

用户可见入口：

```bash
python main.py bangumi-configure            # 从环境变量写入固定配置
python main.py bangumi-configure --upgrade  # 旧 config.local.toml → 固定输入
```

- 数据目录不再从 `AKASHIC_PLUGIN_DATA_DIR` 猜，改为
  `workspace / "plugin-data" / f"{name}-{marketplace}"`。
  `workspace` 由命令签名直接给出；`marketplace` 从插件 ID 后缀取，
  无后缀时用 `builtin`。这正是 `agent/plugins/manifest.py:workspace_plugin_data_dir` 的规则。
- 明文 Token 仍只经 `save_credential` 进私有凭据库，不进 `config.input.json`、
  不进命令行参数、不进日志。
- 已实测：`entrypoints` 声明的命令用相对导入（`from .pkg.helper import …`）在
  `invoke_plugin_command` 的 `_plugin_command_<hex>` 前缀下可正常执行。

> 不选 Web 设置页（`UI.register_configuration` + `ONBOARDING`）的原因：需要手写
> React 前端模块并绑定 `shell.settings-plugins.v1` 契约摘要，回归面大；
> 本插件一年配置一次，离线命令够用。将来要接设置页时，`configure.py` 的
> 校验与归一化逻辑可原样搬到 `Settings.save()`。

### 4.7 后台刷新与生命周期（无变化，仅跟随合同）

`RUNTIME_STARTED` / `RUNTIME_STOPPING` 仍是 `agent.plugin_composition` 的导出，
`ctx.on(...)` 语义不变。需要保持的两条既有约束：

- candidate 校验期零调度：`TIMERS.schedule` 在 candidate 期会失败，
  当前代码靠「只在 `RUNTIME_STARTED` 里启动」满足，保持不变。
- 后台租约：`start_background_refresh` 自己开一份长期凭据租约并负责关闭，
  保持不变。

### 4.8 与 fitbit-mcp 的交叉验证

同组织仓库 `akashic-plugins/fitbit-mcp@5b6eef6` 是已经跑在 Akashic v3 上的外置插件，
用它对照本文的结论（只读，未修改）：

| 结论 | fitbit 的写法 | 与本文是否一致 |
|---|---|---|
| 插件根不在 `sys.path`，必须相对导入 | `from .tools import register_tools`、`from .src.content_adapter import …` | 一致 |
| 制品入口不做测试分支 | `plugin.py` 无 `try/except`、无 `__package__` 判断 | **本文据此改了方案**（原稿的 `__package__` 双路导入已废弃） |
| 测试侧伪造包身份 | `tests/conftest.py` 注册 `fitbit_test_plugin`，`__path__` 指向仓库根 | **本文采纳** |
| 工具注册无 `risk` | `register(ctx, name=…, description=…, parameters=…, open=…, idempotent=…)` | 一致 |
| 结果用宿主 `ContentPart` | `ContentPart("text", value.output)` | 一致 |
| eventmail v2 要显式 `close` | `bind(...)` 后用 `effect(lambda: source.close, …)`，`exception` 时再手动 `close()` | 一致，本文补上了兜底 `close` |
| v2 的 key 名 | `EVENTMAIL_ALERT_SOURCE` / `EVENTMAIL_CONTEXT_SOURCE` | 一致 |
| 配置入口 | **没有** `configure.py`、**没有** `entrypoints`，只在 `apply` 里 `FitbitConfig.model_validate(ctx.config)` | **不一致，见下** |

**唯一实质差异是配置入口。** fitbit 的凭据（Fitbit client_secret）由它自己的
`monitor` 子进程从 `plugin-data/**/monitor.config.local.toml` 读明文，不经过
`core.credentials`；它的 `ctx.config` 里只有轮询间隔这类非敏感值，所以不需要写入入口。
本插件的 Bangumi Access Token **必须**进私有凭据库（明文不得落 `plugin-data`），
`initial_config.json` 又只能承载非敏感默认值（内置 `telegram_channel` 就只写了
`{"enabled": null}`），因此仍需要一条能调用 `save_credential` 的显式入口。

选 `entrypoints` 而非 Web 设置页的理由不变（见 4.6）；若将来要接设置页，
`configure.py` 的校验与归一化逻辑可原样搬到 `Settings.save()`。

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `plugin.py` | 绝对导入 → **纯包内相对导入**（`from .bangumi.…`，无 `__package__` 分支）；`inject` 用新 key；`register` 去掉 `risk`、加 `parallel`；新增 `entrypoints` 字面量 |
| `bangumi/_host_contract.py` | 4 个 key 名更新；`register` 签名更新；`BoundAlertSource`/`BoundContextSource` 改 async + `close()` |
| `bangumi/tools.py` | `_TextPart`/`_ToolResult` → 宿主 `ContentPart`；`ToolEntry.risk` 降为内部元数据，新增 `parallel` |
| `bangumi/runtime.py` | 返回类型改 `ToolResultValue`；`deploy_alert_bridge` 登记 `close` Effect；`sync_pending_alerts` 改 async |
| `bangumi/alert_source.py` | `AlertBinding.report` / `is_terminal` / `BangumiAlertBridge.tick` 改 async；`AlertBinding.close()` |
| `bangumi/anime_push_loop.py` | `alert_sync` 改 await；删掉 `asyncio.to_thread(alert_sync)` |
| `configure.py` | **导入改为相对**（`from .bangumi._host_contract import CredentialRef`、`from .bangumi.settings import …`）；入口签名改 `(arguments, *, workspace, config_path)`；数据目录改由 `workspace` 推导；新增 `main()` 参数解析 |
| `requirements.txt` | 保持 `requests==2.32.5`。它**不是** `bangumi-configure` 的运行时前提（命令跑在宿主解释器里），但保留它会让安装器为本制品建一个根环境；删掉也能工作，见风险 2 |
| `tests/conftest.py` | 注册 `bangumi_test_plugin` 伪包（对齐 fitbit），测试改用它导入；不再依赖 `import plugin` 顶层导入 |
| `tests/*` | 契约测试断言新 key 名；`_host_contract` 桩同步 async/close；新增告警桥 async 与释放测试；`from bangumi.…` 改为 `from bangumi_test_plugin.bangumi.…` |
| `README.md` / `skills/bangumi/SKILL.md` | 安装与配置命令改 `bangumi-configure`；说明不再有 `main.py setup` 插件步骤 |
| `plugin.py` / `bangumi/settings.py` | `version` 与 `DEFAULT_USER_AGENT` 里的 `0.6.0` 一起升到 `0.7.0`（两处都要改，否则 User-Agent 与实际版本不符） |

## 6. 验证策略

**当前全绿：`pytest` 189 passed。** 测试必须在宿主环境跑（见下）。

| 文件 | 覆盖 |
|---|---|
| `tests/test_target_contract.py` | 本文承诺的契约形状：key 名、导入方式、结果类型、async/close、配置入口 |
| `tests/test_upstream_contract.py` | 同一批契约放进**真实宿主**验证：用宿主 `FreshPluginImporter` 加载、用宿主 `ServiceKey` 比较身份、用 `api.coerce_result` 校验结果 |
| `tests/test_alert_source.py` | 告警桥领域行为（async + close） |
| `tests/test_apply_registration.py` | apply 的工具注册（无 `risk`、有 `parallel`） |
| `tests/test_anime_push_loop.py` | 刷新循环与 lifecycle（await `alert_sync`、v2 key） |
| `tests/test_settings.py` | 配置解析与 configure 入口（`workspace` 参数） |
| `tests/test_service.py` 等 | 纯领域逻辑，本轮零改动 |

### 测试必须在宿主环境运行

工具结果现在直接是 `plugins.ledger.contract.ContentPart`，导入它等于导入宿主。
`tests/conftest.py` 会按 `AKASHIC_AGENT_ROOT` → `../akashic-agent` 的顺序找宿主，
找不到就在收集阶段明确失败，**不会用本地桩假装通过**。

```bash
AKASHIC_AGENT_ROOT=/path/to/akashic-agent \
  /path/to/akashic-agent/.venv/bin/python -m pytest
```

这与 `akashic-plugins/fitbit-mcp` 的 CI 一致（它同样 checkout 宿主并装宿主依赖）。
原设计里"插件 venv 里跳过上游用例"的做法已废弃：`plugins` 不再是可选依赖。

### 测试如何导入相对导入的 `plugin.py`

按 fitbit 的做法，在 `tests/conftest.py` 注册伪包 `bangumi_test_plugin`，
`__path__` 指向仓库根；测试写 `from bangumi_test_plugin import plugin`。
`plugin.py` 保持纯相对导入，不含任何 `__package__` / `try-except` 分支。

### 本轮删除的测试（及其原因）

重构会让一部分测试**失去被测对象**，这些直接删除，而不是改写成测别的东西：

| 删除项 | 原因 |
|---|---|
| `test_configure_requires_data_dir` | 断言 `main.py setup` 必须注入 `AKASHIC_PLUGIN_DATA_DIR`；该注入已不存在，数据目录改由 `workspace` 推导，行为由 `test_configure_*` 的新签名覆盖 |
| `test_alert_source_protocol_exposes_close` | 只检查 `hasattr`，与 `test_binding_close_releases_both_sources`（真实释放行为）重复，保留强的 |
| `test_alert_binding_releases_both_sources`（形状版） | 同上，存在性检查被行为测试完全覆盖 |
| `test_real_eventmail_rejects_duplicate_binding_before_close` | 用 `inspect.getsource` 匹配字符串，实现一重构就误报且几乎不会失败；同名语义由 `test_source_id_can_be_rebound_after_release` 真实覆盖 |
| `test_plugin_declares_v3_identity_literals` | 与 `test_plugin_identity_matches_host_ast_loader_rules` 重复，且硬编码 `version == "0.6.0"`，与本轮升版冲突 |

同时**改写而非删除**的：`test_alert_source.py`（同步→ `run()` 驱动协程）、
`test_anime_push_loop.py`（`alert_sync` 改 async、v1 key 改 v2）、
`test_apply_registration.py`（假 service 去 `risk` 加 `parallel`）、
`test_settings.py`（configure 改 `workspace` 参数、文档断言改指向 `bangumi-configure`）。
这些文件里测的**领域行为没变**，变的只是调用方式。

### 测试基础设施的两处约束（踩过的坑）

- **宿主路径只能追加、不能前置**：宿主仓库也有 `tests/` 目录，前置会让
  `import tests.…` 解析到宿主的 tests 包，本仓库的测试辅助模块集体消失。
- **`tests/` 必须是真包**：加 `tests/__init__.py` 之前，`tests` 是 namespace 包，
  一旦宿主路径进入 `sys.path` 就可能整个解析到宿主目录。加包文件后解析固定。

### 运行方式

```bash
# 插件仓库自身（无宿主时 9 个上游用例跳过）
pytest

# 上层契约校验（用宿主解释器，宿主有 pythonjsonlogger 等依赖）
AKASHIC_AGENT_ROOT=/path/to/akashic-agent \
  /path/to/akashic-agent/.venv/bin/python -m pytest tests/test_upstream_contract.py
```

## 7. 风险与回滚

- **风险 1**：`plugins.ledger.contract` 的 `ContentPart` 属于 `ledger` 插件，
  若 `ledger` 未启用则 import 失败。实测 `ledger` 在 `bundles/base.toml` 里且是
  `tools` 的强依赖，实际不可缺席。
- **风险 2**：制品内任何名为 `requirements.txt` 的文件都会让安装器建 Python 环境
  （`_prepare_static_python_runtimes`），包括**空文件**——第一方 `plugins/prompt` 就是
  一个空的 `requirements.txt`。本仓库根目录的 `requirements.txt` 会让安装多建一个
  根环境，但 `bangumi-configure` 并不需要它（跑在宿主解释器里）。
  保留 = 多一份环境开销；删除 = 安装更快、也没有环境需要维护。
  **建议：保留**，因为 `requirements-dev.txt` 与本地 `.venv` 的既有工作流依赖它。
- **风险 3**：`bangumi-configure` 是新命令名，老用户按 README 找 `main.py setup` 会失败；
  README 与 `SKILL.md` 必须同步，且保留 `--upgrade` 提示。
- **风险 4**：`invoke_plugin_command` 只从**已提交选择**（`plugin-stable.json`）里找命令
  provider，失败有**两条不同路径**，README 的提示必须覆盖两种：

  | 情形 | 报错（`agent/plugins/entrypoints.py`） | 行号 |
  |---|---|---|
  | 完全没有已提交选择（插件从未安装） | `当前空选择没有命令 provider: bangumi-configure` | `:33` |
  | 有选择，但匹配到的 provider 数 ≠ 1（装了但未启用、`entrypoints` 声明与静态 manifest 不一致等） | `命令 bangumi-configure 需要唯一 provider，当前有 N 个` | `:50` |

  两者都是 `LookupError`，`main.py` 统一以退出码 2 结束。
  所以 README 不能只写“先 plugin-install”，要写「先安装**并启用**，再跑
  `bangumi-configure`」。
- **风险 5（`bangumi-configure` 依赖宿主内部写盘实现）**：
  `configure.py` 通过 `agent.plugin_composition.config_input` 的
  `save_config` / `save_credential` / `upgrade_config` 落盘。这三个函数
  **在模块级公开清单里、但不在 §9.37 评审的符号面（`CONFIG_INPUT`、`load_config`）里**，
  且 R2 是模块粒度检查、外置插件仓库也不在门的扫描范围内（见 4.6）。
  后果与表现：
  - 若上游改了这三个函数的签名或语义，`bangumi-configure` 会**在运行时**失败，
    而不是在安装或静态检查阶段被发现；
  - 最坏情况是写坏 `config.input.json`（格式版本、字段编码、凭据引用形态变化），
    而 `load_config` 对格式错误是 `ValueError("固定配置输入格式错误；须显式升级")`，
    插件随后加载失败。
  - 缓解：`configure.py` 写完立即回读 `load_config(data_dir)` 自校验，失败则不改变
    原有 `config.input.json`（`save_config` 本身会先把旧文件备份到
    `.plugin-credentials/<owner>/config-history/`，可人工恢复）。
  - 检测：升级宿主版本后必须重跑一次 `bangumi-configure` 的干跑校验，不能只看
    「命令能 import」。
- **回滚**：全部改动在 `codex/` 分支；`plugin-data/` 与 `anime_updates.db` 不删不改；
  凭据库不重写（`--upgrade` 只追加恢复点）。

## 8. 完成情况

全部完成，`pytest` 189 passed、`ruff check` 无新增错误。

- [x] 4.1 纯相对导入 + `tests/conftest.py` 伪包（对齐 fitbit）
- [x] 4.2 `_host_contract.py` key 名与 Protocol 刷新（4 个 key + async/close + register 签名）
- [x] 4.3 工具结果改用宿主 `ContentPart`
- [x] 4.4 工具注册去 `risk`、只读工具加 `parallel`
- [x] 4.5 告警桥 async 化 + `close()` Effect 登记 + `bind`/`effect` 之间兜底 close
- [x] 4.6 `entrypoints` 命令接线 + 数据目录由 `workspace` 推导 + 写完回读自校验
- [x] 版本升 `0.7.0`（`plugin.py` 与 `bangumi/settings.py` 的 User-Agent）
- [x] 旧测试按契约改写（alert_source / apply_registration / anime_push_loop / settings）
- [x] README / SKILL.md 更新
- [x] CI 改为 checkout 宿主并按宿主环境跑测试

**未做**：用真实安装链做一次 `plugin-install` + `plugin-doctor` 端到端验证
（需要运行中的 Gateway 与真实 workspace，本轮只做静态与单元验证）。

## 9. 本轮不改的东西

- `bangumi/service.py`、`query.py`、`client.py`、`anilist.py`、`anime_updates.py`
  五个领域模块：它们不接触宿主合同，本轮零改动。
- 工具名、JSON Schema、确认语义、放送事件库结构：保持不变，避免影响已有对话与数据。
- 不新增 `workspace_roots` / `workspace_files`：本插件的数据都在 `ctx.data_root`。
