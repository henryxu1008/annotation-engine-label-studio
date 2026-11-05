# #84 受限 V1：当前收口索引

2026-11-04。基线 Fork `henryxu1008/annotation-engine-label-studio` 的实际 main
`d814ab63f205fb2ef4ecff007525c836e928bb82`，tree
`c4af2313a80360487522afa6b634c1bf692c4f07`。

本轮只编写设计和验收要求；没有开发产品代码、迁移、运行测试/服务、实际启用、推送、PR、合并或部署。
范围固定为静态 inline 单 Text + static Choices、`supervisory_only`、固定脱敏。
#81/#84 保持 OPEN。主仓、原有 worktree、Docker/#76 和历史证据均不在操作范围内。

配套：[受控启用验收设计](issue84-v1-closeout-activation-design.md)、
[72 项未来验收矩阵](issue84-v1-closeout-activation-acceptance.md)、
[机器可读矩阵](issue84-v1-closeout-activation-matrix.json)。

## 1. 当前事实与历史快照

`issue84-closeout-matrix-20261103.md` 及各阶段设计的 PROPOSED/NOT_RUN 是当时记录，保持原样。
本索引补充后续交付事实，不把旧文档改写成当时已执行。

| 阶段 | 已核验事实 | 能说明的范围 |
|---|---|---|
| 二审 concern 跟进 | PR #134，source `f0d7930e2c3ec85457614e14bff363e3f6479bfe`，实际 merge `78b6c9a0cfd0f85a69300d31276aa0d1daeed480` | 一次独立 Manager 处置，`no_change` 或合法 `revision_linked`；无新的批准或发布效力 |
| 审核审计下载 | PR #135，source `0c770207246962382fbfd2569debeee69ca7d050`，实际 merge 为本基线 | 单 batch 的有界完整关系图，在 T 冻结可观察/可证明事实并固定脱敏 |
| 实际 main CI | 上述 merge 的 19 个 push workflows、38 个实际 checkout-bound jobs 成功；UI 12 个 evidence records /26 次 browser invocations，独立回放成功 | 历史合成 CI 证据，不是当前保留环境、真实队列、producer 质量或业务启用的证明 |
| 本轮设计 | 独立 worktree 中的当前文档、静态检查与独立设计审查 | 只形成验收规范；矩阵执行栏全部 NOT_RUN |

PR #135 的两个历史 P2 已在 source 和实际 main 上补回归：
错误 artifact UUID/末端拒权不隔离健康包的 12 条边界回归；blocked retry 的准入预算 11 条回归。
不能把先前的失败/取消历史抹成第一次通过。

已封存证据根（读取，不修改）：

- `C:/Users/Henry/.codex/reports/ls84-review-audit-download-quota-merge-20261104/`
  的 `actual-merge.json`、`main-final.json`、`SUMMARY.json`、`final-evidence-guard.json`。
- 相应 source 的本地、PR warm/cold/真实部分重跑证据由上述报告追溯到 sealed sibling reports。
- 当前 source/app/fixture 开关默认值不证明任何保留实例已经启用。该实例状态本轮 UNKNOWN。

## 2. 原 #84 五项验收的受限映射

| 原验收方向 | 已实现与已有证据 | 受限解释 / 本轮未来验收 |
|---|---|---|
| 比例、人员、任务包、冲突规则稳定重放 | [抽审/返工契约](review-sampling-v1.md)、[分类优先](review-priority-v1.md)、[低置信度](review-confidence-v1.md)；`review_batches/service.py` 的 package union、rate/per_submitter/seed 与 frozen plan | 任务包不是新缺口。冻结分母和单一策略保留；本设计只验静态分类。可信 producer 主张可重放不证明校准质量；E01–E06 |
| 驳回→返工→新 revision→再审核 | 原 My Work/Editor/assignment 与只读 revision comparison；旧拒绝/批准证据不覆盖 | 不新增永久返工任务账本，不复活取消/改派的原 assignment；F01–F02 |
| 单 revision 单次审批、自审/撤权隔离 | 原 ReviewDecision OneToOne、独立二审与独立 concern handler 排除集 | 二审只是一次监督意见；Manager 组织权不替代 Reviewer；D01–D06/F03–F04 |
| 未审/跳过不被批准、发布不旁路 | 原 Fork release/#79/#80 仍使用自己的批准策略；supervisory confirmed/concern 和跟进不改变资格 | 不增加二审发布门禁；F03–F05/G06/K03 |
| 多用户、两语、重试并发、审核结果下载负例 | 现有 source/main CI 与审计下载 80 项历史分阶段矩阵 | 新审计 browser fixture 的队列是 synthetic；本设计 G01–G06 明确要求将来的独立真实 RQ 传输证据，不能复用旧 PASS |

本索引不勾选/关闭 GitHub Issue。这里只定义受限 Fork V1 的证据边界，不宣布原 #84 所有模态、
真实用户、Host/Core 与部署目标已经完成。

## 3. 剩余项的分类

| 类别 | 当前状态 | 处置边界 |
|---|---|---|
| 本轮设计/矩阵/独立 reviewer | 本轮授权 | 仅文档与静态审查；执行状态不回填 PASS |
| 隔离合成环境的真实队列与完整流程 | NOT_RUN；环境、启动配置桥接与验收支撑尚未获执行/实现授权 | 必须先通过资源所有权和权限前置；不能调用现有宿主环境或 seeder 的 mock 路径冒充真实队列 |
| 非测试新 operation provider | 未提供非测试实现/交付证明 | 库内唯一非测试 Legacy provider 不授予这些新 operation；真实启用前须由权威所有者提供并独立审查，不能把测试 provider 投入业务 |
| 自定义 Host identity 完整审计浏览 | 当前不支持完整集合接口 | 即使有 `resolve_background`，history collection 仍只接受 exact LocalIdentityProvider；真实 Host 联验/适配另行设计 |
| 真实 confidence producer /质量校准 | 当前可用性 UNKNOWN；模型/质量验收 NOT_RUN | 可先验合成的 revision assertion 协议；真实低置信度业务可用必须单列 producer/版本/校准证据 |
| 全局原子停用、撤回已下载字节 | 不是现行能力 | 采用外部受控进程屏障、逐组件配置收敛与在途流排空的设计；不新增 kill-switch |
| 永久审计、保留期限、物理擦除 | DEFERRED，Host/运维政策未在本轮定义 | 初审不是永久冻结账本；T 前缺失不能重建；私有孤儿文件不等于已物理删除 |
| NER/字段/bbox 扩展、多风险组合、改派/重开、多级复核、concern→仲裁、二审阻断发布 | DEFERRED | 继续保留各已验收契约；单独授权设计，不作为本轮暗含开发范围 |
| 实际启用/部署/Host 业务验收/Issue 关闭 | NOT_RUN，未授权 | 必须与设计和隔离合成验收分别确认；#81/#84 OPEN |

## 4. 本轮可交付结论

“设计审查通过”表示范围、来源、依赖、权限、环境准入、验收断言和停用/回退语义可评审，
不表示运行准备齐全。依赖缺失允许设计完成，但实际执行/启用必须停在相应准入关口。
矩阵69项E、L03/L04两项P和L06一项O分别汇总；E可使用已批准synthetic provider证明协议，
真实启用仍需选定组件的非测试operation provider。真实producer缺失仅阻断真实低置信度业务，
不自动否定独立二审/audit组件的隔离验收或其另获授权的准备；局部准备不称完整业务V1就绪。
源码/数据永久账本等架构边界由 Host/相应领域负责，不在 Fork 内重复建立平台调度、IAM 或通用审计服务。
