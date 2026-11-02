# #84 二次复核 V1 设计（待确认）

2026-11-02；基线 Fork main `15ca19c5abf48505042ee6b54bbb7194c5bf8d1e`（#130实际merge）。
**本轮只有设计/验收要求，没有实现、迁移、启用或功能验收。**
配套：[验收矩阵](second-review-v1-acceptance.md)；
[CI治理](ci-governance-v1-design.md)应先于下一轮功能开发交付。

## 1. 效力提案与范围

**拟议选择，须随本设计由用户确认：V1固定为 `supervisory_only` 监督复核，
不是第二次审批，也不是发布前置条件。** 本轮设计授权不等于用户已选择该效力或授权实现。
不提供客户端effect字段、任意规则编辑器或新的项目业务策略引擎。

| 结果 | V1效力 | 原初审/发布 |
| --- | --- | --- |
| `confirmed`（复核无异议） | 独立人员确认所见exact初审来源 | 不增加approved、不构成双人批准、不授予发布权 |
| `concern`（复核有异议） | 原因必填，追加不可变监督意见 | 不撤销approved/rejected、不自动返工、不自动拦发布 |
| pending / cancelled / unavailable | 未完成、取消、来源不可验证分别显示 | 均非confirmed，也不改变发布资格 |

**初审approved且二审concern，仍可能满足旧release/#79条件。**
初审rejected且二审confirmed，仍不能因此发布。UI并列显示“初审结论”和“监督复核意见”，
明确提示“本轮复核不控制发布”，不能显示“双审通过/允许交付”。
要求二审阻断发布时，须另设计legacy release、#79/#80、历史产物与Host的完整政策，
不能将本V1宣称为该门禁的局部实现。

首版只覆盖已有ReviewBatch中**selected、已初审approved/rejected的静态inline Text +
静态Choices** exact Submission。复用confidence V1的严格静态分类子集识别，
普通batch不依赖confidence；confidence来源仍须原confidence flag/mode/instance/read scope，
分类冲突来源仍须原策略flag/mode/read scope，但不要求新的producer grant、fresh profile或评分；
已冻结profile/proof按原历史读取规则验证，retirement不得被误写成新来源。无scorer或模型调用。
允许普通、分类冲突优先、revision-confidence批次；NER/字段批次拒绝。
不支持URL/动态Choices、HTML/CSS装饰、媒体、NER/bbox/字段、多级复核、自动抽样二审、
批量派发、改派、申诉、自动返工或自动仲裁交接。

## 2. 架构约束与复用

- `tasks.models.ReviewDecision.submission` 为OneToOne；`tasks.submissions.review_submission`
  只审pending。保持唯一约束和旧命令，不重审旧revision，也不覆盖旧决议。
- `review_batches.service.candidate_rows` 只选latest pending。旧ReviewBatch table、plan、
  hash、分母和排序不变，二审不能改变普通候选接口含义。
- `Submission.is_releasable` 只看approved；`data_export.approved.source_record` 校验初审。
  当前没有二审发布门禁。
- 旧初审reviewer/submitter是可空本地FK，无历史principal/provider。新记录不得合成历史principal；
  冻结已验证actor ID，缺失则不能新派发。
- 复用可信principal、operation provider、Project-first fence、bounded snapshot/metadata loaders、
  分类parser/answers校验、数据库冻结保护、ReviewerWorkspace只读来源展示、typed DTO、分页、
  双语及原键UNKNOWN恢复。使用既有Django/DRF/数据库/hash和前端依赖，不引入审批平台、
  商业Enterprise组件、调度服务或第二套IAM。

## 3. 最小载体、唯一性、状态

在现有`review_batches` app内拟增加两个typed载体；当前不存在这些实现：

| 载体 | 冻结内容/有限状态 |
| --- | --- |
| `SecondReviewTask` | UUID、project/org/instance、batch ID/hash/所选候选完整proof、九pins、初审proof/hash、rule/effect、派发principal/source/actor、受派actor、key/body hash/receipt；pending→completed或pending→cancelled，CAS version1→2 |
| `SecondReviewDecision` | task OneToOne、confirmed/concern、reason、source/first-review hash、二审principal/source/actor、server time、key/body hash/receipt；只追加一次 |

Task另有一次性cancellation envelope：取消principal/source/actor、server time、reason、key、
body hash、receipt/hash；不覆盖原派发receipt。pending→cancelled时原子填齐后冻结，
completed与cancellation互斥；数据库约束/triggers拒绝只翻status/version却无一致回执，
也拒绝有decision时取消或取消后插decision。创建/取消receipt各自可恢复。

instance为operator-owned稳定UUID；缺失不可启用，不借用Host、producer或测试namespace。
独立clone/reset的身份规划另行处理。保持既有project cascade，不声称永久审计库。
便捷FK仅允许SET_NULL，冻结标量ID不重绑。

唯一subject是`(project, instance, submission_id, revision)`，**跨batch、key、初审hash、
受派人均只允许一个V1 task，包含cancelled/completed历史**。
校验初审hash，但不以改变hash获取第二名额。终态不重开、不换人、不用新key替代concern；
pending取消后V1也不再派，派错人后的治理另设计。这避免反复选人获取有利结果。
创建/决议/取消分别使用project内命令key namespace；同key/body/owner恢复原回执，
跨actor/principal/source或改body冲突。恢复不改变终态。

PG/SQLite新triggers保护PK、scope、proof/receipt/hash/identity/time/key，包括bulk/native UPDATE。
只允许定义的单向状态/CAS变化及FK清理；decision插入和completed同事务；
cancel只允许pending且无decision。其他DB拒绝启用。
PG的proof/receipt/cancellation比较使用jsonb文本/严格结构表示，禁止将1→1.0视为未改变；
复用confidence冻结trigger经验。跨表状态由事务命令及当前读取validator共同校验：
completed必须有匹配唯一decision、cancelled必须有一致取消envelope且无decision。
SQLite不能宣称具备PG deferred constraint；若原生SQL留下pending+decision等半状态，
读写validator必须报corruption，不展示为可继续任务；API事务异常则完整回滚。

## 4. 冻结来源和历史语义

fresh dispatch要求原batch selected、该assignment最新revision、有一致approved/rejected初审。
unselected/unchecked/pending/superseded/无初审/缺源/前后不一致拒绝。禁止只按task ID关联。
proof完整绑定：

1. instance/org/project、batch ID、完整plan hash、schema/algorithm/priority版本、完整包络及所选candidate归属；
2. 九pins：`submission_id, assignment_id, task_id, revision, assignee_id, submitter_id,
   result_hash, task_data_hash, label_config_sha256`；
3. 初审ID/submission ID、原reviewer actor ID、decision、**完整未截断reason**、UTC created_at、
   `first-review-proof/v1`及其hash；历史principal/source明确unavailable，不伪造；
4. `second-review-source/v1`、`static-text-choices/v1`、`independent-supervision/v1`、
   `supervisory_only`，以及整个typed source hash。

重验目标snapshot/hash、冻结task data/XML、静态classification answers；原策略当前读取授权不可验证则unavailable，
不降级、不替换batch。冲突来源沿用whole-cohort自审排除；不展示其他人的答案、raw confidence或producer proof。

增加窄的targeted-origin loader：只解码完整有界plan元数据，检查完整包络/hash、目标selected
归属、原策略版本及frozen cohort参与者排除；组合原策略当前权限/flag/mode/instance gate，
然后只加载目标snapshot及其初审。禁止调用会hydrate整batch候选的read_sources/checked_sources。
现有helper若不能分离gate，新增内部窄policy接口，旧调用合同不变，不能绕开源策略授权。
source_health精确定义为`target_source_verified`；原metric/cohort答案健康为
`metric_integrity=not_checked`，不重新计算或宣称全cohort当前已验证，也不返回旧分数作当前质量结论。

派发前或冻结后，必要reviewer/submitter/assignee FK为空都使source/decision unavailable，
仍保留元数据和当前授权下的原键恢复；冻结actor ID仅用于历史关联和自审排除，不能代替缺失源。
这避免仅靠当前NULL猜测是合法删除还是改写；非空ID改写/重绑是corruption。
初审人后来失去Reviewer角色但历史FK仍在，不抹去合法初审；二审人当前角色/成员失效拒绝操作。
原assignment/task/submission/初审行或batch消失：只保留元数据历史，source/decision unavailable。
这里的unavailable仅指源读取/新增decision动作；已存二审意见及命令回执仍以not_checked元数据历史呈现，
不删除或改写原二审意见。
assignment改派不靠冻结ID“修复”成可写；受派FK清空不能按复用数字ID恢复权限。

派发**之后**出现新revision，原pending二审可继续检查旧exact revision并标明“历史修订”，
意见不继承给新revision；派发前已有新revision则旧源不得fresh dispatch。
来源graph/hash和初审proof仍须一致，不用latest快照替换。原键回执只说明原命令结果，
不能证明当前来源健康或当前权限。

## 5. 权限与拟议API

默认`ANNOTATION_SECOND_REVIEW_ENABLED=false`，另有operator-owned org:project `fork_review` mode map。
missing/unknown/core_only_internal均拒绝新API；不改变Core-only普通内部Submission读取。
显式operation `ORGANIZE_SECOND_REVIEW` / `SECOND_REVIEW`；Legacy provider不因flag自动授予。
READ_WORK、REVIEW、producer、ORGANIZE_REVIEW、仲裁和release权限不能替代它们。

| 行为 | 当前权威 |
| --- | --- |
| context/受派人候选/派发/原派发键恢复 | Manager + READ_WORK + ORGANIZE_SECOND_REVIEW |
| 取消/原取消键恢复 | Manager +既有SECURITY_CANCEL fence及当前scope，允许工作operation撤销后安全终止；不要求READ_WORK/组织operation，不返回答案 |
| list/detail元数据 | 当前Manager或冻结受派人的当前Reviewer，均需READ_WORK |
| source/初审意见详情 | 上述当前scope + 完整来源可见性/自审排除和byte/hash校验 |
| 决议/原决议键恢复 | 受派Reviewer + READ_WORK + SECOND_REVIEW，排除原assignee/submitter/初审人/原冲突cohort本人 |

Manager可组织但不能代二审；Annotator、其他Reviewer、org admin或unscoped superuser不获得新权限。
无独立人选时明确不可派，不自动候补。SQL/scoped query先过滤，再计数/分页/解码；
object/list/capability/history统一。取消只允许读取有界任务回执元数据，仍需feature/mode/org/project/account有效；
Core-only/disabled不能借security cancellation读取旧内容。

以下全属拟议路由，当前无endpoint：

| Method / project路由 | 严格输入 |
| --- | --- |
| GET `review-batches/{B}/sources/{S}/second-review-context/` | 返回eligibility、source hash、人选分页入口和已有task元数据，不内联完整roster |
| GET `review-batches/{B}/sources/{S}/second-review-assignees/` | Manager组织权威；默认50/上限100、scope签名游标，仅id/display_name标量 |
| POST `second-reviews/` | batch_id、submission_id、expected_plan_hash、expected_source_hash、assignee_id、expected_viewer_hash；UUID key |
| GET `second-reviews/` / `{T}/` | 签名scope游标；default50/max100，metadata-only |
| GET `second-reviews/{T}/source/` | 单一原snapshot+初审、source_health、历史修订标记 |
| POST `second-reviews/{T}/decision/` | expected_version、expected_source_hash、outcome、reason、expected_viewer_hash；UUID key |
| POST `second-reviews/{T}/cancel/` | expected_version、reason、expected_viewer_hash；UUID key |

不接收actor/reviewer/status/approved/result/effect/首审字段/Host IDs/source proof。
拒绝未知key、重复JSON key、bool/float/string冒充ID/version、非法UUID。
confirmed reason可空；concern/cancel trim后必填，≤2000字符且UTF-8≤8KiB。
实际body≤16KiB，parser前累积检查，含chunked/短读；响应private/no-store。
无新增文件、媒体、annotations、export、download或bulk权限。
assignee列表先在服务端过滤当前Reviewer/成员、独立身份及原cohort，再计数/分页。
游标绑定project/source hash/当前viewer/排序；POST assignee单ID另查当前资格，列表不是授权凭据。
只投影必要name标量（单人显示字段UTF-8≤1KiB），不加载User的JSON/私有profile/完整组织数据；
context实际响应≤128KiB，候选页≤256KiB；超限明确失败，不无限展开roster。

## 6. 并发、预算和恢复

统一锁序：Project → OrganizationMember/User/ProjectMember（既有顺序）→ Task →
Assignment → Submission → ReviewDecision → ReviewBatch → SecondReviewTask → SecondReviewDecision。
相关user IDs排序，保持事务直到render及最终authority/source检查结束。
review/submit/成员撤销按Project fence排序；外部operation配置末尾重查，不假称数据库锁住配置。
submit先提交则旧派发preview拒绝；派发先提交则保留历史复核。
CAS+唯一约束使两个decision或decision/cancel仅一个终态；失败无部分写。

有效source请求/新写：初次targeted-origin完整有界验证 → render →
第二次实际有界source/初审/batch验证及表示等值比较 → 当前身份/权限复验。
无metric/scorer。metadata history标`source_integrity=not_checked`。

| 资源 | Stored SQL bytes | Canonical UTF-8 |
| --- | ---: | ---: |
| 单snapshot | 2MiB | 256KiB；静态文本字段另限64KiB |
| 每遍全部metadata（batch plan+task proof/receipt+初审reason/decision） | 32MiB | 8MiB；原策略更小限额仍适用 |
| 单task proof/receipt两列累计 | 64KiB | 16KiB |
| 初审reason / 新reason | 32KiB / 8KiB | 8KiB / 8KiB |
| 单页metadata≤100行 | 32MiB | 4MiB |
| 实际响应含wrapper | — | 4MiB |

双遍snapshot≤4MiB SQL/512KiB canonical，metadata≤64MiB/16MiB。
页面不含100份答案，超限拒绝、不截断reason/proof。
SQL preflight及**实际hydration SELECT内row/union CASE gate**共同阻止并发增长后JSON先到decoder；
TextField reason也需SELECT内gate。累计所有列和helper物理重读，复用现有bounded/window loader。
继承的完整Project授权配置加载不计入新源预算，不声称已完成全局projection硬化。

原键恢复在当前owner/scope/相应operation下先验证有界不可变receipt，零live snapshot/初审reason/
batch读取，零source重算；只要求新二审feature/mode和当前命令权威，不依赖来源策略仍开启或live
profile/source可用，回执标not_checked。salt-only viewer变化保留同owner UNKNOWN intent，
真正principal/source/actor/project/org变化清空。transport失败保留原body/key及UNKNOWN，
刷新不能重发POST或换新key。明确成功和随后progress读取失败分别显示。

## 7. 页面、仲裁和发布边界

原batch已初审selected行提供“安排监督复核”，Manager先读context再明确选人。
Reviewer只进入自己受派队列，复用只读来源/初审展示，保留batch/filter/focus返回路径；
二审按钮/transport与旧approve/reject分开。严格DTO在缓存前验证，late read/ACK绑定
project/task/owner/viewer/source/version；换域/撤权清除旧答案和动作。
两语明确呈现历史修订、来源不可用、原初审与独立意见及“复核不控制发布”。

| 路径 | supervisory_only的边界 |
| --- | --- |
| legacy `/submissions/{id}/release/` | 原授权+approved规则；无二审门禁，不宣称修复其另行治理的历史完整性范围 |
| #79 candidates/preview/freeze/worker READY/content/manifest/download/retry | 原初审证据及既有实时权威；不消费confirmed/concern |
| #80 capture/worker/manifest/assets/download/diff及已有READY产物 | 原来源证据合同；不回写旧Manifest，不伪称旧schema已覆盖二审 |
| 普通Submission/annotations/export/media/files/bulk | 无新增访问/发布权，unchecked/skipped/unselected不变approved |
| Host/Core-only | 不合成业务二审批准，不改Host/Core/DatasetVersion/Gold/交付政策 |

未来blocking方案必须覆盖联合批准条件、每条发布/下载、运行中worker竞争、
历史产物访问/迁移和版本；已交付字节不可追缴。本V1不是该方案。
现有review→arbitration handoff要求selected frozen conflict+latest pending，
已初审来源不满足；V1不放宽它。打开既有仲裁工作区不构成受控交接或闭环验收。
concern只记录监督意见，不自动返工或新增仲裁来源类型。

## 8. 未来接点、验收和保留边界

拟改`review_batches/models.py`、新增additive migration/typed second-review service/API、
`access_control/project_operations.py`、project URL/capability、DataManager现有流程、
typed DTO/双语catalog及合成seed/tests。不改旧迁移、source/decision/plan hash、scorer、
release/#79/#80 schema或Host。先预算/权限负例，再独立PG并发、SQLite和两语浏览器。
只用新原生隔离合成资源，不使用Docker/#76、真实producer或其他worktree保留数据。

功能/数据库/浏览器/远端CI均NOT_RUN；设计review不将其标SUCCESS。
保留运行态/全库一致性UNKNOWN及既有CHANGED/数据库替换/中断比对记录。
Fork-only；#81/#84 OPEN；本轮不开发、推送、PR、合并、部署或启用环境。
