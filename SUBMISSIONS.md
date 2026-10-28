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
| 三 Pi 会话历史 | `feat/pi-history` | `784d33e8a57461e77eb9e09b2221850673c75cf6` | 正式提测已得分（参赛者截图） |
| 四 PDF 全文与证据页检索 | `fix/pdf-statement-evidence` | `9895a4b5d5f561794cecd75ee48b98a81fe299c7` | 已修复指定报表遗漏，待正式重提 |
| 五 HTTP 重排 | `feat/http-reranking` | `5f69b92cccf85939935b2430aa09c53e99b30feb` | 正式提测已得分（参赛者截图） |
| 六 上下文增量传输 | `feat/incremental-context` | `e8234b9662cd580815c702cc7d63b6b4b66221eb` | 正式提测已得分（参赛者截图） |
| 七 来源数量触发 | `feat/count-trigger` | `8bed194fc220b2c3c44f1655b240ad9da53024fb` | 正式提测已得分（参赛者截图） |
| 八 Embedding 并发合并 | `fix/embedding-usage` | `047ffaff478268cb73b1b8ea510f4c3990b4005a` | 协议修复已上传，本轮未报告失败 |
| 九 来源生命周期 | `feat/source-lifecycle` | `c284088bf0329d62007ba35c0bb78fa1160ee4bc` | 正式提测已得分（参赛者截图） |
| 十 查询驱动原文选择 | `feat/evidence-selection` | `0973a8c536d9cffbde1b1ca899469714d5da076b` | 正式提测已得分（参赛者截图） |
| 十一 原生单 SQL 混合检索 | `fix/hybrid-candidate-order` | `5aeb7f227d37045f232d13c1ad5cb4ea7dbc9bb9` | 已修复同距候选顺序退化，待正式重提 |

各分支从已有累计版本继续开发，因此可以同时包含其他已完成题目。`baseline/2026-competition` 固定保留公开比赛起点 `dab240063bfcfa7917898dc44da4baadb3747bd0`；官方冻结产品基线 `9db1753434162c0aa7c9f1189a1722e617880a52` 保留在历史中。`main` 仅保留最初初始化提交，不作为代码提测版本。

## 正式反馈与复核状态

参赛者确认累计提交 `91926534e38d1ee11d12b1bf221675c1e1ce915e` 中第四、十一题仍未通过，已排除误用旧提交的情况。此前截图中第一、二、三、五、六、七、九、十题已得分，第八题的协议修复已保留。平台未提供失败阶段或错误详情，以下记录的是已经复现并修复的问题，不代表已经确定全部正式失败原因。

- 第四题已用真实 PDF 和 SQLite 公共接口复现：问题要求查看利润表时，重复出现关键词的附注挤掉报表正文。检索识别问题指定的利润表、现金流量表及资产负债表，保留正式标题和续页，并在涉及多种报表时分别保留候选；支持 P&L、D&A 和断行标题，区分综合收益单表与利润表，正式标题优先于附注中的裸列名。完整公开 PDF 的 503 页经过真实入库、重启与检索后，指定报表实际返回。原有发行人识别与完整候选池修复保留，页原文、摘要和身份不变。候选仍来自公开 `Memory.search`，引用仍通过 `Memory.get` 精确读取；没有读取答案或标注证据。
- 第八题已复现两个协议问题：包装器对 provider 成功返回的向量增加了数量以外的坐标限制；部分批次 token 用量未知时错误返回已知部分之和。现仅在包装器核对向量数量，真实 Memory 的维度和有限数校验保留；用量有未知项时汇总保持未知。
- 第十一题已在固定原生环境复现排序退化：合法等距向量被合并 SQL 额外按身份排序，使同一证据的完整搜索排名从冻结路径第 1 降为第 10，MRR 从 1 降至 0.1。修复保留两路候选与冻结查询相同的正文 JOIN，在 ANN 有序 LIMIT 后记录候选序号，UNION 后恢复向量通道的原生顺序。仍执行一条候选 statement，保留两类索引、数据库内过滤、当前 head 校验和原有单通道路径。自建同距反例、173 篇自建文档的对照不能代替正式未公开语料的逐查询质量验收。

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

累计基线 `59599db4c01f75a37503105d3ca665e96be057ca` 在该环境完整通过下表 95 项官方公开测试；修复版本再次运行 PDF、Embedding 与原生混合检索，共 15 项全部通过。该环境使用 Python 3.12、官方固定依赖、`pylibseekdb==1.3.0.post4`，离线运行，限制 1 CPU／2 GiB，产品源码和官方资产只读。在 Mac ARM 主机通过 AMD64 仿真运行时，不用仿真延迟声称正式平台性能达标。

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

本轮受影响路径的产品整合回归 172 项通过，15 项因可选运行环境跳过；原生测试在固定 Linux 环境另外执行。新增产品回归在 `tests/`，覆盖真实 PDF/SQLite 入库后的指定报表检索、报表组合、断行标题、裸列名与综合收益表干扰，以及原生等距向量的候选排序。此前累计版本的 299 项核心回归记录保留；本轮没有修改其他已得分题目的产品实现。

第十一题的 7 项产品回归在固定原生环境全部执行通过，包含主机环境跳过的两个原生用例。三个 scope 各先完成 173 篇自建文档和 384 维单位向量入库，再执行 60 次完整对照，候选排序无差异，自建逐查询质量无退化，每次恰好一条候选 SQL；观测 p50 为 62.79 ms。连续 10 次同距完整搜索与冻结路径结果相同，实际执行计划同时包含 `TEXT RETRIEVAL SCAN` 和 `VECTOR INDEX ADAPTIVE SCAN`，并显示候选 LIMIT 物化之后才执行窗口编号。这些结果来自固定环境及自建输入，不是正式数据的成绩或正式平台延迟证明。

本次改动的 Python 源码与自有测试通过 Ruff、格式和类型检查，产品依赖与锁文件未改变。全库预提交检查仍发现官方公开资产中的 189 项既有类型诊断及格式问题；工具自动修改的官方资产已恢复，未随提交更改。

公开测试和合成对照不生成正式成绩，也不能证明未发布数据上的检索质量、完整 40 轮传输成本或正式平台延迟。最终分题结果以平台回传为准。
