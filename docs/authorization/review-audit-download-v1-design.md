# #84 审核审计结果下载 V1：设计提案

2026-11-04；基线 `main@78b6c9a0cfd0f85a69300d31276aa0d1daeed480`，tree
`b179cfeca633ed2a39f21564848f92eadbf406bd`。**用户已批准本设计及本地实施；本地结果见配套验收记录。**
原设计及独立设计审查的封存副本仍保留，本文件记录实现中的兼容性澄清；远端及部署另行授权。
配套：[来源与复用映射](review-audit-download-v1-source-map.md)、[验收矩阵](review-audit-download-v1-acceptance.md)。

## 1. 效力、范围与“完整”的定义

用户已确认“审核结果下载”指完整审核审计包。V1 为 Fork 内、单个 ReviewBatch、静态 inline
单 Text + static Choices 的**有界完整关系图的固定脱敏快照**，效力固定 `supervisory_only`。
普通、分类冲突优先、revision-confidence 批次复用原协议；NER/字段/bbox 批次明确不支持。
已有二审/concern 跟进均复用，不增加审批、仲裁、返工、发布、DatasetVersion 或 Gold 权威。

完整的含义是下述已声明范围中每个应有实体都有记录或明确 gap，不是恢复所有已删除历史，
也不是原始文本/答案/理由副本、永久审计平台、匿名化证明或法律真实性认证。
新包不改变原 ReviewDecision、Submission 状态、ReviewBatch.plan、二审/concern、#79/#80 字节或权限。

范围闭包：

1. 批次完整 frozen candidate universe（含 selected / unselected / confidence-uncovered），不只导出已批准项。
2. 每个 candidate 的原 assignment 在冻结前可观察的全部修订（含早于 batch source 的修订）与初审；
   原冻结 candidate 消失也保留该根位置和 gap。只读差异采用现有静态分类比较契约，不调用模型/scorer。
3. 明确绑定本 batch/subject 的 SecondReviewTask、意见、取消和 concern follow-up；
   nullable FK 为空时，按已有冻结 receipt/proof 验证历史绑定，不按相同数字 ID 找一个当前源替代。
4. 由本 batch 的分类 handoff 直接指向的分类 arbitration/task/decision/cancellation 与冻结 cohort 依据。
   cohort 中非根 assignment 的输入只作为 `boundary_source_ref` 表示，不递归扩展其所有审核历史；
   其他 batch 的二审/仲裁、整个项目历史均不在闭包内。

ReviewDecision 没有 review_batch FK：其关联标为 `subject_observed`，
`batch_execution_provenance=not_recorded`。不把“这个 candidate 的初审”说成“在这个 batch 内执行的初审”。
“未观察到意见”不同于“证实尚未审核”：approved/rejected 无意见、或冻结二审 proof 引用已缺失初审，必须 gap。

## 2. 现有架构边界与复用决定

#79 的 ApprovedDatasetRelease 和 #80 的 ReleaseSnapshot 强绑 `fork_approved`、READY release、
批准来源、creator 和完整 live source 图。包含 rejected/pending/unselected 的审计不能伪装成这些领域对象。
**不直接继承 ApprovedExportAPI / ReleaseSnapshotAPI，不放宽原策略，不给原 Manifest schema 增加另一种效力。**

拟在现有 `data_export` app 内增加窄的 audit adapter/模型；机械基础设施复用或提取有参数的窄 helper：
私有目录保留、RQ alias/健康检查、token attempt fence、流式文件生成、同 descriptor 校验、Range reader、
JSON/JCS/hash/manifest 校验、既有客户端 authenticated download helper。禁止复制一套通用导出服务。
提取 helper 时保留 #79/#80 现有调用和回归；领域 guard/creator/policy/源存活谓词不能混用。
现有通用 Export/DataExport.generate_export_file 含用户信息、mutable task、Converter/resource 下载语义，
不能作为审核审计捷径，也不解除 formal Submission 对 generic export 的现有限制。

现有 ReviewDecision 不是 DB 冻结的永久账本；本提案只在 T 冻结当时可观察的类型化事实，
不能追认 T 前从未保存的理由、真实原始发生时间、已删除事件或执行归属。Host 仍拥有长期保留和业务审计政策。

## 3. 冻结时点、预览与来源版本

`preview` 在 P 时刻给出范围计数、gap 分类、固定脱敏说明及 `batch_binding_hash` / `capture_preview_hash`，
不生成审计包、不保留答案。P 不是冻结。其正文不返回原文、答案、自由理由或人名。
这两个对客户端的 hash 是独立 preview signing key 下的 domain-separated HMAC，不是原 plan/答案的裸 SHA；
私有 version vector 不出接口。preview vector 排除每包随机别名和观察时钟，包含全部语义成员/版本/内容绑定，
所以无源变化时可重验。key/version 轮换使未提交 preview 失效，不能自动刷新后创建；原命令恢复不重验 preview。

T 是创建 AuditPackage 事务成功提交的线性化点：

1. 外层事务取得 Project fence，先恢复同 owner/key 的原命令，再读取新的 live source hint。
2. SQL scalar/length preflight 后建立完整有界 membership，取得当前 authority/human fence，
   按既有顺序锁 Task → Assignment → Submission → ReviewDecision → ReviewBatch；
   随后锁直接 handoff/classification/arbitration 图，再 SecondReviewTask → SecondReviewDecision → Followup，
   同表按主键排序，AuditPackage 最后写。已核验的审核类写路径先取 Project fence，不能先锁 batch 再锁 task。
   **不能据此假定现有全部删除路径已经有 fence**：ProjectAPI.perform_destroy、ProjectTaskListAPI.delete、
   TaskAPI.delete/Task.delete_tasks_without_signals 等必须在未来实现中加入/复用最小 Project-first 协调点，
   在 ORM Collector/子行锁之前取得相关 Project（多项目按ID排序）。权限与既有删除语义不变。
   这是实现前置范围，不是当前已具备的保证；同时审查内部批量/cascade入口，不能只保护按钮或一个URL。
   删除与capture互斥、全量锁顺序兼容性必须通过 PG 双连接验收；未协调的维护路径不得与capture并发，
   DB冲突/死锁必须整事务回滚且有界重试，不输出半冻结包。不引入长事务中的文件/网络/RQ I/O。
3. 在相同 byte Budget 中两次独立建立完整 membership/version vector、类型化证据与 rendered projection；
   identity/role/op/policy、成员集合、源 pin、意见和关联有变化则回滚。SQLite 先在本事务创建不可见 capture
   claim 获得写序列化，失败整事务回滚；锁冲突明确重试错误，不能退为无锁成功。
4. P 的两个预期 hash 不匹配返回 `AUDIT_PREVIEW_CHANGED`，不产生新包。匹配后同时写入冻结私有
   InputBundle、固定 public projection、receipt 和创建 access event；`on_commit` 才尝试 enqueue。

`captured_at` 是最后一次验证时的服务器观察时间，在事务内冻结；不是数据库精确 commit wall-clock 认证。
T 后的审核、修订、仲裁、concern、改名、删除都不进入本包；新快照必须新 key/新 package。
worker 从不重新选择或补全来源。batch/源删除、producer grant 退休不会使 retry 改变 input。

私有 InputBundle 必须绑定：

- audit instance UUID / org / project；root batch ID、plan/request hash、creator binding、created_at；
  `batch_binding_hash` 不仅是一个可复用数字 ID。
- 原 sampling schema/algorithm/seed/quota/denominator/selected flags；classification rule/input set/metric hash；
  confidence descriptor/profile digest/producer namespace/model/evaluator/calibration version及原 observation/receipt。
  保留原版本，不再评分或查询当前 producer grant 来重写历史。
- 每个 Submission 的原九 pin、revision、result/task-data/config SHA 及原 snapshot canonicalization 名称；
  初审的当前完整 proof hash、二审/concern/handoff/arbitration 的 schema/rule/effect/version/receipt/origin hash。
- 全部 scope、entity membership、gap、private raw binding → public opaque ref 的映射、固定 redaction profile、
  public projection SHA、request SHA、creator audit actor generation/owner fingerprint、T 的允许回执。
- `audit-zip-format/v1`、formatter compatibility digest、canonicalization/ZIP精确契约、
  README精确UTF-8字节及SHA；见第7节。不能只存“使用当前formatter”。

所有 graph/field 白名单版本固定为 `review-audit-source/v1` / `static-text-choices/v1`；未知协议版本拒绝。
普通初审标 `observed_at_capture`；有冻结回执的历史事实标 `frozen_receipt_verified`；不能统一包装为永久原始事实。
现存源与已有冻结 proof 冲突是 `SOURCE_INTEGRITY_INVALID`，不是 gap；T 前损坏数据不得被正常 READY 掩盖。

## 4. 缺源、删除、ID 复用和计数

| 时点/情形 | 明确行为 |
| --- | --- |
| P/T 前 root batch 不存在、plan/hash 损坏或绑定无法成立 | 拒绝新 capture；不能猜完整 universe |
| root plan 健康，某 frozen candidate/修订已缺失 | 保留根位置；以 plan pin 或可验证 frozen receipt 形成 gap，绝不换成 latest 或同数字 ID 的新对象 |
| nullable FK 已空且 receipt 能证明原 batch/subject | 收录历史 receipt 的脱敏投影，`live_origin=unavailable`；关联 proof 不足则 `ambiguous_binding` gap |
| 已有 approved/rejected 状态但 ReviewDecision 缺失 | `initial_review_unavailable`；不是 pending、未审或零意见 |
| 仅没有可证明的历史事件 | `history_completeness=not_provable`；不声称从未发生 |
| T 后 batch/task/assignment/submission/人/原意见删除 | worker/download 只使用冻结 bundle/文件；允许按当前 audit 权限读历史包，不读取重建同 ID 对象 |
| T 后原包 creator 的 User 账户删除 | audit actor binding 永久tombstone；所有原包访问/worker/retry/原key恢复拒绝，同数字ID新账户不能接管 |
| T 后 source 状态改变或新关系出现 | READY 文件和原计数不变；public manifest 明示 `as_of_capture`、`live_source_integrity=not_checked` |
| current project/org 软删除、账户/成员/op 撤销、audit mode 关闭 | 所有旧 URL、metadata、manifest、HEAD/Range/conditional request 立即按当前 gate 拒绝 |
| Project 硬删除 | 审计模块不新增 Project PROTECT；Project 删除后 DB-owned cleanup 删除 audit 业务行，旧 URL 不可恢复；原 #79/#80 的独立 PROTECT 不改 |
| 删除后复用 project/batch/人/Submission 的数字 ID | 原包的 namespace/owner/binding 不重绑定；Project 硬删后原 package UUID 不存在 |
| READY 文件缺失/size/hash/inventory 异常 | quarantined，拒绝任何部分字节；不能使用旧 key 从当前来源重造 |

新模型 Project 关系拟复用 concern 的 DO_NOTHING + parent-gone DB cleanup 模式；不能靠禁 FK/guard 完成清理。
Project 尚存时直接删除 package/业务冻结行拒绝。Project 硬删后私有文件可以成为不可达孤儿，
没有公开路径；本 V1 不新增文件清理/保留期限作业，也不承诺物理擦除。后续受控保留/擦除另行设计，不动 Docker/#76。

每个 frozen candidate 恰有一个 root record；revision、意见、handoff 等另有唯一 typed entity record 和显式 edge。
`coverage=complete` 只表示声明图的记录/缺口枚举完整，另给 `evidence=complete|with_gaps`、gap counts。
redaction 与 gap 分开计数；不能将隐藏字段计成“无理由”，或缺失记录计成“0 次操作”。
selected/unselected、source state at T、superseded、unavailable 分列；unchecked/skipped永不隐式 Approved。

## 5. 固定脱敏：完整链路，不导出原文副本

V1 只有 `review-audit-redacted/v1`，无客户端选择“原文/实名/带答案”开关。
界面预览和包 README 明示以下取舍，作为用户下一次确认设计的重点。

| 数据 | 包内表示 |
| --- | --- |
| 原任务 text、XML、答案/label 值、媒体、外链、draft、Annotation、token/cookie | 不导出，也不放进日志或文件名 |
| 自由 reason/备注、项目/batch 自由名称、人名/email/username/principal/local user ID | 不导出；理由只给 `reason_state=absent|present_redacted|unavailable`，不做不可靠的正则自动脱敏 |
| 意见类别、取消/处置类别、sampling命中类型、revision序号、schema/rule/version、时序 | 类型化白名单，分别表示初审/二审/concern，保留 `supervisory_only` |
| 样本差异 | 现有静态分类比较的 `equal|changed|unavailable`、added/removed counts和输入 opaque refs；不含label值，不创造新质量分数 |
| 技术源 ID / 原文/答案/reason/plan/result 等 raw SHA | 只留私有 bundle；包内用 package-specific opaque refs/commitments，避免公开低熵 hash 的枚举和跨包关联 |

采用 stdlib HMAC-SHA256 + capture 时一次性随机256-bit secret，domain 分隔实体/字段/绑定类型；
同包同一经证实实体一致，不同包不可直接连接。**T 时同时冻结 public projection 和私有 binding map，之后丢弃临时 secret**；
worker/retry 不重算别名、不依赖 SECRET_KEY 或旧临时 key，不旋转已有包的引用。事务回滚不产生公共引用。
私有 bundle 也不复制原文、答案或自由理由，仅存验证所需的类型化 pin/hash、scope/actor标量与映射。
公开 version 只允许 source-map 中固定的协议常量；自由 model/producer/profile/policy 标签不得直接出包，
其精确版本/digest 绑定保存在私有 bundle，公开仅给类型化来源类别与同包 opaque commitment。
既有历史 source actor 没有账户代际证明时标 `actor_incarnation=not_recorded`，仅给 record/role occurrence ref；
不能按同一个数字user ID或旧owner hash把多次历史行为合并成同一自然人。此限制与完整实体/edge计数分开声明。
公开 package/opaque ref 用规范 UUID/hex 字符串；精确大整数、旧Python snapshot hash保持私有原格式。

这是面向有权限审计者的假名化；保留的时间、revision和统计仍可能辅助关联，不称匿名化或自动合规。
外部持有人可验证包完整性，不能仅凭本包还原原文或独立证明 T 前原始事实从未修改；Host 自行定义接收权威。

## 6. 权限、配置与身份隔离

默认 `ANNOTATION_REVIEW_AUDIT_ENABLED=false`，operator-owned policy 按 org:project 明确 `fork_review`、
稳定 audit instance UUID、policy version、redaction version、格式/预算。缺失/未知/core_only_internal 一律拒绝。
不依赖/改写 APPROVED_EXPORT_POLICY 的 `fork_approved`。源模块开关退休不授予 audit 权限；
已有冻结包仅按独立 audit policy 读取，不能借旧源旗标或旧 producer grant 恢复新操作。
新 capture 需要可验证的历史 source contract，不重新执行已关闭源模块的命令。

拟新增单一 `EXPORT_REVIEW_AUDIT` operation：所有 collection/preview/create/detail/manifest/retry/download
均需当前 active Manager、live org/project membership、READ_WORK 和此 op；legacy provider 不默认赠予。
Annotator、Reviewer、org admin、无 scoped Manager 映射的 superuser、Core-only 和未实现后台解析的身份提供者均拒绝。
human actor 仅从 trusted identity mapper 得出；客户端 actor/reviewer/mode/effect/time/Host 字段拒绝。

审计读取不是审核决定：固定脱敏包可记录调用者自己的操作，不新增“独立审计员”产品角色，
不通过导出权限获得任何 initial/second/follow-up/arbitration 写能力。上述原命令的自审/cohort隔离完全不变。
无答案/原文/低熵raw hash出包，是此权限决策的前提；未来非脱敏模式必须另行做 cohort 和权限设计。

每次读取/下载都检查 requester 和原 creator 的当前完整权限、相同实例/policy/redaction版本。
现有 LocalIdentityProvider 的 principal 只是 source+数字User ID，原owner hash不是账户代际证明。
拟在audit域增加窄 `AuditActorBinding`：不可复用的随机UUID generation、audit instance、nullable liveUser FK、
不可逆tombstone。首次**已授权create写命令**在锁定User后创建binding，active(instance, liveUser)唯一；
GET/preview不隐式建binding。existing binding用于原key资格判断；新User首次create用新generation和新UUID key，
不能借新代际/instance转发旧UNKNOWN命令。
creator 身份绑定包括 generation + principal/source/local actor/project/org/instance；包、owner/key命名空间、
后台resolve和最终gate均要求原generation仍关联同一liveUser、未tombstone、原身份元组仍匹配。
User的便利FK采用DO_NOTHING和DB parent-delete hook：删除User的事务原子置null+tombstone，
禁止null→nonnull、换User、复用generation或删除墓碑。PG/SQLite都必须覆盖ORM/bulk/directSQL删除。
首次binding/capture为Project→authority/User→binding→source；User删除的API/ORM协调需在Collector前
按ID排序锁相关Project，再User→binding，禁止持有User后再取Project。directSQL触发器仍原子墓碑化；
冲突/死锁整事务回滚，不允许成功半删除或失去墓碑。兼容入口清单与真实PG删除/创建竞争属于实现前置。
binding仅为本audit域不可重绑凭据，不改变全局Principal或给旧源补造历史generation；无公开管理/重新绑定入口。

creator仅撤权不删除或改写READY内容，但当前所有内容访问拒绝；原live账户未删除、generation/身份/版本不变，
合法恢复权限后可再读取。creator User删除则**永久拒绝**原包，绝不从冻结数字ID重新hydrate creator。
其他source actor删除只保留脱敏历史；不能把这种来源保留规则误用于creator访问门。
metadata collection同样按这些条件过滤，不泄露失权包的存在/计数。read能力不能继承创建时权限缓存。
worker 使用现有可信 resolve_background，队列只放 package UUID + attempt token，绝不保存 session/token。
worker start 和 publish 各做当前 creator gate，非Local provider无该 hook 就拒绝创建，不提供同步 fallback。

新 capture/final publish 使用 Project → org membership → User/role/op fence 的既有兼容顺序；
read/render/access audit完成后再检查当前 identity/op/version。授权序列化到受支持撤权路径前/后之一，不能返回部分字段。
静态/LocalFiles/import/storage/blob/Nginx/signed URL等不得直接访问整个新私有 audit subtree，
即使 audit 开关关闭也保留 private-root reservation。Scope无法建立时默认拒绝。

## 7. 拟议存储、文件和重试

新增窄域 AuditActorBinding / AuditPackage / AuditAttempt / AuditArtifact / AuditAccessEvent（名称待实现时对齐既有 app），
不新增 scheduler、IAM、队列服务或 Host 数据库依赖。私有 InputBundle 与 public projection 在 T 原子冻结，
input SHA和projection SHA分别保护；PG/SQLite DB guards保护全部业务字段（含JSON数值类型/重复键）、来源绑定、
creator generation/namespace/key与READY封口字段。便利源 FK只能 SET_NULL，不能重绑；
AuditActorBinding仅允许User删除导致的不可逆tombstone转换；Project cleanup见第4节，不删除其他项目共用的binding。

状态：`dispatch_pending → queued → running → ready`；另有 `failed_retryable`、`blocked_auth`、
`blocked_runtime`、`failed_final`、`quarantined`。未完成 READY、审计写失败或文件校验失败均无下载字节。
T事务不依赖网络；enqueue失败留 dispatch_pending，显式 retry 复用冻结 bundle。
复用既有 RQ alias、3s健康检查、on_commit及token/attempt fence，独立 job 函数，不修改全局CE queue行为。
每 project最多2个active包、每owner最多1个；worker job上限30分钟，不在请求中同步生成文件。

create key的保留/冲突范围为 project+key（DB唯一）；精确恢复资格为原stable owner generation+principal/source+
instance+原bodyhash全部相同。bodyhash同时绑定source batch binding、preview hash和固定profile。
先检查当前audit权限，再在Project fence内查任何owner/instance已保留的key；不匹配仅generic409，
不返回原receipt/UUID/状态/计数，不查来源/文件/队列、不创建新T。合法新操作使用新UUID key。
通用冲突对当前已授权Manager提供“已知key不可复用”的最小信号，不承诺此命令完全无key保留信号。
原key精确重试先查当前audit authority及自己receipt，**不查询任何batch/revision/意见/producer/新图**。
同key改body、换root/owner/instance冲突；UNKNOWN保留原body/key，不刷新后偷偷重发。
不同key明确创建新的T，可有相同业务内容但新package/opaque refs，不能覆盖旧包。

retry 命令使用独立的 `audit-retry/v1 + package UUID + stable owner + instance + key` namespace，
不能与 create key 相互命中。首次命令在 Project/authority/package fence 内检查状态并冻结重试回执，
记录原/新 attempt 与 token；相同 key 精确恢复只返回该回执，不再次 enqueue 或推进新状态。
新 key 对尚健康的 active attempt 只返回当前状态；仅 dispatch_pending、可重试失败或达到下述时限的
queued/running 可分配新 token。blocked_auth 在原身份/版本合法恢复后仅允许显式新 retry 命令；
blocked_runtime仅在原兼容formatter恢复后允许显式新retry；协议损坏/failed_final/quarantined/READY
不重生文件。enqueue 的未知结果由 token claim 消重。

queued超过最近一次成功enqueue起30分钟可换token；running超过31分钟显式fence旧token，
新attempt仍只格式化同一T的projection。V1可对I/O失败/中断做此精确重生成，因为T已完成、无后续媒体捕获。
旧worker只能清理自己且尚未发布的scratch；不能删除已READY或提交结果UNKNOWN的文件。
blocked_auth/blocked_runtime/协议损坏/READY quarantine不自动重试，禁止用旧key重捕获当前源。
READY不允许更换artifact、manifest、attempt或token；只可转quarantined，不能恢复为READY。

固定 ZIP 文件集：`manifest.json`、`records.jsonl`、`README.txt`、`SHA256SUMS`。
records逐行JCS+LF，manifest为现有RFC8785/JCS，schema使用锁定jsonschema；不手写另一套canonical serializer。
记录顺序为type+opaque ref，ZIP_STORED、固定entry时间/权限/顺序、固定UTF8和静态双语README，
使同T/同runtime的worker retry产物相同。manifest绑定records和README SHA/size；
SHA256SUMS绑定manifest及payload，不包含自身；整个ZIP SHA/size由私有Artifact记录和HTTP头给出，避免自哈希循环。
public manifest不包含私有InputBundle、storage key、binding map、creator标识、raw源hash或临时HMAC secret。

T冻结 `audit-zip-format/v1` 的完整格式契约：四entry顺序/名称/时间/权限/UTF8/ZIP_STORED设置、
records/manifest canonicalization及README精确字节。formatter compatibility digest绑定所用audit formatter
代码、Python implementation/full version/zipfile兼容指纹、锁定rfc8785/jsonschema版本与格式常量。
首版只接受完全匹配，不引入未验证“兼容版本”别名；不能沿用approved.runtime的release专属映射作证明。
worker start和publish均检查当前compatibility digest与T一致，README取冻结字节；部署漂移进入blocked_runtime，
显式恢复原兼容环境才可source-free retry，不从新代码读取新README、不重canonicalize已封口的public projection。
READY只需独立完整性/当前权限gate，不因本机formatter升级而重生成或失效；旧协议reader不可识别时仍fail-closed。

hash/serve复用 `snapshots.checked_file` 的同descriptor路径和 `PrivateRangeResponse`，
还必须验证祖先路径无symlink/reparse、私有root的可信owner/写权限、常规文件、完整inventory/size/hash。
helper的leaf O_NOFOLLOW不单独保证所有TOCTOU。**支持部署的前置条件是可信OS私有根、祖先及发布后不可变内容**：
其他账号无写权限，可信应用写者只创建token-owned scratch并原子发布，绝不原地修改已发布inode。
首字节前可检测的替换/截断/同inode篡改须拒绝；该FD校验不冻结未来inode。
开始stream后有OS权限的写入、root/服务账号受控之外修改属于前置条件破坏；不承诺此时始终无错误字节或撤回已发字节。
不把仅leaf nofollow/一次stat称为防御恶意OS writer的证明。任一新Range请求重新授权、重新完整性校验；
FD按既有response生命周期关闭，不在锁内持续stream。不具备这些OS前置条件的环境不支持启用V1。

allow access event写入失败时不交付，deny event尽力记录；access事件只留内部package/actor/policy/phase/code，
不记source正文、reason、请求原body或凭据。requester/creator权限、版本、inventory和FD在发出header/首字节前最终检查。
不新增公开静态路由、通用exports入口、转换器或外网下载。

## 8. 拟议API与页面

base `/api/projects/{project}/review-audit-packages/`，独立 guard，不继承批准发布 API。

| 方法/后缀 | 契约 |
| --- | --- |
| GET capability/ | 当前可用profile、限制，不返回任何source内容 |
| POST preview/ | strict body `{review_batch_id}`；返回root binding、图hash、count/gap和脱敏说明 |
| POST base + UUID Idempotency-Key | strict `{review_batch_id, expected_batch_binding_hash, expected_capture_preview_hash, profile:'review-audit-redacted/v1'}`；原子T，202 receipt |
| GET base | 当前授权history，page≤50，签名cursor绑定owner/project/instance/policy/order；无任意filter/排序/offset |
| GET {package}/ | 状态、scope/count/gap、冻结时间、固定profile、READY已验artifact元数据 |
| POST {package}/retry/ + UUID key | body `{}`；原creator显式操作，新namespace回执；不得重捕获 |
| GET {package}/manifest/ | 固定JCS public manifest，X-Manifest-SHA256，当前完整gate |
| GET {package}/artifacts/{artifact}/download/ | 固定ZIP，当前完整gate，HEAD/单Range/ETag/If-Range同等检查 |

未授权在任何304/416/HEAD/OPTIONS之前拒绝；不靠URL不可猜测、前端隐藏或浏览器缓存当安全边界。
重复/未知query、query重复key、非正整数、bool作ID、重复JSON键、short/chunked超量和extra字段严格拒绝。
文件名固定ASCII `review-audit-{uuid}.zip`，无用户字符串；private,no-store / nosniff / attachment。
下载时当前source不可用不触发live源查询，当前project/policy/双creator/requester gate仍必须通过。

UI只在现有ReviewBatch详情加“审核审计包”入口：范围/脱敏说明 → preview counts/gaps → 明确确认
“只冻结T时证据，不改变审核/发布” → history/status → authenticated download。复用现有下载helper；
不新增平行审核工作区，不自动采集/轮询写命令、不把包READY标成审核通过。
generation/owner/project guards丢弃异步旧响应；UNKNOWN仅原命令恢复；读失败清除权限派生字段和链接。
原生Tab/Enter/Space、disabled前焦点迁移、返回焦点及不抢用户焦点沿用现有生命周期。
双语文案区分“完整范围”“证据缺口”“已脱敏”和“未验证当前源”。

## 9. 硬预算与失败分类

| 对象 | V1硬上限（operator只可调低） |
| --- | --- |
| 请求/preview/detail/history/manifest | 16KiB / 128KiB / 128KiB / 256KiB / 256KiB |
| frozen root candidates / distinct root assignments | 1000 / 1000，整批，不做隐藏截断 |
| 全部root lineage revisions / 每assignment | 2000 / 20；超界整包拒绝，不只导出前20 |
| graph entities / edges / cohort references | 5000 / 10000 / 2000 |
| source snapshot / 同次snapshot union / stored row | 256KiB / 512KiB / 2MiB，保留原source限制 |
| complete source读取（两pass合计） | canonical8MiB / stored32MiB，复用Budget/SQL CASE预检，counts和scalar也计入 |
| 原plan | canonical4MiB / stored16MiB，且受共享累计预算 |
| 私有frozen bundle / public records | 8MiB / 8MiB |
| ZIP / scratch union / files | 16MiB / 32MiB / 固定4文件；每payload仍≤8MiB |
| page / active jobs / worker | 50 / project2且owner1 / 30分钟 |
| audit source loader SQL round trips | 每pass≤128、一次两pass≤256；不含已有共享authority入口的固定基线 |

Graph计数先SQL scalar COUNT/受限ID枚举，JSON/Text/用户名/时间字段materialize前按实际stored bytes CASE gates；
禁止通过defer后访问绕过预算。重复source/pass按实际读取累计，不把同hash当免费缓存；
有界批量查询的计数/锁定/byte preflight/hydration全部计入SQL上限，执行下一条前检查预算。
固定chunk≤250个ID，禁止逐entity查询；实现报告须单列既有共享authority查询基线和audit增量，
不得用“共享查询”名义隐藏新源加载或无界循环preview/scoring。
保留共享Project授权上下文原有兼容边界，不宣称全仓所有字段已经统一有界；此处提取helper不得扩大该面。

输入/版本错误400/422，未授权403或scoped404，缺root404，预览/幂等冲突409，超量413/422，
active限额429，queue/私有root/background-provider不可用503；gap是明确记录，不用HTTP成功掩盖损坏或越权。
损坏InputBundle/manifest/file/指针/hash、不可建立scope、未知schema，一律fail-closed且无部分答案/文件。

### 输入类型的缺源前置条件

普通 ReviewBatch.plan 只保存 identity pin，不保存所有 snapshot/type。V1 新 capture 必须对每个根 candidate
通过匹配其 exact frozen pin 的 live snapshot 或已有完整 typed frozen source proof 证明 static Text/Choices。
有源却不支持的类型整包 `UNSUPPORTED_INPUT`；缺源且没有足够类型/身份 proof 时整包
`INPUT_SCOPE_UNPROVABLE`，不能凭当前项目XML推断旧源类型。证据完整的 retained receipt 可形成历史源记录及 live-origin gap。
同一规则应用于实际纳入的 lineage；无法证明的扩展不静默跳过。缺源后的成功下载由 T 前已冻结的包支持，
不承诺任何已删除、从未保存类型证据的 batch 都能在事后创建新包。

## 10. 阶段、CI和升级要求

本次在独立worktree实施模型/迁移/API/UI及独立CI调用，默认开关仍为关闭，策略须显式配置。
本地验收包含RED/GREEN、PG/SQLite负例、原生文件安全、双语原生浏览器和独立review；
后续push/PR、exact-head merge/main CI、部署/启用和Host业务验收分别授权。

现有main19push workflows/37jobs成功是历史基线，不是本提案执行。二审main单调用267.502秒/300秒，
新下载流程应有独立预算/清单设计，不能继续无度堆入二审单call或提高原限额。
复用现有closed inventory、producer/view/attempt绑定、warm/cold/真实partial、fail-closed聚合；
新增CI覆盖须守恒原测试、required名称/app、max-parallel3及原限额，新增预算单独说明。

升级时核对：snapshot/private-range helper签名和FD生命周期、root reservation所有访问面、Project/authority锁顺序、
原哈希与JCS/ECMAScript表示区别、两DB不可变/Project cleanup guards、后台identity provider契约和旧#79/#80行为。
保持Fork-only、#81/#84 OPEN、Docker/#76、主仓和其他worktree不动。

## 实施阶段的边界澄清

独立审查确认：旧 `static-text-choices/v1` receipt 不证明 Text 数量。新capture逐份冻结snapshot验证恰好一个Text；缺根时，旧receipt仍只证明原subject pins，另须同root assignment中已完整校验的冻结revision提供与原label_config_sha256完全相同的XML字节类型证明。私有type_proof绑定证据revision精确pins和config digest，纳入两pass与预算；不借其答案、状态、actor或时间补原根。无此精确类型证明则INPUT_SCOPE_UNPROVABLE，当前Project XML永不补证。

Local identity的history在SQL分页前检查活generation、principal/source、当前User/org/Manager、instance和完整auditpolicy，分页后仍作creator最终检查。旧签名游标不能含隐藏行的UUID或时间。非Local provider仅有resolve_background不足以证明collection完整范围，capabilities明确collection_supported=false，V1 UI流程不启用，collection直接503；其可信hook仍供已知对象和worker的逐对象gate使用。没有新增临时Host collection协议或静默空列表。

这里的内置Local只指精确`LocalIdentityProvider`类型。自定义子类属于custom provider，不能按`isinstance`跳过其后台撤权hook；无hook失败封闭，有hook仍不支持V1 collection。独立审查发现旧#79也有类似继承判断，这是既有域的架构风险；本次根治新audit域，未更改旧发布权限契约，不把本地audit验收称为旧域custom-provider整改。

所有计数、字节和SQL限制同时成立。1000根的紧凑真实输入已验收；2000修订等计数是硬上限，不能据此承诺任意该数量输入都满足512KiB snapshot union或其他更早的门禁。累计snapshot union按整个pass计算，不能按chunk重新获得预算；两pass元数据、scalar、ID、COUNT、锁查询均计入同一Budget。

已断开的nullable FK不按冻结数字ID/UUID水合当前对象。根batch已断链而同ID再出现时拒绝新capture；可验证的历史subject/target仅作为retained记录并标live-origin gap。活followup target仍须全部九pins一致。相邻差异采用已知修订序列，缺输入与序号缺口显式报告，不借当前答案补历史。

创建命令只有在确定处于提交前并回滚时才返回`X-Audit-Command-State: rejected`。已提交后的enqueue/响应异常保留UNKNOWN，显式重试只能恢复原body/key。固定错误码脱敏不改变这个提交阶段语义。

异常脱敏同时覆盖响应与共享日志器的活动异常。audit API重建固定码/状态/context并抑制原异常链，避免仅替换参数仍让`sys.exc_info()`记录provider原detail；认证401、框架403/404和未知500语义保留。后台仅在custom hook调用处把未知普通异常归一为固定audit错误，避免原detail逃到RQ日志；不扩大publish事务的DatabaseError捕获，也不改变不确定发布时保留文件/lease的规则。

新增review-audit独立closed call覆盖两语，300秒调用上限与15秒终止宽限；11 shard/26 calls中原25项语义摘要保持不变，max-parallel3、原required名及原二审预算不变。新call增加runner容量需求，远端warm/cold/partial和实际耗时尚未运行。本地浏览器使用有所有权证明的新建测试库与显式synthetic queue，只证明页面/API/冻结文件流程；不冒充真实Redis传输或Host业务接收验收。
