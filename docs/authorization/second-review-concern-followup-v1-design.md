# #84 二审 concern 受控跟进 V1 设计提案

2026-11-03；基线 Fork main `0143fe04e4b70dec5f7c124f85a8fe7abb5001da`（PR #133 实际 merge）。
状态：**PROPOSED，待用户确认；只有文档，没有功能实现、迁移、启用或功能验收。**
配套：[56 项验收矩阵](second-review-concern-followup-v1-acceptance.md)、
[#84 收口矩阵](issue84-closeout-matrix-20261103.md)。历史设计和验收快照不改写。

## 1. 本轮建议确认的效力和最小范围

复用已经交付的 [supervisory_only 二审](second-review-v1-implementation.md)。
V1 增加一份**独立 Manager 对 concern 的不可变跟进处置记录**，支持说明无需改动，
或关联由现有合法工作流产生的一个新 Submission。记录处置人、原因和 exact 来源。
这不是第二次批准、质量问题已经解决的证明，也不是任务派发或发布门禁。

本提案刻意使用“跟进处置已记录 / Follow-up disposition recorded”，不使用
“异议已解决”“双审通过”“允许交付”。原 concern 永远保留。
没有处置记录只表示“未记录跟进处置”，不自动生成返工待办、锁定任务或撤回产物。

| disposition | 必填依据 | 效力 |
| --- | --- | --- |
| `no_change` | 独立处置人说明不改动的原因；关联修订必须为 null | 记录判断，原 concern、初审和发布资格均不变 |
| `revision_linked` | 必填原因，关联一个已存在、满足本契约的新修订 | 记录跟进关联；新修订仍按原审核规则处理，不能凭关联获得 Approved |

首版只覆盖普通、分类冲突优先、revision-confidence ReviewBatch 中，
静态 inline Text + 静态 Choices 的已完成二审 `concern`。
不支持 confirmed、pending/cancelled 二审、NER/字段/bbox、动态 Choices、URL 或媒体。
无需 confidence producer 新事件、fresh profile、评分或模型调用。

本期不增加待办负责人分派、抢占、草稿、改派、取消、重开、追加第二份处置或改写理由。
处置人就是服务端鉴权的记录提交者；记录前没有“已领取/负责人”状态。
需要跟踪执行中负责人或纠正已经记录的处置，须另设计追加事件及其并发/权限契约，
不能以覆盖此 V1 记录实现。独立 Manager 不足时保持未记录，不降级为当事人自处置。

## 2. 为什么不能直接复用仲裁按钮

已交付二审针对初审 approved/rejected 的 exact revision；现有 ReviewBatch → 仲裁
fresh context/write 要求 latest pending 来源。`tasks.submissions.review_submission`
也只允许 pending，ReviewDecision 对 Submission 是 OneToOne。
直接开放旧来源的仲裁按钮或把旧修订改回 pending，会改变已验收的初审、来源与发布契约。

V1 不从 concern 直接创建/链接仲裁任务，不构造单人“冲突 cohort”，不放宽原交接 guard。
后续若要关联仲裁，必须独立定义可验证 cohort、exact 输入集合、目标资格与处置效力。
现有合法分类/NER/字段仲裁交接继续复用原流程，不在本期改动。

返工也复用原流程：rejected → MyWork → 原合法标注员编辑并提交新 revision → 单独审核。
approved + concern 不会变成 rejected 或在 MyWork 中伪造返工任务；原有 active assignment
若允许继续编辑，可由其合法持有人通过现有 Editor/submit 命令产生新修订。
缺少权限、assignment 失效或 Host 未授权时，本扩展不能代替派发/编辑权限。

## 3. 复用点与拟议改动边界

使用现有 Django/DRF、数据库事务、canonical hash、typed DTO、ReviewBatch、
SecondReviewTask/Decision、可信 identity/operation provider 和现有前端依赖。
不引入任务平台、规则编辑器、商业 Enterprise 组件、第二套 IAM 或审计导出框架。

| 既有组件 | 复用方式 / 保持的契约 |
| --- | --- |
| `second_review_contract.py`、`second_review_api.py` | 严格 JSON、字节限制、owner/viewer、私有响应与最终权限复核；旧 wire/hash 不变 |
| `second_review_service.py` | bounded task/opinion metadata 校验及原键恢复模式；不更改二审 task 状态或 subject 唯一性 |
| `second_review_origin.py` | 既有 selected candidate、完整 plan/hash、九 pins、初审和静态分类历史验证；用 frozen 历史语义，不用 fresh dispatch 语义 |
| `second_review_bounded.Budget`、snapshot loader | hydrate 前 SQL 字节门禁、strict 类型和有界 targeted snapshot；跨调用共享预算 |
| `tasks.submissions`、`tasks.my_work`、已有 rework 只读比较 | 产生/审核新修订和展示返工链，保持原权限及不可变历史 |
| `project_operations.lock_project_operation` | Project-first fence、当前 membership/actor/operation、固定加锁次序 |
| ReviewerWorkspace / 二审工作区现有组件 | Manager 的来源、初审、二审意见只读展示和两语交互，不另造编辑器或审核工作台 |

拟在 `review_batches` app 增加一个 typed model、对应 PG/SQLite 冻结 guards、
窄的 followup contract/service/API/target adapter 和受 feature flag 控制的 Manager UI。
升级敏感入口在 [authorization-plan](authorization-plan.md) 仅增加索引。
本轮没有创建这些实现文件或迁移。

架构注意：当前 `second_review_origin.load()` 内部创建 Budget，而 `task()` 可接收 Budget。
未来组合原来源与新修订时，不能简单串联各自重新置零的 loader。
应给窄内部读取接口传入请求级 Budget，保留所有旧调用默认值、限制与 wire/hash，
用组合超限回归证明没有绕过总预算；不在此机会重写整个审核子系统。

## 4. 最小载体、状态与防改写

拟议 `SecondReviewConcernFollowup`，一个 concern 只能追加一条：

- UUID；project FK CASCADE、organization/instance；second-review task/opinion 的便捷 FK SET_NULL，
  冻结 UUID 标量；唯一 `(project, instance_id, opinion_id_frozen)`，删除 FK 不释放名额。
- `schema=second-review-concern-followup/v1`、`rule=independent-disposition/v1`、
  `effect=supervisory_only`、`version=1`。
- 原 batch ID/plan hash、原 task/source/first-review/opinion 身份和 hash、九 pins，
  原 submitter/assignee/初审人/二审人及 frozen cohort 排除集合；不合成历史 principal。
- 处置人的 authenticated principal ID、trusted source、local actor ID、server UTC time。
- disposition、完整 reason；关联新修订的九 pins、status_at_record、初审 proof/hash（若已有）
  或明确 null。两种 disposition 的 null/非 null 组合由 DB 和读写 validator 共同约束。
- project 内唯一 Idempotency-Key、完整原请求 hash、原 receipt 及 hash。
  receipt 不含答案；绑定 scope、actor、来源摘要、disposition、完整 reason 与目标摘要。

状态只从“没有记录，expected_version=0”到“有冻结记录，version=1”。
没有 mutable completed/resolved 字段，不维护第二个跟进任务状态机。
源二审 task 仍为 completed/version=2，二审意见仍为 concern。
DB guards 冻结主键、scope、来源、actor/time、正文、key/hash/receipt；保护 ORM/bulk/native SQL。
只允许便捷 FK 清理为 null；非空 ID 重绑、1→1.0 的表示变化等须拒绝/报损坏。
project cascade 沿用现状；不声称是 Host 永久审计账本。
仅支持 PostgreSQL/SQLite；其他数据库不启用。

## 5. 来源绑定、关联资格与缺源行为

fresh 处置必须验证：原二审 task completed/version=2；其唯一 opinion 为 concern；
原 dispatch/opinion receipt、typed source、初审全文 reason/time/actor 和所有 hash 一致。
完整 plan/hash、selected 归属、schema/algorithm/priority 版本和九 pins 均沿用二审来源契约。
旧来源可因合法新 revision 已非 latest；**不能再次要求原 revision latest**，
否则无法关联其后继。原批准/驳回及 frozen inputs 必须仍一致，不以当前最新快照替代旧来源。

九 pins：`submission_id, assignment_id, task_id, revision, assignee_id, submitter_id,
result_hash, task_data_hash, label_config_sha256`。来源摘要另绑定原 second-review task/opinion
UUID、source_hash、first_review_hash、原两份 receipt hash，以及 opinion outcome、完整 reason、
actor/principal/source、server time。它是 concern_hash，不复用裸 source_hash 充当二审意见证明。

`revision_linked` 的目标须全部满足：

1. 与原来源同 instance/org/project/task/assignment，revision 严格更大且不是原 Submission。
2. 原 submitter/assignee 未改派，当前 assignment active，目标由该合法持有人经原 submit 产生。
   同 task 的他人 assignment 或相同答案不能代替相同返工链。
3. fresh context 和提交时均为该 assignment latest；状态仅 pending/approved/rejected，
   不接受 superseded、缺失、伪造状态或被越权变更的目标。
4. 完整 snapshot/result_hash、task data、静态 Text/Choices 与输入 schema 校验；
   原/目标 task_data_hash 和 label_config_sha256 必须相同。配置/文本漂移另行设计。
5. pending 须没有初审；approved/rejected 须有一致、非自审的唯一初审，冻结其完整 proof/hash。
   处置本身不要求目标先获批准，也不为目标增加初审记录。
6. 记录时验证当前读取权限，最终响应前再次校验来源、目标与输出；只返回该两份合法来源。
   不 hydrate 整 batch/cohort、不重算冲突或 confidence，不获取其他标注员的答案。

这些检查基于现有持久化 Submission 身份、snapshot/hash 和合法 submit 工作流，
不声称能从旧表溯证每次历史 API 调用入口及其当时权限，也不补造缺失的命令审计。

记录表达的是处置人的**关联判断**，原因说明为何关联；不自动证明新答案已修复 concern。
后续目标被审核或出现更新修订，不改写 `status_at_record`/proof，不自动换链或更新原 batch。
历史展示区分“记录时状态”和按需读取的“当前可验证状态”。

原 batch/task/assignment/submission/初审/opinion、必要 actor FK 或目标缺失时，
新处置和答案读取 fail closed；不按冻结数字 ID 重绑复用对象、不补当前来源或 refresh profile。
已存处置可按当前 Manager 权限展示冻结元数据，标记 `source_integrity=not_checked`；
只有独立 source read 验证成功才能说 `target_source_verified`。
`metric_integrity=not_checked`，不把未验证 cohort 质量说成已通过。
必要源损坏必须区分 CORRUPT 与 unavailable，不能将损坏降级成未知来继续写入。

## 6. 权限隔离与运行开关

新增提议操作 `record_second_review_followup`，不得由 legacy、review、second_review、
organize_second_review 或 Manager 角色自动推出。
新增 default-off `ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED`；仍要求原二审 enabled、
稳定 `ANNOTATION_SECOND_REVIEW_INSTANCE_ID` 和精确 org/project fork_review mode。
不另造 instance 身份；Core-only/未知 mode/不支持的 DB 拒绝。

| 动作 | 当前权威要求 |
| --- | --- |
| concern/处置集合、单项冻结元数据 | 当前组织及项目 Manager、READ_WORK、feature/mode/instance；不因有记录而绕过撤权 |
| 新处置 context/提交 | 上述要求 + 显式 record_second_review_followup；处置人独立性；源策略当前读取 gate |
| 两份 exact 答案/比较 | 上述 source 读取权；原 strategy flag/mode/read scope 和 targeted 来源验证 |
| 自己原 key 的恢复 | 当前 Manager + READ_WORK + 显式 record 操作 + feature/mode/instance；同 principal/source/actor；不依赖源/目标存活 |

**处置人不得是原 submitter、assignee、初审人或二审人；分类冲突来源还排除完整 frozen cohort
中的所有 submitter/assignee。** 这是待确认的 V1 独立性选择，不能用换 principal 或客户端 actor
绕过同一 local human ID。新修订同样应用独立性检查。历史初审人后来降权不抹掉合法历史；
处置人当前账户、membership、Manager 角色或操作被撤销则立即拒绝。

其他 Reviewer（含原二审人）、Annotator、跨组织/项目、未分配身份无新跟进接口权限。
旧二审人的已有读取边界不扩大。集合先过滤再计数/分页；详情、context 和直调写逐对象复核。
actor、组织、mode、effect、rule、time 不接受客户端字段。UI 隐藏不承担安全职责。

## 7. 拟议 API、幂等与并发

新独立前缀 `/api/projects/{project}/second-review-followups/`，保留旧二审 wire：

| 接口 | 固定职责 |
| --- | --- |
| GET collection | 派生 concern + 是否已有处置的有界元数据列表；默认 page_size=50，最大100；按 opinion created_at、UUID 固定排序；不返回答案/全文 plan |
| GET `context/{second_review_task_uuid}/` | 无目标或单一 related_submission_id 的预览，返回 concern_hash/context_hash、viewer/owner、expected_version=0、可提交资格及有界的原/目标只读投影 |
| POST collection | 追加一份不可变处置，201；相同原键恢复200 |
| GET `{followup_uuid}/` | 冻结元数据，source_integrity=not_checked；不隐式读取旧/新答案 |

context 的原/目标只读投影通过窄 targeted adapter 取得，UI 复用既有受控展示；
原源与可选目标均成功验证才返回投影，不能先返回一侧再忽略另一侧失败。
GET 不产生 Submission 或任务；没有目标时 target 严格为 null。
集合只接受固定分页参数；context 只接受一个可选 positive related_submission_id，拒绝重复参数。
cursor 签名绑定 instance/org/project/current owner、固定 concern 选择条件、排序与分页，15 分钟有效。
列表和单项历史在来源不可用时仍为元数据 not_checked；context 作为 fresh 来源读取则整体拒绝。
已有处置的行仅显示回执/历史，不再返回 version=0 的可提交 context。

POST 固定字段：`second_review_task_id, expected_concern_hash, expected_context_hash,
expected_viewer_hash, expected_version, disposition, reason, related_submission_id`。
no_change 的 target 为 null；revision_linked 为 positive int。expected_version 必须严格 int 0。
context_hash 绑定 exact concern、选择的目标 proof/记录时初审或 null、效力/规则/作用域；
关联目标 latest 资格还须在提交事务中实时复核。no_change 不用“当前最新答案”替代原来源。
拒绝额外字段、重复 JSON key、bool-as-int、NaN/Infinity、无效 UUID/hash、非 UTF-8 和 query 注入。
Idempotency-Key 必须 canonical UUID；恢复不能跨路径 subject、scope 或 action。

事务顺序：验证当前 caller → 严格解析 → 查原 key；命中则进入 receipt-only 分支。
fresh 分支先取得有限身份 hints → Project fence + 所有相关 humans/membership/User/ProjectMember
按既有固定顺序 → 重新校验身份与 operation → 重查原 key/已存在 subject → Task/Assignment →
原/目标的所有 Submission（按 ID）→ 原/目标 ReviewDecision（按 ID）→ ReviewBatch →
原二审 task/opinion。原/目标 task、assignment 去重，各资源集合一次锁齐，随后重验 hints。
不能先运行只锁原来源的 helper 再补目标锁并声称全局有序；窄 source adapter 须遵守组合锁序。
所有 related human locks 在 Project fence 一次取得，不能在持有来源锁后补取身份锁。
需与既有 submit/review/revocation 路径作 PG 双连接验证。

CAS 是“仍无处置”的 version=0 + DB subject 唯一约束。一个 concern 的不同 key/actor 竞争仅一条成功；
同 key/body/owner 等待后恢复同回执。冲突不吞成500，不自动换 key/重预览再写。
仅将已知 key/subject 唯一约束竞争映射为幂等恢复/409；须在有效 savepoint/事务边界外处理，
不能吞其他 IntegrityError、使用已损坏的事务或把真实约束缺陷当作成功。
若 membership/目标 submit/初审先提交，等待者按新状态拒绝或要求刷新；若本写先提交，
后来的合法状态变化不能回写这份冻结处置。

原 key 恢复校验 typed 存储记录/receipt/hash/原 body hash 和原 principal/source/actor，
**不读原 batch/初审/答案、目标或当前 profile，不评分，不换来源，不比较旧 viewer 与新 viewer**。
同 owner 的 viewer 轮换可恢复原 body；主体变化仍拒绝。响应显式 replayed、source_integrity=not_checked。
当前权限撤销、feature 关闭或 instance 改变不能靠旧 key 绕过。
原策略 source gate 只约束 fresh source/target 读取；receipt-only 恢复不读取答案，
不重新要求 producer grant/fresh profile，也不假装原策略来源已验证。
响应渲染后再做当前权限、输出 hash/字节及 fresh 来源复核；失败丢弃答案，写入整体回滚。
渲染和响应校验必须位于原子命令内，不能先提交再补检查。

## 8. 预算和最小公开面

- request 在解析前总字节上限16 KiB；reason 非空白、无 NUL，最多2000字符且 UTF-8≤8192字节。
- 请求级总预算 canonical 8 MiB / SQL stored 32 MiB，原来源、二审 metadata、目标与响应前复核
  均不得通过新 Budget 重置逃逸；分次物理读取均计入同一请求预算，不能无界重试。
- plan 沿用 canonical4 MiB / stored16 MiB；单 snapshot canonical256 KiB，单源 stored row2 MiB，
  原/目标 snapshot 的 canonical union≤512 KiB；仍须通过请求总预算。
- 新记录 origin_ref + receipt 等主要 JSON metadata 单记录 union canonical32 KiB / stored128 KiB；
  原二审 task/opinion 各沿用现有更严格门禁，不复制完整 plan 或答案到新记录。
  包括 reason、identity/time、SQLite 弱类型 scalar 在内的所有选中字段均做 SQL 前置字节/类型门禁。
- metadata/列表响应≤4 MiB；含最多两份 snapshot 的 context/比较响应也≤4 MiB，先有界后 materialize。
  超限422，不截断理由、不跳过候选、不 silently fallback、不改变冻结分母。
- source loader 的固定重复验证次数必须可计数，禁止整批 hydrate、N+1 后置 payload 查询、
  全 cohort 答案或 producer/scorer 调用；分页 SQL 先 limit，计数不解码 proof/快照。
- 新 GET/POST 都 private,no-store 和 no-referrer；typed 最小 DTO，不给 raw producer proof、token、
  full roster 或他人独立答案。无新下载、bulk、export、import、file/media、storage endpoint。

## 9. UI 与异步恢复约束

复用 Manager 二审历史入口：concern 行 → 只读 exact 初审/二审来源 → 选择处置类型 →
（可选）选择已有同链新修订并比较 → 填原因 → 明确确认“仅记录跟进，不控制发布” → 提交回执。
处置前同时显示初审结论、二审 concern、所选 revision 和新修订记录时状态；没有目标时明确 null。
不是把后台 policy 细节或内部 hash 强迫用户手填。

提交中冻结原 body/key；网络结果不确定显示 UNKNOWN，保留原命令并提供原键恢复，不新 key 重发。
新选择或来源变化清除 preview；晚到的 context/source/target/list 响应按 request generation + owner
丢弃，不能覆盖新选择。失权/跨项目/换 actor 隐藏已载答案并废弃可写 context。
键和私有答案不写入共享持久缓存；页面离开后无法恢复的命令不得宣称已失败或自动重做。

进入详情和返回列表的焦点复用 PR #133 的显式生命周期；加载/刷新/分页前先保留合法焦点落点，
不能因 disabled/unmount 落回 BODY。受控延迟响应验证 Tab 顺序、原生 Enter/Space 激活、
错误/UNKNOWN/成功后 focus、返回和重复进入；不以脚本 click 冒充原生键盘验收。
中文/英文全路径与状态文案都覆盖。

## 10. 实施闸门和未执行项

本提案需要用户确认的选择：单次追加处置、两种 disposition、不新增执行中派发状态、
处置人与标注/初审/二审及完整冲突 cohort 独立、关联修订固定同链同文本/配置、
不含仲裁关联和审计结果下载。它们是本轮设计建议，不是已经批准的运行政策。

确认后才可在本独立 worktree 开发、RED/GREEN、PG/SQLite/双语真实浏览器验收，
并在用户要求独立 reviewer 时安排复审，修复后补验收。远端 push/PR/CI、merge/main CI、
部署/真实 Host 或用户验收与 Issue closure 分别授权。
本轮功能/DB/浏览器/远端验收均 **NOT_RUN**；独立 reviewer **NOT_RUN**。
Fork-only；#81/#84 保持 OPEN；Docker/#76、主仓、原聊天目录和其他既有 worktree 不动。
