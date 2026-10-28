# PowerContext 参赛仓库与提测

仓库根地址：<https://github.com/Point-yang/powercontext-competition>。

默认累计分支为 `2026-competition`，包含第一至十一题的已实现功能。产品代码保持完整工程结构，位于 `src/powercontext/`；各题没有搬到单独的题号目录。

## 提测填写

在 PowerContext 题目页点击“立即提测”，使用累计版本：

```text
代码仓库地址：https://github.com/Point-yang/powercontext-competition
Branch：2026-competition
Commit ID：该分支已上传版本的完整 40 位 SHA
```

仓库地址自动填入时保持原有报名配置。不要在根地址后追加 `/tree/分支名` 或源码路径。普通网页地址不包含私有仓库的读取权限；需要认证时按官方参赛指南在报名页面配置，凭据不要写入源码或本文。

从 GitHub 最新提交详情复制 SHA，或在本地执行 `git rev-parse 2026-competition`。提交后查看各子题的结果。需要单独复测某题时，可使用下表对应的历史分支与 SHA；提交后这些版本仍保留。

## 各题留存版本

| 题目 | Branch | Commit ID | 状态 |
| --- | --- | --- | --- |
| 一 准备参赛仓库 | `chore/repository-setup` | `dab240063bfcfa7917898dc44da4baadb3747bd0` | 已通过（参赛者确认） |
| 二 CSV 行级采集 | `feat/csv-row-source` | `d78a4cfb78d67ed6686b83bd104790c50132fde7` | 已通过（参赛者确认） |
| 三 Pi 会话历史 | `feat/pi-history` | `784d33e8a57461e77eb9e09b2221850673c75cf6` | 公开自测通过 |
| 四 PDF 全文与证据页检索 | `feat/pdf-retrieval` | `8c2d4e4ed315efa59945c6a016445aa42c4bde42` | 公开自测通过 |
| 五 HTTP 重排 | `feat/http-reranking` | `5f69b92cccf85939935b2430aa09c53e99b30feb` | 公开自测通过 |
| 六 上下文增量传输 | `feat/incremental-context` | `e8234b9662cd580815c702cc7d63b6b4b66221eb` | 公开自测通过 |
| 七 来源数量触发 | `feat/count-trigger` | `8bed194fc220b2c3c44f1655b240ad9da53024fb` | 公开自测通过 |
| 八 Embedding 并发合并 | `feat/embedding-batches` | `74f9144ec375c642b207535b86214efad5b99eed` | 公开自测通过 |
| 九 来源生命周期 | `feat/source-lifecycle` | `c284088bf0329d62007ba35c0bb78fa1160ee4bc` | 公开自测通过 |
| 十 查询驱动原文选择 | `feat/evidence-selection` | `0973a8c536d9cffbde1b1ca899469714d5da076b` | 公开自测通过 |
| 十一 原生单 SQL 混合检索 | `perf/hybrid-search` | `a6258f0f0b119c43fb10bc7717788bfcd74c3025` | 指定 seekdb 版本原生自测通过 |

各分支从已有累计版本继续开发，因此可以同时包含其他已完成题目。`baseline/2026-competition` 固定保留公开比赛起点 `dab240063bfcfa7917898dc44da4baadb3747bd0`；官方冻结产品基线 `9db1753434162c0aa7c9f1189a1722e617880a52` 保留在历史中。`main` 仅保留最初初始化提交，不作为代码提测版本。

## 后续题目实现

- PDF：完整解析所有物理页，保留空白页和修订摘要，经真实 scoped Memory 检索并返回不同页的精确引用。
- HTTP 重排：校验每批全局 rank 与有限分数，跨批稳定排序，保留原 Memory 身份和实际请求用量。
- 增量上下文：完整 Pack 与 Delta 可独立序列化、校验和重建；普通 prepare 行为兼容，空内容及 UTF-8 边界合法。
- 数量触发：按 scope 和处理族的成功游标计算新增 Source 数量，通过已有持久化调度机制接受、执行和恢复任务。
- Embedding：共享重复和执行中的文本请求，限制批次，隔离取消与局部失败，提供 LRU 和每调用准确用量。
- 生命周期：持久化来源依赖、事件与状态，原生停用或恢复 Memory，过滤不可用 Topic，保留历史正文和精确引用。
- 原文选择：默认关闭，启用后按 query 选择原文字符区间，正文、citation、origins 和完整 UTF-8 输出预算一致。
- 原生混合检索：一条 SQL 获取全文和向量候选；标签身份在 SQL 内一次物化，进入向量索引预过滤，避免 ANN 截断后的标签漏召。

## 公开自测

官方命令：

```bash
make -C competition check-baseline
make -C competition test
```

固定 Linux AMD64 环境：

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 POWERCONTEXT_CANDIDATE="$PWD" \
  docker compose -f competition/compose.yaml run --rm --no-deps \
  -e NO_PROXY=127.0.0.1,localhost -e no_proxy=127.0.0.1,localhost \
  selftest make -C /selftest test PYTHON=python PRODUCT=/candidate
```

本次整合版本在该环境完整运行通过下表 95 项官方公开测试。该环境使用 Python 3.12、官方固定依赖、`pylibseekdb==1.3.0.post4`，离线运行，限制 1 CPU／2 GiB，产品源码和官方资产只读。在 Mac ARM 主机通过 AMD64 仿真运行时，不用仿真延迟声称正式平台性能达标。

| 公开题目测试 | 用例数 |
| --- | ---: |
| CSV | 17 |
| Pi | 15 |
| PDF | 6 |
| HTTP 重排 | 10 |
| 增量上下文 | 14 |
| 数量触发 | 17 |
| Embedding 合并 | 8 |
| 生命周期 | 4 |
| 原文选择 | 3 |
| 原生混合检索 | 1 |
| 合计 | 95 |

核心产品整合回归 296 项通过。新增产品回归在 `tests/`，包括真实 SQLite 持久化、后台任务恢复、Unicode 和协议边界、跨 scope 引用、取消与失败恢复。额外原生回归验证稀疏标签、空结果、当前身份及两类索引计划；384 维、三个 scope 的 36 次合成对照中，两通道候选排序与冻结双查询路径一致。

本次改动的 Python 源码与自有测试通过 Ruff、格式和类型检查，产品依赖与锁文件未改变。全库预提交检查仍发现官方公开资产中的既有类型或格式诊断；这些文件保持原样。

公开测试和合成对照不生成正式成绩，也不能证明未发布数据上的检索质量、完整 40 轮传输成本或正式平台延迟。最终分题结果以平台回传为准。
