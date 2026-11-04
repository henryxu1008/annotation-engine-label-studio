# 审核审计下载 V1：当前源码、版本与复用映射

2026-11-04；只读基线 `78b6c9a0cfd0f85a69300d31276aa0d1daeed480` / tree
`b179cfeca633ed2a39f21564848f92eadbf406bd`。[设计](review-audit-download-v1-design.md) 和
[80项矩阵](review-audit-download-v1-acceptance.md) 已获批准并进入本地实施；下表的基线映射仍区分历史功能证据与新audit验收。

## 1. 现状更新，不回写历史

#84 body 的五个未勾项是原验收清单，不是最新缺失列表。旧
[收口矩阵](issue84-closeout-matrix-20261103.md) 基线0143中 concern 仍为提案；PR134已将其合并到本基线。
比例/人员/任务包、三类冲突优先、静态分类confidence、返工修订、独立二审、concern跟进已有受限实现。
本阶段新增“完整审核审计包”，其独立本地验收不能用已批准数据下载的历史通过证明代替。

PR134实际main证据位于 `C:/Users/Henry/.codex/reports/ls84-concern-followup-merge-20261103/`：
19 push workflows/37jobs、25非UIcheckout、25browser调用、二审267.502s/300s均成功；
这是已合并功能的历史基线，不是本审计设计80项验收。#81/#84仍OPEN，部署/Host业务验收NOT_RUN。

## 2. 领域契约与证据等级

| 已有来源 | 可以复用/验证 | 不得借此声称 |
| --- | --- | --- |
| [ReviewBatch模型](../../label_studio/review_batches/models.py)、[service](../../label_studio/review_batches/service.py) | frozen candidate universe、selection、seed、身份pin/plan hash；有界查询和Project fence | plan不是完整历史意见副本；没有源snapshot/type时不能从当前Project猜 |
| [Submission/ReviewDecision](../../label_studio/tasks/models.py)、[审核命令](../../label_studio/tasks/submissions.py) | 精确修订/原snapshot hash、OneToOne当前初审proof；标准命令一次审批/自审拒绝 | ReviewDecision非DB永久冻结；没有batch执行FK，关系只能subject_observed |
| [二审origin](../../label_studio/review_batches/second_review_origin.py)、[二审service](../../label_studio/review_batches/second_review_service.py) | targeted bounded plan/source、9pin、独立opinion/cancel、原receipt-only恢复和锁顺序 | 不重审/不批准/不阻断发布；SET_NULL不能按数字ID复活旧源 |
| [followup origin](../../label_studio/review_batches/followup_origin.py)、[followup service](../../label_studio/review_batches/followup_service.py) | frozen concern/source/target/receipt、原owner/body恢复、typed预算/渲染验证 | no_change/revision_linked不是resolved/approved；无自动任务或仲裁关联 |
| [分类handoff](../../label_studio/review_batches/handoff.py)、[分类仲裁](../../label_studio/classification_disagreements/arbitration.py) | 本batch直接交接的完整typed receipt/cohort/hash/权威记录 | 不扩到其他batch全部历史；不把权威仲裁改成初审或publish权限 |
| [confidence契约](../../label_studio/review_batches/confidence_contract.py) | 固定profile/input/producer/observation版本与原9pin | 可信producer断言不是模型质量/概率/校准或新scoring |
| [SQL snapshot loader](../../label_studio/classification_disagreements/snapshot_loading.py)、[共享metadata](../../label_studio/review_batches/handoff_metadata.py) | SQL stored-byte preflight、CASE/wrapper/union gates及类型化读取 | 不是全Project授权上下文的全仓有界证明 |
| [Local identity](../../label_studio/access_control/identity.py)、[User模型](../../label_studio/users/models.py) | trusted server actor、当前active User及既有session security boundary | principal=source+数字ID，session version不是不可复用账户generation；旧source owner hash不证明历史actor incarnation |

V1 adapter版本白名单（原字符串保持；未知版本fail-closed）：

| 来源 | 原版本/规则 |
| --- | --- |
| ordinary | service.ALGORITHM `sha256-ranked-v1`；原plan schema按其现有strict validator |
| classification priority | `review-classification-priority/v1`，`classification-conflict-ranked/v1`，metric `classification-exact-sets/v1` |
| confidence | `review-confidence-priority/v1`，`revision-confidence-ranked/v1`，`review-confidence-input/v1`，`review-confidence-observation/v1`，`review-confidence-observation-receipt/v1`，`review-confidence-profile/v1`，`static-text-choices-submission/v1`，`millionths-int/v1`，`revision-confidence-lower-uncertain/v1` |
| second review | `second-review-source/v1`，`independent-supervision/v1`，`static-text-choices/v1`，effect `supervisory_only` |
| concern follow-up | `second-review-concern-followup/v1`，`independent-disposition/v1`，`second-review-followup-receipt/v1` |
| classification handoff / arbitration | `review-arbitration-handoffs/v1`；`classification-arbitrations/v1` / `adopt-exact-submission/v1` |

每个adapter必须继续验证其实际row/receipt中的完整版本和hash，而不只是比较表面schema。
初审没有版本化永久receipt时标observed_at_capture，不能创造一个“原初审receipt版本”。
公开version仅为固定协议白名单；自由模型/producer/profile/policy标签、原schema外字段保留私有或拒绝。

## 3. #79/#80机械复用与禁止的继承

| 来源 | 复用方式（未来） | 保留的边界/验收 |
| --- | --- | --- |
| [approved.py](../../label_studio/data_export/approved.py) `private_root`、queue健康、dispatch/attempt模式 | 共用私有root下audit-packages子树、既有RQ alias和身份后台hook，窄helper参数化 | 不复用其 `policy/manager/creator/freeze` 的fork_approved/批准源领域谓词，不关闭原provider要求 |
| [approved_models](../../label_studio/data_export/approved_models.py) | 参考token attempt/artifact/access状态形状，新audit域记录用自己FK | 不塞入ApprovedDatasetRelease表；原release/Item/Artifact/schema均不扩权 |
| [snapshots.py](../../label_studio/data_export/snapshots.py) `checked_file`、canonical、inventory概念 | 窄化机械FD/hash helper且保留旧默认错误码；使用相同descriptor | 可信OS root/祖先和不可变发布是部署前提；首字节后OS授权写入不在保证内，helper不冻结未来inode |
| [approved_api](../../label_studio/data_export/approved_api.py) `BoundedRangeReader/PrivateRangeResponse` | 仅复用认证后流式单Range/关闭FD机制 | API class不能继承：initial/scope/content_gate强绑批准policy与当前源 |
| [snapshot_api](../../label_studio/data_export/snapshot_api.py) | 借鉴manifest/文件schema检查和quarantine模式 | 不继承ReleaseSnapshotAPI；原gate检查live release图，audit READY需独立的冻结输入gate |
| [release manifest契约](release-manifest-v1.md)、锁定rfc8785/jsonschema | 公共envelope/JSON schema校验依赖直接复用 | 不手写JCS；旧snapshot exact Python hash不改，非有限/弱类型拒绝 |
| [下载helper](../../web/apps/labelstudio/src/utils/download.js)、[导出页面](../../web/apps/labelstudio/src/pages/DataManager/ApprovedDatasetExport.jsx)、[快照页面](../../web/apps/labelstudio/src/pages/DataManager/ReleaseSnapshots.jsx) | authenticated response/blob + generation校验后调用downloadFile | 不开放浏览器签名静态URL，不新增平行审核工作区 |
| [通用export](../../label_studio/data_export/models.py)、[generic API](../../label_studio/data_export/api.py) | 仅做兼容回归参照 | DataExport包含mutable数据/实名/Converter/resource语义，不能拿来直接生成audit包 |

复用优先是机械helper和上游已锁依赖，不是复制整个服务或放宽旧出口。若提取无法保持原契约，则先做兼容review，
不能用“复用”名义悄悄更改#79/#80授权、数据保留或Host含义。

## 4. 必须列入未来实现范围的兼容协调

[Project API](../../label_studio/projects/api.py) 的 `ProjectAPI.perform_destroy` / `ProjectTaskListAPI.delete`，
[Task API](../../label_studio/tasks/api.py) 的 TaskAPI.delete 及其公共批量/cascade helper，
**不能假定已具有Project-first fence**。未来新增审计capture必须审计并协调这些删除入口，在Collector/子行锁前锁定相关Project；
保持原授权和删除结果，覆盖多项目排序、guard rollback、Project hard-delete清audit行和既有release PROTECT。
当前本阶段不修改这些源码，只把必需的最小兼容补丁和真实PG负例作为实现准入项。

账户代际同样不能仅复用旧owner元组。未来audit域的AuditActorBinding只在已授权create写入，
generation不可复用、User消失原子tombstone、creator gate永久拒绝。其便利User FK必须避免Collector提前
SET_NULL产生binding→User倒序：采用DO_NOTHING+DB parent-delete hook，并审计User删除API/ORM/cascade
在Collector前协调Project→User→binding的路径；PG/SQLite directSQL仍有原子墓碑/不可重绑guard。
这是窄审计域和最小删除协调，不改全局Principal，不将旧审核记录伪装为有incarnation的永久账本。

audit-zip-format/v1冻结formatter compatibility digest、README字节和完整canonical/ZIP设置；
approved.runtime的release mapping/Python/SDK标识不是audit兼容证明。部署漂移只能blocked_runtime，
恢复原兼容环境后显式source-free retry；READY不重生。文件安全和runtime兼容的本地证据分别记录于80项验收记录。

初审缺永久账本、删除锁不统一、文件helper边界有限是当前源码约束；设计应诚实处理，不据此宣称现有公开API已被绕过。
数据库超级用户/离线维护可修改任意源的信任边界并不由下载包认证；此类维护不得与capture并发或需同等协调。
实现不能恢复没有留下类型/来源proof的旧数据，没有证明时拒绝新capture。

## 5. 本地实施与复用边界

本次新增data_export内独立audit模型/guards/adapter/API/formatter/worker与ReviewBatch详情中的页面入口。
复用既有RFC8785/jsonschema、Project/authority gate、ReviewBatch/冻结receipt、有界snapshot及Range响应；
共享private-file helper保持原snapshot错误契约，Budget元数据observer默认None以保持旧调用行为。
公开删除协调只覆盖既有Project/Task/User/Org roots；Assignment取消与Submission只读/审核路由没有变成硬删入口。
新增独立review-audit CI shard而不堆入原二审调用，原25项清单和required/并发/超时保留。
本地使用专用合成DB/测试资源，无真实model/Host/Docker调用，无push/PR/merge/deploy或Issue写入。

关键供用户确认的取舍：完整范围是单batch有界图；固定脱敏（不含原文、答案和自由reason）；
T前类型proof不成立则拒绝新capture；T后从冻结projection生成，Project删除结束访问；
审计Manager显式op可审计自己的操作，但原所有review/self-review权限不变；无发布效力。
creator账户删除则永久失效，同ID新账户不接管；其他source actor缺代际proof只给occurrence ref。
