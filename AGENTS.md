# Repository Agent Rules

本文件适用于整个 `bangumi-mcp` 仓库。

## Git 操作边界

- Agent 可以读取、编辑、测试、查看 diff、暂存并创建本地 commit。
- 用户说“实现”“更新”“修复”或“提交”时，默认授权边界止于本地 commit，不包含任何远端操作。
- `git push`、force push、远端分支创建或删除、创建或修改 PR、合并或关闭 PR、创建 tag 或 release、修改 GitHub 仓库设置，默认全部由用户执行。
- 只有用户在当前消息中明确要求 Agent 执行具体远端操作时，Agent 才能执行该项操作。之前任务中的授权不得沿用到后续任务。
- 未获得明确授权时，Agent 在本地 commit 后必须停止，并报告当前分支、commit SHA、验证结果和工作区状态。
- Agent 可以按用户要求提供 PR title 和 description，但不得因此自行创建或更新 PR。
- 不得为了“完成流程”而推断 push、PR 或 merge 已获授权。

## 分支与提交

- 不直接在受保护的 `main` 上提交；默认从最新 `main` 创建 `codex/` 前缀的任务分支。
- commit 使用约定式提交格式，并使用中文描述，例如 `docs:添加用户收藏列表设计`。
- 未获明确授权时，不得改写已经发布的分支历史。
- 发现工作区存在用户改动时必须保留，不得回退、覆盖或混入无关提交。

## 合并方式与分支收尾

本仓库 PR 使用 **squash merge**：合并后 `main` 上是一个新提交（带 `(#N)` 后缀，
committer 为 `GitHub`），**分支上的原始提交不会进入 `main`**。

由此产生三条硬约束：

- **每次新建任务分支必须基于最新 `main`**，不得基于任何已合并的分支。
  否则分支会带着「内容已进 `main`、SHA 未进 `main`」的提交，
  在 GitHub 上表现为无法自动合并的假冲突。
- PR 合并后按顺序收尾：
  1. `git checkout main && git pull --ff-only`
  2. 删除已合并的本地分支；远端分支删除需用户明确授权。
- 若发现分支已基于错误基线，修正方式为
  `git rebase --onto origin/main <错误基线> <当前分支>`，
  使分支只保留真正未合并的提交。rebase 之后必须 force push，
  且**须用户明确授权**（见「Git 操作边界」）。

## 仓库与凭据边界

- `/Users/lfegg/Documents/GitHub/akashic-agent` 只允许按任务需要只读参考，不得修改、暂存或提交其中任何文件。
- Bangumi Access Token 只能保存在 Akashic plugin-data 的私密配置中，不得写入 Git、日志、命令参数或回复。
