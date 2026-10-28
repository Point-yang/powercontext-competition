# PowerContext 参赛仓库与题目分支

PowerContext 全部题目使用报名时登记的同一个仓库地址，通过 Branch 选择不同题目的代码，通过 Commit ID 固定待测版本。各题保留完整的 PowerContext 工程结构，产品实现放在 `src/powercontext/`；不要把项目搬到题号子目录。

## 仓库地址

| 方向 | 仓库根地址 | 用途 |
| --- | --- | --- |
| PowerContext | <https://github.com/Point-yang/powercontext-competition> | 本文所列的十一道 PowerContext 题目 |
| seekdb | <https://github.com/Point-yang/seekdb-competition> | seekdb 方向的独立代码仓库 |
| MiniOB 训练 | <https://github.com/Point-yang/miniob-training> | MiniOB 历年练习 |

提测的“代码仓库地址”填写上表的根地址，不追加 `/tree/分支名`、文件名或子目录。报名已锁定地址时，后续各题继续使用已登记仓库。

## 当前可用的提交

| 题目 | Branch | Commit ID | 代码准备情况 |
| --- | --- | --- | --- |
| 一 准备 PowerContext 参赛仓库 | `chore/repository-setup` | `dab240063bfcfa7917898dc44da4baadb3747bd0` | 已通过（参赛者确认）；保留官方冻结基线历史 |
| 二 CSV 行级采集与原始记录回查 | `feat/csv-row-source` | `d78a4cfb78d67ed6686b83bd104790c50132fde7` | 已通过（参赛者确认）；CSV 实现保留在累计版本 |
| 三 Pi 会话分支采集与断点恢复 | `feat/pi-history` | 从对应分支最新提交复制完整 40 位 SHA | 完整事件、持久化断点与公开自测已完成 |

Branch 是代码分支，例如 `feat/csv-row-source` 表示 CSV 功能分支；名称中的 `/` 是分支名的一部分。Commit ID 是一次提交的完整标识。提交后分支还可以继续更新，但已经填写的 Commit ID 始终代表同一个版本。

第一题原来提交的 `2026-competition` 和 `dab240063bfcfa7917898dc44da4baadb3747bd0` 也是有效的历史组合。专用分支只是便于区分题目，改名本身不能修复评测读取权限或平台执行故障。

## 分支规划

| 题目或用途 | 分支 | 状态 |
| --- | --- | --- |
| 官方公开赛题起点 | `baseline/2026-competition` | 已建立，固定为 `dab240063bfcfa7917898dc44da4baadb3747bd0`，不提交个人实现 |
| 已完成题目的累计版本 | `2026-competition` | 默认分支，包含 CSV 与 Pi 历史采集实现 |
| 一 仓库准备 | `chore/repository-setup` | 已建立 |
| 二 CSV 行级采集 | `feat/csv-row-source` | 已建立 |
| 三 Pi 会话分支采集与断点恢复 | `feat/pi-history` | 已建立，包含累计版本中的 CSV 功能 |
| 四 财报 PDF 全文解析与证据页检索 | `feat/pdf-retrieval` | 计划，开始该题时创建 |
| 五 HTTP 重排服务接入与跨批次排序 | `feat/http-reranking` | 计划，开始该题时创建 |
| 六 上下文增量传输与完整重建 | `feat/incremental-context` | 计划，开始该题时创建 |
| 七 按新增来源条目数触发记忆处理 | `feat/count-trigger` | 计划，开始该题时创建 |
| 八 Embedding 并发请求合并与向量缓存 | `feat/embedding-batches` | 计划，开始该题时创建 |
| 九 来源撤回后的记忆停用与恢复 | `feat/source-lifecycle` | 计划，开始该题时创建 |
| 十 按查询和预算选择原文证据 | `feat/evidence-selection` | 计划，开始该题时创建 |
| 十一 seekdb 混合检索的单 SQL 候选查询 | `perf/hybrid-search` | 计划，开始该题时创建 |

通常从 `baseline/2026-competition` 创建各题分支；题目存在已实现功能依赖时，从累计分支建立，并记录依赖。每题验证通过后合入 `2026-competition`，更新本表的 Commit ID。已用于提测的提交保留在历史中，不强制改写。

平台允许同一仓库和 commit 包含多道题的实现，并按所选题分别验收。分支划分用于管理代码，不会自动把整组提测限定为一道题。需要提交所有已完成实现时，选择默认分支 `2026-competition`，并从 GitHub 复制它当时的完整 Commit ID。

`main` 保留最初的 README 初始化提交，未包含 PowerContext 源码，不用作参赛代码版本。

## 第三题实现与提测

本题在已包含 CSV 功能的累计版本上开发。实现位于 `src/powercontext/sources/pi_session.py`，不修改官方评测资产或固定依赖。

- `PiSessionInput` 和 `parse_pi_session` 完整验证 UTF-8 Pi v3 JSONL，根据默认或显式叶节点返回所选分支的全部可见事件。
- 保留控制节点的父链关系、压缩前原文、工具调用、完整工具结果、摘要与完整输入 record。
- `PI_EVENT_SOURCE_DEFINITION` 提供 CAPTURED Source、稳定事件身份、标准文本证据投影及原始事件回读。
- `PiSessionConnector` 通过已有 `ConnectorLifecycle` 保存 checkpoint，支持切分支、追加、进程重启、失败恢复和已采集记录改写检查。
- Source 保存完整大文本，派生 Memory 的大小限制不会截断原始事件。

在平台的 PowerContext 题目中选择“赛题三：Pi 会话分支采集与断点恢复”，提测填写：

```text
代码仓库地址：https://github.com/Point-yang/powercontext-competition
Branch：feat/pi-history
Commit ID：复制该分支已上传版本的完整 40 位提交标识
```

报名页面自动带出的仓库地址保持原有登记配置。不要在仓库根地址后追加 `/tree/feat/pi-history` 或题号目录。

从 GitHub 对应分支的最新提交页面复制 Commit ID，或在本地执行：

```bash
git rev-parse feat/pi-history
```

需要提测当前所有已完成实现时，也可使用累计分支 `2026-competition` 的最新完整 Commit ID。第一、二题的专用分支及已提交历史仍保留。

## 提交前验证

官方公开自测使用 `competition/.venv` 中的 Python 3.12 和固定依赖：

```bash
make -C competition check-baseline test-pi test-csv
```

产品专项测试及真实 SQLite 持久化回归：

```bash
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  competition/.venv/bin/python -m pytest -p pytest_asyncio.plugin \
  -o asyncio_mode=auto -q tests/test_pi_session.py tests/e2e/test_pi_session_persistence.py
```

本地验证包括默认与显式叶节点、工具调用和结果、压缩摘要之前的事件、Unicode 与原文空白、分支共享祖先去重、重启恢复、失败后继续、历史改写拒绝，以及大工具结果的逐事件精确引用回读。

本题源文件与两个自有测试文件的 Ruff、格式和类型检查通过。全库类型检查仍有其他未实现赛题接口及公开验收代码的既有错误；没有修改这些官方资产。

本地公开测试和产品回归不生成正式成绩。平台可能对整组题目运行评测，后续题目尚未实现时，应按分题结果确认第三题；正式通过情况以平台回传为准。

官方题面：[Pi 会话分支采集与断点恢复](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/oi71gg4g80uda2ls)。
