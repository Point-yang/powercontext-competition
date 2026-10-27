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
| 一 准备 PowerContext 参赛仓库 | `chore/repository-setup` | `dab240063bfcfa7917898dc44da4baadb3747bd0` | 保留官方冻结基线历史；平台读取权限仍须满足 |
| 二 CSV 行级采集与原始记录回查 | `feat/csv-row-source` | `d78a4cfb78d67ed6686b83bd104790c50132fde7` | 官方公开测试 17 项、相关回归 71 项通过；未确认正式评测成绩 |

Branch 是代码分支，例如 `feat/csv-row-source` 表示 CSV 功能分支；名称中的 `/` 是分支名的一部分。Commit ID 是一次提交的完整标识。提交后分支还可以继续更新，但已经填写的 Commit ID 始终代表同一个版本。

第一题原来提交的 `2026-competition` 和 `dab240063bfcfa7917898dc44da4baadb3747bd0` 也是有效的历史组合。专用分支只是便于区分题目，改名本身不能修复评测读取权限或平台执行故障。

## 分支规划

| 题目或用途 | 分支 | 状态 |
| --- | --- | --- |
| 官方公开赛题起点 | `baseline/2026-competition` | 已建立，固定为 `dab240063bfcfa7917898dc44da4baadb3747bd0`，不提交个人实现 |
| 已完成题目的累计版本 | `2026-competition` | 默认分支，已包含 CSV 实现和本提交说明 |
| 一 仓库准备 | `chore/repository-setup` | 已建立 |
| 二 CSV 行级采集 | `feat/csv-row-source` | 已建立 |
| 三 Pi 会话分支采集与断点恢复 | `feat/pi-history` | 计划，开始该题时创建 |
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

## 第一题的提交与核查

第一题单独留存的仓库准备版本如下：

```text
代码仓库地址：https://github.com/Point-yang/powercontext-competition
Branch：chore/repository-setup
Commit ID：dab240063bfcfa7917898dc44da4baadb3747bd0
```

此版本不包含第二题 CSV 实现。若从 PowerContext 整组提测入口提交所有已完成题目，使用累计分支 `2026-competition` 当时的最新完整 Commit ID，或使用 `feat/csv-row-source` 与 `d78a4cfb78d67ed6686b83bd104790c50132fde7`。换用第一题专用分支本身不能解决先前的执行失败。

第一题要求仓库可读取，且保留官方 Fork 关联或至少一个分支包含冻结基线 `9db1753434162c0aa7c9f1189a1722e617880a52`。当前导入仓库采用后一个条件；第一题无需新代码、专用分支或 PR。见[官方第一题说明](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/ygfg2iskh62zntl5)及[仓库中的题面](competition/challenges/setup-00-repository-fork/instruction.md)。

2026 年 10 月 27 日的检查结果：

- 15:40 提测记录的仓库根地址、分支和完整 Commit ID 均与 GitHub 实际历史对应；该提交包含冻结基线。
- 该记录显示“执行失败”，结果列为 `-`，问号未展示具体报错；页面没有提供足以确定失败原因的日志。
- 仓库仍为 Private。个人 GitHub 登录和本地推送权限不能证明官方评测系统已获得读取权限。私有仓库须按赛事约定授权；选择公开仓库则需仓库所有者确认公开。
- 16:52 的 CSV 提测记录仍显示“待执行”，不能据此判定 CSV 实现失败。

因此，现有证据不支持将失败归因于填写路径或分支名称。下一步应核实官方读取权限，或取得这次任务的后台错误日志。若权限无误，再排查 GitHub 网络、认证、限流及评测服务异常。平台未给出分题结果时，也不能仅凭整组“执行失败”判断第一题的正式判分。

本地可检查提交历史：

```bash
git merge-base --is-ancestor \
  9db1753434162c0aa7c9f1189a1722e617880a52 \
  chore/repository-setup
make -C competition check-baseline
```

命令成功只证明本地历史符合基线要求，不证明评测系统能够读取私有仓库。
