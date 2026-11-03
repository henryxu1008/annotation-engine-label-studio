# #84 收口矩阵与下一阶段边界

2026-11-03；只记录本轮可验证的源码/既有证据及拟议设计，不更新或关闭 Issue。
远端 main：`0143fe04e4b70dec5f7c124f85a8fe7abb5001da`，tree
`cad7ced4c33e58bb3fe7d30f2b813788b0f271c4`。本地设计从此 exact commit 分叉。
[#84](https://github.com/henryxu1008/annotation-engine-label-studio/issues/84) 与
[#81](https://github.com/henryxu1008/annotation-engine-label-studio/issues/81) 本轮读到均 OPEN。
#84 body/5个checkbox 自2026-10-21未更新，不能当作当前实现缺失清单。

## 1. 证据等级

- IMPLEMENTED：在上述 exact tree 中可读到实现与回归入口；不等于真实部署/业务验收。
- HISTORICAL_SUCCESS：已有 sealed 执行回执，本轮复核其来源，不重新执行其工作负载。
- PROPOSED / NOT_RUN：本轮设计或未来验收，不能混入当前功能通过数。
- DEFERRED：明确不属首版；UNKNOWN 是未建立事实，不能写作通过或已阻塞。

PR #133 实际 merge/main 的历史证据根：
`C:/Users/Henry/.codex/reports/ls84-handoff-focus-merge-20261103/`，
`final.json`、`final-verified.json`、`last-read.json`、`evidence-seal.json`。
reviewed head `5f05f3b7a8f3b8a22e329d0d7d58aca0ebff1827`；实际 squash merge 为上述 main。
记录的19个 main/push workflows、34 jobs均 SUCCESS；required checks为
Authorization foundation tests `125784924960`、Current enterprise UI validation `125796640899`。
额外 scheduled stale `41600927465` 单列，不能混算为20个push workflows。
这些是既有运行，不是新设计的远端验收。

其中 field-handoff/main 既有记录为 PG944通过/4跳过、Jest343通过、Chrome双语2/2；
Enterprise/main 为25次浏览器调用、147 browser tests，prepare4921通过/5既有pending。
不是所有历史 slice 都重跑了一遍，更不是模型/生产数据/Host 验收。
设计阶段不需为文档重跑整套 CI；当前没有已证明仍未解决的 CI 超时阻塞。
新增功能若改变 CI coverage，仍须复用已治理的完整清单、fail-closed聚合和producer/view核验。

## 2. 原 #84 五项验收逐项映射

下列源码链接相对本文，均绑定本设计的基线；测试名是回归入口，不声称本轮执行。

| #84 原验收 | 当前实现和源码依据 | 已有执行证据/剩余解释 | 当前判定 |
| --- | --- | --- | --- |
| 1. 比例/人员/冲突规则在合成数据稳定重放 | [sampling](review-sampling-v1.md)、[priority](review-priority-v1.md)、[NER](ner-review-priority-v1.md)、[field](field-review-priority-v1.md)、[confidence](review-confidence-v1.md)；`test_batches.test_preview_replays_exact_plan_and_per_person_rounding`、`test_confidence_acceptance.test_strict_threshold_low_first_ties_and_uncovered_count` | ReviewBatch冻结plan/seed/分母、quota及三类冲突优先已存在；confidence仅可信producer revision-specific assertion。真实producer/校准质量NOT_RUN，多风险合并策略DEFERRED | 已有受限功能；不再把基础抽审列为未开发 |
| 2. 驳回→返工→新revision→再审核，旧修订不变 | [sampling rework](review-sampling-v1.md)；[test_batches.py](../../label_studio/review_batches/tests/test_batches.py) 的 `test_rejected_rework_new_revision_and_old_evidence_unchanged`；[test_rework.py](../../label_studio/review_batches/tests/test_rework.py) | MyWork推导返工，原active assignment合法持有人经Editor/submit生成新revision；旧batch不扩容，无独立永久返工任务账本。approved+concern不自动返工 | 基础闭环已实现；concern处置本轮PROPOSED |
| 3. 单revision单次审批、自审拒绝、移除成员立即拒绝 | [submissions.py](../../label_studio/tasks/submissions.py)、[second-review implementation](second-review-v1-implementation.md)；`test_batches.test_self_review_and_query_injection_denied`、`test_revoke_reviewer_clears_collection_detail_queue_and_stale_decision`；[second_review_authority](../../label_studio/review_batches/tests/test_second_review_authority.py) B01–B10 | 初审pending+OneToOne；二审独立opinion不重审，subject含cancelled终态。新处置操作/完整cohort隔离尚未实现，不能用旧测试替代 | 初审/二审规则已实现；新规则NOT_RUN |
| 4. skipped/unchecked不成为Approved，交付不绕批准策略 | [test_second_review_integration.py](../../label_studio/review_batches/tests/test_second_review_integration.py) F01/F02/F03_F04/F06；[approved.py](../../label_studio/data_export/approved.py) | approved+concern保留旧release资格，rejected+confirmed仍不合格；supervisory_only不是发布门禁。#79/#80真实PG/Python3.11已有独立测试入口，部署/真实交付NOT_RUN | 已有Fork效力明确；如要二审阻断发布须新政策设计 |
| 5. 多用户、两语、重试/并发、审核结果下载负例 | 现有 review_batches tests，`test_second_review_concurrency` D04–D07、authority/budgets/ledger；[second-review CI](second-review-ci-integration.md)、[focus](review-handoff-focus.md)；[approved export acceptance](issue79-local-acceptance.md) | exact-main既有合成多用户/双语/重试并发和下载授权证据已留档；“审核结果下载”如果指审计包，不能由已批准数据下载代替。见下一节 | 部分语义需确认；不把checkbox整体标完成 |

注：表中的简写 `test_revoke_reviewer_clears_collection_detail_queue_and_stale_decision`
属于 `test_batches.py`；其余方法名按对应文件查找。不能把源码测试入口当成独立执行回执。

## 3. 其余目标与当前边界

| 范围 | 当前状态 | 下一步 / 不得声称的能力 |
| --- | --- | --- |
| 版本、seed、selected/unchecked/skipped、批次/风险/队列 | IMPLEMENTED：ReviewBatch与现有三类风险/可信confidence | 多风险组合、任意规则编辑不是V1 |
| 初审/返工/immutable修订与只读差异 | IMPLEMENTED：原工作流与rework比较 | 原有ReviewDecision不是DB冻结的永久审计账本；不能扩大为Host永久审计 |
| 分类/NER/字段审核→仲裁 | IMPLEMENTED：原pending来源+冻结cohort交接，PR133修复焦点时序 | 已批准/驳回concern不具fresh交接资格，不直接开放按钮 |
| 静态分类独立二审 | IMPLEMENTED：固定supervisory_only、无初审/发布效力 | concern后续处置、改派/重开、NER/字段/bbox、多级复核分开设计 |
| concern后续记录 | [本轮提案](second-review-concern-followup-v1-design.md)，56项未来验收NOT_RUN | 单条独立Manager处置＋可选合法新revision关联；不做新的任务/审批系统 |
| 二审/处置阻断发布 | DEFERRED | 必须覆盖legacy release、#79/#80、历史产物及Host完整政策，不靠UI提示实现 |
| 真实provider、模型质量、用户/Host/Core集成 | NOT_RUN | 不以syntheticfixture、开关或merged CI代替业务验收；本轮不调用真实模型/平台 |
| 部署/启用、Issue关闭 | NOT_RUN / 未授权 | 保持Fork-only、#81/#84 OPEN，Docker/#76不动 |
| #81 bbox等更广模态与综合质量闭环 | #81仍OPEN，超出本轮 | 不能因分类/NER/字段子集交付而宣布#81完成 |

## 4. “审核结果下载”必须先明确两种产品语义

1. **已批准标注数据下载**：复用legacy release和#79/#80；已有批准来源/文件/权限负例。
   后续新处置必须补回归，证明不给这些出口增加权利，原pending/rejected仍不能发布。
2. **审核审计结果包**：抽样原因、初审意见、二审意见、仲裁/处置及来源链的审计下载。
   当前没有专用此类download endpoint。本轮不新增出口；若#84最终验收确实要求，
   先定义schema、exact冻结时点、reason/个人身份脱敏、当前权限和历史缺源语义，
   再复用已有export/snapshot框架评估，不另建通用导出服务。

没有用户确认前，不把第一种当成第二种已经通过，也不擅自将第二种判定为必须开发。
这项语义确认不阻塞本轮文档完成，但阻止将#84整体验收/关闭写作已完成。

## 5. 本轮交付与后续顺序

1. 本轮：收口矩阵＋concern单次处置设计＋56项验收要求，只有文档和文档静态校验。
2. 用户确认效力/范围并授权开发后：在本独立worktree实现；本地负例、PG/SQLite、双语原生浏览器；
   独立reviewer须按用户明确要求安排。本轮未开展独立审查。
3. 另授权push/PR后完成远端完整矩阵及最终SHA；另授权exact-head merge后核验实际merge/main CI。
4. 审计下载语义、二审门禁或仲裁关联，以及真实Host/部署/业务验收单独推进。

旧 `second-review-v1-design.md` / `ci-governance-v1-design.md` 等“待确认/NOT_RUN”标题是
当时冻结设计快照；当前实现状态以 implementation/CI integration文档和sealed运行回执为准。
只添加这个当前状态索引，保留所有历史证据和原始声明，不回填成伪造历史通过。
