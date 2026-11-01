# 字段审核批次到仲裁的受控交接 V1 设计（#84）

本切片把已有字段冲突优先抽审与已有字段修订仲裁连接起来：经理从审核批次中一份已抽中的、仍待审核的精确冲突修订，显式派发字段仲裁，或关联完整语义输入相同的已有仲裁。独立受派审核员继续在现有字段仲裁工作台采纳一份完整冻结修订。审核状态、交接事实和仲裁状态分别呈现。

**阶段：仅设计。** 2026-11-01 Asia/Singapore，基线是 PR #128 实际 merge SHA `5757b5a5b65f1637923ad47aba29df915fa267cd`，tree `2edc490096f2ae5ec8c4375b42bbcb3a4ee62344`。独立 worktree 为 `ls84-field-handoff-design/annotation-engine-label-studio`，分支 `codex/issue84-field-handoff-design`。本轮没有功能代码、迁移、测试夹具或 workflow 实现；[验收矩阵](field-review-handoff-v1-acceptance.md) 全部是未来要求，不能记为已执行。

## 1. 范围与前置事实

- 复用 [NER 交接](ner-review-handoff-v1.md)、[字段冲突优先抽审](field-review-priority-v1.md)、[字段仲裁](field-arbitration-v1.md) 与 [冻结字段批次](field-batches-v1.md)。不引入另一套审核、仲裁决议、指标、工作流引擎或权限存储。
- fresh handoff 仅处理 `field-conflict-ranked/v1` / `review-field-priority/v1` 审核批次中 `selected=true`、`state=conflict`、`reason=frozen_conflict` 的一份来源；要求来源 pin、完整冻结 cohort 及计算依据匹配，当前仍是该 assignment 的最新 pending Submission，且没有 ReviewDecision。
- 原 cohort 的其他修订使用冻结来源，不替换为最新修订。原先 null 的来源保持 null；缺来源、全无值、样本不足或不可比较输入不能创建交接。有效分歧中的完整空值修订可以成为仲裁 winner。
- 格式差异沿用 `field-raw-difference-queue/v1`：raw 不同、normalized 一致率 100% 仍可为冲突。冲突描述整组输入，不认定某个参与者错误；不新造分数或阈值。
- 两个动作均须用户明确选择：`dispatch` 新建字段仲裁和关联；`link_existing` 关联同输入的 pending v1 或 resolved v2 字段仲裁。不能把派发失败自动转为关联。cancelled 目标不能新关联。
- 不增加批量交接、改派、字段拼接/改写、自动 winner、自动审核批准/驳回/返工、二次复核、低置信度策略、bbox、发布许可或 Host/Core 集成。仲裁完成不改变任何原 Annotation、Submission、ReviewDecision、抽审分母/排序或交付资格。
- #84 当前无 assignee/comment；现有 8 个 worktree 无本切片分支，最近聊天清单未发现另一字段交接 owner，GitHub CLI 只读开放 PR 清单为空。该核对是设计起点快照；实现前须重新核对 owner、main、开放 PR 和权限范围，不能从本设计继承发布授权。

## 2. 复用点及必须解决的适配边界

| 位置 | 已有能力及本切片的最小扩展 |
| --- | --- |
| `label_studio/review_batches/ner_handoff.py` / `ner_handoff_api.py` | 复用显式交接、父回执、typed provenance、元数据历史及 target-before-source 的结构；新增 field 专有模块和 schema，不把 NER scorer、权限或表替换成字段版本 |
| `label_studio/classification_disagreements/field_arbitration_api.py:create_task` | 已有事务内 `verified=(origin,evidence,prepared)` 接口，无需再次发明派发服务；在当前 project fence 内调用，保留独立派发 wire/hash/恢复语义 |
| `field_arbitration.py:evidence/check_engine/input_hash/checked` | 复用字段完整 origin、语义 hash、九项 pin、计算与决议验证；最终 `evidence(..., replay=prepared)` 只重载/检查，不作第三次计分 |
| `field_priority.py` / `review_batches/service.py` | 复用完整本地 plan 验证、候选可见性、整组 self-cohort 排除及当前读门禁；不调用会扫描所有候选答案的 review projection/planning |
| `FieldArbitrations.jsx` / `ClassificationArbitrations.jsx:FrozenArbitrationWorkspace` | 复用完整字段派发、详情、整份采纳、取消和返回流程；补充 typed field handoff adapter，保留字段 evidence-retention/generation fence |
| `ReviewArbitrationHandoff.jsx` / `ReviewBatches.jsx` | 复用完整交接历史、分页、入口和导航；把当前 NER/分类二选一改成显式分类/NER/field 注册，未知类型拒绝，禁止静默分类回退 |
| 数据库与依赖 | 沿用 Django/DRF、PostgreSQL/SQLite 唯一约束和 immutable guards、现有 JSON/SHA-256 规范化及锁定的 `@noble/hashes`；不加新依赖或商业 Enterprise 组件 |

两项现有适配限制需要在实现时同时解决：共享工作台按 `kind==='ner'` 才发送 `expected_viewer_hash`，并有 NER 专属 unavailable 遮罩分支；字段必须通过 typed adapter 声明/校验所需字段、源状态及遮罩。只把 `fieldArbitrationAdapter.handoff=null` 改成对象会漏掉这些约束。分类旧 body/schema/hash 保持不变。

`create_task(verified=...)` 当前仍会重读 FieldBatch 的 manifest；已有 `evidence` 也会读取完整 manifest。本切片增加窄的内部 bounded-origin loader/已锁批次参数，交接把本次预算管理器读取并验证的批次交给这些 helper，独立派发保留默认路径。载体绑定 project、organization、FieldBatch ID/hash、完整 origin 和 prepared 输入，只在当前事务/请求内有效。不能只因传了 `verified` 就漏记第二次 manifest 解码；helper 不接受客户端 proof，不持久缓存权限，也不成为公开 API。

## 3. 冻结关联与同输入判定

拟新增 `review_batches.FieldReviewArbitrationHandoff`，明确外键指向现有 `FieldArbitrationTask`。禁止 GenericForeignKey、凭整数 ID 猜类型或复用 NER/分类关联表。字段仲裁的 Task/Decision 表及历史审核 plans 不迁移、不回填。

记录 project、可空 review_batch / field_arbitration / created_by 外键，冻结 review_batch / Submission / target / actor ID，可信 creator_principal、creator_source、created_at、父 UUID、request_hash、origin、origin_hash。origin 不含字段值、原文、答案快照、媒体 URL、token 或 child UUID，固定以下证明：

| 证明 | 必须冻结的身份与依据 |
| --- | --- |
| 顶层命令 | `review-field-arbitration-handoff/v1`、`kind=field`、project/organization、principal/provider source/actor、mode、时间、父键和 typed request hash |
| review | ReviewBatch ID、plan_hash、`field-conflict-ranked/v1`、完整 candidate、selected/conflict/reason、calculation_hash、原九项 source_pin |
| analysis A | FieldBatch ID、完整 manifest_hash、source_schema、selection、metric_rule、explanation、engine、queue_rule、目标 task、calculation_hash、input_set_hash |
| target B | field 类型、target ID、自身 origin_hash/input_set_hash、自身 FieldBatch ID/manifest_hash、创建时间、冻结派发者/受派人、`status_at_link/version_at_link` |

source_pin 严格包含六项正整数 `submission_id/assignment_id/assignee_id/task_id/revision/submitter_id` 和三项摘要 `result_hash/config_hash/task_data_hash`；禁止 bool、float、字符串伪装 ID，整数须在 JS safe 范围内。既有六字段 review candidate 不改写；新证明从已验证的 field evidence 取九项 pin，并逐项对齐 candidate。不能照搬 NER 的六字段候选证明。

版本固定为 `field-batches/v1`、`active-assignment-latest/v1`、`field-lf-ascii-trim/v1`、`field-value-differences/v1`、`python-string-equality/v1`、`field-raw-difference-queue/v1`；仲裁沿用 `field-arbitrations/v1`、`field-arbitration-input/v1`、`adopt-exact-field-submission/v1`。未知或被篡改版本拒绝，包含重新计算了外层 hash 的伪造包络。

关联已有 B 时，完整验证审核来源 A 的 plan/Manifest 和 B 自己的 origin/Manifest；复用 `field_arbitration.input_hash(project,item)`，同时比较完整 item/cohort、所有九项 pin、规则、冻结结果/成本和计算 hash。仅 task ID、两份修订、一致率、冲突标签或 hash 字符串相等均不足。两个批次的 ID、名称和时间可不同，完整语义输入必须相同；相等后才允许共用一个目标 cohort 的答案加载和计算。B 损坏/删除不得用 A 替代，反向亦然。

唯一约束为 `(project,idempotency_key)` 与 `(project,review_batch_id_frozen,submission_id_frozen,arbitration_id_frozen)`，来源索引用冻结 batch/Submission。PostgreSQL/SQLite UPDATE guards 冻结 PK、scope、全部冻结 ID、principal/source、时间、proof/hash、键与请求 hash。便捷 FK 仅允许 non-null→NULL，不允许 NULL→non-null 或非空改绑；不能凭冻结 ID 查回重建对象。NULL 本身不证明发生过删除。项目删除保持现有 cascade 语义，不承诺项目外永久审计。

## 4. 服务端权限与开关

新增 `ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=false`。同时交集 `field_priority.read_guard` 和 `field_arbitration.guard`：review batches、field priority/disagreements/batches/arbitration 开关均开启，review/field-analysis/field-arbitration 的 `organization:project` 映射均严格等于 `fork_review`。Core-only、未知 scope/mode、停用账号/组织成员、项目删除、不支持的数据库均拒绝。没有独立 handoff mode store。

| 行为 | 当前服务端授权与数据范围 |
| --- | --- |
| context | 当前 Manager + READ_WORK，完整交集门禁；返回当前 `can_handoff`，没有组织仲裁权限时不得写 |
| dispatch / link_existing / 原键恢复 | 当前 Manager + READ_WORK + ORGANIZE_FIELD_ARBITRATION，身份/principal/provider/organization 均重新验证 |
| Manager 历史 | 当前 Manager + READ_WORK 和全部门禁；允许读保留的 unavailable 关联元数据 |
| Reviewer 历史 | 当前 READ_WORK；可见 review candidate、整个冻结 self-cohort 排除、自己当前独立受派 FieldArbitration 范围的交集 |
| 历史 provenance 核对 | 与历史 detail 相同的当前读范围；独立验证 A/B 冻结关联，不要求来源仍 pending/latest，不要求组织仲裁写权限 |
| 采纳 / 取消 | 继续使用原字段端点；assigned independent Reviewer + ARBITRATE_FIELD 采纳，原 Manager SECURITY_CANCEL 通道取消 |

交接写入不额外要求 ORGANIZE_REVIEW、ORGANIZE_FIELD、REVIEW 或 ARBITRATE_FIELD；这些能力也不能替代 ORGANIZE_FIELD_ARBITRATION。组织抽审和组织分析的创建权与使用既有冻结来源的读权分离。Manager/全局管理员身份不赋予采纳权限，分类/NER operation 不赋予 field 权限。

所有列表先在 SQL/有界 plan 元数据层建立 scope，再 count、分页和 proof/target JSON 加载。Reviewer 先排除自己整个冻结 cohort 和自己其他候选，再交集当前独立受派目标，缺 review/target FK 的行不展示；直接 detail 与列表相同。Annotator 无新接口权限，不能通过改 ID、旧页面、直接 API 或 actor 字段绕过。

新受派人沿用现有 eligible_assignees：同组织、有效当前 Reviewer 项目成员，排除整组 assignee/submitter、项目创建者及派发经理。链接不改派、不恢复已撤销采纳权；受派人失效可保留 Manager 关联事实，其实际 `can_decide` 仍由原端点决定。原安全取消在 READ_WORK 被撤回后仍按既有已知 ID 契约工作；交接不会增加取消搜索/证据旁路，也不能用 SECURITY_CANCEL 绕过交接 READ_WORK。

## 5. API、错误与回执

路由前缀 `/api/projects/{P}/review-batches/{B}/`；仅新增 field 命名，不改变分类/NER 路由。

| 方法 / 路径 | 行为 |
| --- | --- |
| GET `sources/{S}/field-arbitration-context/` | Manager 的当前资格、冻结期望、现有目标及明确可用动作；成功取证不输出字段答案 |
| GET `sources/{S}/field-arbitration-handoffs/` | 当前可见的分页历史，默认 50，最多 100 |
| POST `sources/{S}/field-arbitration-handoffs/` | 显式 dispatch 或 link_existing |
| GET `field-arbitration-handoffs/{H}/` | 不可变关联及授权的当前目标元数据 |
| GET `field-arbitration-handoffs/{H}/provenance/` | 历史打开专用的完整 A/B 冻结关联核对，仅元数据，不加载快照或计分 |

POST 要求 UUID `Idempotency-Key`、严格 JSON 对象、精确字段集合：共有 `mode/expected_plan_hash/expected_input_set_hash/expected_viewer_hash`；dispatch 加 `assignee_id`；link_existing 加 `field_arbitration_id/expected_arbitration_version`。拒绝多余或重复/未知 query、actor/reviewer/provider/role/答案/winner/状态字段。命令 body 原始字节和 canonical JSON 都限 16 KiB，ID/hash 严格验证。

typed request hash 覆盖新 schema、`action=handoff`、路由 project/batch/Submission 和完整 canonical body，包括 expected_viewer_hash。父键的唯一范围是 field handoff 表，不借用独立仲裁键。fresh 请求必须匹配 field arbitration viewer hash；field batch 的不同 viewer salt 不得混用。

已授权且在完整元数据预检时即可判定不合格的 context 返回 `eligible=false/reason/can_handoff=false`，此分支零 snapshot/零计分；包括错误批次类型、unselected/agreement/unknown、no_values/样本不足及当前不再 pending/latest。取证开始后才发现 source unavailable 则返回明确 409，不转成 200 eligible=false；超预算仍是 422，失败前真实部分工作由 instrumentation/失败记录保留。合格 context 才走两遍完整取证。越 scope 对象 404，不提供存在性探测。非法格式/键 400；当前 policy 拒绝 403、隐藏资源/关闭总开关 404，遵循已有门禁约定；不支持数据库 503；source/plan/input/CAS/key/重复关联冲突 409；预算超限 422。新领域错误使用 `REVIEW_FIELD_HANDOFF_*`，复用 field 错误映射明确保留 HTTP 含义。损坏 proof 不转空列表，不把超预算转 unavailable 或 unknown。

fresh 成功 201、合法原键恢复 200。ACK 绑定新 schema/kind、project/batch/Submission、handoff/target ID、原 actor、当前 viewer、plan/input hash、mode、assignee、父 UUID、request_hash、origin_hash、关联时 target status/version、`replayed` 和实际 processing_cost。客户端验证字段之间的一致性、原 intent 和请求 hash，不只验证外形；使用现有 canonical JSON 和 `@noble/hashes`，支持没有 SubtleCrypto 的合法 HTTP 测试 origin。

历史 list/detail 不加载答案快照、不计分，`source_integrity=not_checked`、`decision=null`，不返回 winner、理由、字段矩阵或完整 origin。目标元数据校验仍须有界验证 resolved decision 的 winner/reason，虽然不向新接口输出这些字段。`available` 只表示关联 FK/当前目标元数据可用，不表示源证据通过复验；当前 source_status 只在任务/assignment/project/assignee/revision/submitter/result_hash 的元数据 pin 匹配后展示，其余为 unavailable，config/task_data 未加载仍是 not_checked。界面使用“关联可读/来源未复验”，不能把它画成已验证证据。

历史打开不调用 fresh context。新的 provenance GET 按当前历史 scope，有界加载并验证 H 自身 proof、完整原 ReviewBatch plan、A/B 各自完整 Manifest 和 target origin/participant/cancellation/必要 decision 元数据，逐项核对 plan/hash、九项 pin、全部规则、完整同语义 item 及 H 的两侧 provenance。它没有 pending/latest/无 ReviewDecision 条件：交接后的正常审核或新 revision 不使原冻结历史失效。source/target 链接或原 Manifest 丢失返回 `provenance_status=unavailable`，不借另一侧替代；损坏 H/target 台账 409、预算 422、撤权 403/404 不伪装成 unavailable。

该 GET 返回最小 `provenance_status/reason`、project/H/target ID、当前 viewer、`input_set_hash/task_evidence_hash/field_batch_id/task_id`、target 创建时间/受派人和当前 status/version。`task_evidence_hash` 复用原 `fields.digest({schema:'field-arbitrations/v1',item})` 算法；H 自身 proof、目标 origin_hash 和 A/B 完整 Manifest/hash 的一致性由服务器独立验证，不要求客户端从旧 B DTO 读取这些字段。所有 Reviewer 新历史/provenance 响应均不新增完整 A/B Manifest hash、target origin_hash、cohort、principal/provider 或冻结答案信息；Manager 的 fresh ACK 继续使用本设计的完整命令摘要。`snapshot_integrity=not_checked` 明确 provenance 只核对元数据，不能单独证明答案。它两遍读取/比较当前元数据，零 snapshot/零计分，单轮及整请求预算见 §7。

从交接打开现有 FieldArbitrations 时，先由原 B 详情按原授权/预算复验共同 cohort 的九项 pin、快照和字段证据，暂存结果但不写入可见状态/retention cache；在 B 的最后一次计算及 hydration 完成后，再请求 provenance GET。原 B 请求的 project/H/target 以当前 intent 闭包和 generation 绑定，provenance 响应须匹配这些身份；不要求旧 B DTO 新增 project/H 字段。两份响应共同已有的 viewer、target ID、`input_set_hash/task_evidence_hash/field_batch_id/task_id/created_at/assignee_id/status/version` 必须相符，B 证据 verified、provenance verified 后才写入缓存并显示完整 winner/理由。不要求从 B 详情读取未提供的 origin_hash 或 Reviewer 不可见的 origin_manifest_hash。任一不可用、身份/版本变化或读取失败都遮罩 comparison/inputs/decision/winner/reason 与行动，保留明确的读取失败/未复验状态，显式刷新才重试。这样 A 在 B 计分期间丢失也会被最后的来源核对捕获；最后核对之后发生的变化仍按既有撤权/blur/刷新与后续命令的当前授权处理，不承诺跨请求永久一致。

原独立 B 详情继续验证自己的来源 B；它不因某条交接来源 A 丢失而改变原授权语义。交接页面的联合显示条件由上述专用流程保证。历史的 `status/version_at_link` 永远不由 live 状态覆盖；原键恢复也只证明原提交事实。provenance 读取不会触发任何 POST 或计算。

## 6. 原子性、恢复及锁顺序

fresh dispatch 在一个事务内创建原有 FieldArbitrationTask 和新关联；给子任务分配独立服务端私有 child UUID，用原字段 create 命令 hash。父/子回执不互借、不暴露 child key。link 只创建关联，不修改目标/受派人/决议。响应渲染、预算检查和最终复核都在事务内，失败二者一起回滚，父键可复用，无孤儿目标。

恢复顺序固定：当前交集门禁和写权限 → 本地父键查找 → 同 principal/provider/actor、完整 request hash 和路由身份 → 固定 V1 proof/hash/组织/输出包络 → 原回执。恢复不读取 ReviewBatch/FieldBatch/目标实时 JSON、答案快照，不作新 assignee/pending/latest/CAS 检查或计分。新修订、源/目标删除、目标终态变化、当前处理上限收紧均不破坏合法原回执；当前撤权、organization 改变、mode/feature 关闭仍拒绝。

fresh viewer 校验位于原回执 owner/body 验证之后。旧 viewer 期望可留在同 owner 的原命令中恢复，当前响应绑定当前 viewer；前端身份切换仍须清空旧 intent。相同键不同主体/provider/body/route 返回冲突，不泄漏原记录；不同键同一关联冲突。取消后的旧键只恢复原关联，绝不重新派发；重新派发必须是新的显式命令。

写锁序：Project → 现有身份/OrganizationMember/User/ProjectMember fence → 已有 FieldArbitrationTask（仅 link）→ 按 ID 的完整来源 FieldBatch A/B → 有序 Task → TaskAssignment → Submission。用户/成员锁沿用 `lock_project_operation` 的现有实际次序，dispatch 将新 assignee 放入 related_user_ids。新目标在来源锁后插入；不另引入逆序 ReviewBatch 锁，后者通过 immutable guard 和最终重读保护。行锁查询仅取 ID/必要元数据，禁止普通 ORM 获取或 refresh 提前加载快照。

正常审核、新修订、成员/组织策略变更、独立字段仲裁写继续使用项目 fence。组织用户禁用等不完全依赖项目锁的现有写路径，依靠相关身份锁与最终重验；独立连接测试验证真实等待次序，不能要求被锁住的 writer 同时先完成。数据库 active-input uniqueness 和新关联约束是最后防线，不替代授权/锁。

| 并发已提交次序 | 预期结果 |
| --- | --- |
| 同父键同命令 | 一次 201；另一方 200 恢复同一关联/目标 |
| 异父键同语义 dispatch；standalone 与 handoff 互抢 | 最多一个 active target；另一方 409，不自动 link，不借独立回执 |
| decision/cancel 先于 link | 旧 expected_version 409；新明确命令可关联当前 resolved v2，cancelled 始终拒绝 |
| link 先于 decision/cancel | 保存当时 status/version；目标按原 CAS 继续转换，关联历史不重写 |
| 普通审核/新 revision 先于 fresh handoff | 拒绝原来源，不换最新源；handoff 先提交则不阻断后续普通审核，不生成 ReviewDecision |
| 撤权/组织迁移先于等待中的 fresh 或恢复 | 拒绝；不得返回已准备答案或旧主体回执 |
| 任一步最终 source/身份/target/输出检查变化 | 新 target 与关联全部回滚，无假成功回执 |

合格 context 和 fresh POST 的最终核验固定为：初次取证/第一遍计算 → 构造并渲染实际响应 → 同一 prepared 输入第二遍计算 → 重读有界 metadata 并调用 `evidence(...,replay=prepared)` 最终 hydration/完整来源检查 → 当前 pending/latest、target provenance/CAS、身份/organization/policy/representation 重验 → 提交/返回。context 不写入；预检不合格分支只两遍元数据表示核验，取证中途失败则保留实际部分成本。历史 list/detail 及 provenance GET 都是两遍元数据表示核验，无 snapshot 或计分；provenance 比普通历史多验证 A/B 完整来源，不检查 fresh pending/latest。不能直接复用 NER 无 replay 参数的 source 回调导致第三次计算。

## 7. 预算与真实成本

完整 ReviewBatch plan 和每份 FieldBatch Manifest 必须先在存储字节 guard 下加载、完整验证，再投影单个目标 task。所有对象默认 defer 答案/大 JSON，SQL 用实际 PostgreSQL OCTET_LENGTH / SQLite BLOB 字节长度，在 hydrating SELECT 的 CASE 内同时限制单字段、行总量、页总量和剩余额度。单独的预检 SELECT 不能防住随后增长；禁止 count/page/目标筛选之前加载隐藏数据。

| 范围 | V1 限额及计费 |
| --- | --- |
| Review plan | 1000 candidates；canonical 1 MiB / SQL JSON 4 MiB；六字段 candidate 及 field proof 完整校验，不加载全体候选答案 |
| 每份 FieldBatch | 100 tasks、20 participants/task、2000 cohort entries；完整 canonical Manifest 1 MiB / SQL JSON 4 MiB，A/B 不同则分别验证 |
| 每次 materialization 累计元数据 | canonical 4 MiB / SQL JSON 16 MiB，包括 plan、至多两份 Manifest、关联 proof、目标 origin/participant_ids/cancellation、resolved winner+reason；相同 table/PK/field 只在同一遍去重 |
| 两遍元数据物理读取 | 最多 canonical 8 MiB / SQL JSON 32 MiB；最终复核使用新的预算上下文和真实 SELECT，不用初次 cache 证明当前源。额外 helper reload 也须记账或通过同遍有界 loader 去重 |
| 目标 cohort | 最多 20 个完整冻结来源，A/B 不同输入直接拒绝；每 hydration SQL snapshots 64 MiB / canonical snapshots 8 MiB，两次合计 128 MiB / 16 MiB |
| 字段处理 | 最多 20 fields、4 KiB/raw value；每 revision raw values/text/XML 各 64 KiB；单遍最多 400 normalizations / 7600 equalities，CPU preflight 在第一遍计算前 |
| 命令 / proof / 输出 | 原始及 canonical body 16 KiB；新 origin canonical 64 KiB / SQL JSON 256 KiB；目标其他字段沿用既有固定 V1 上限；实际响应 1 MiB |
| 历史页 | 默认 50 / max 100；先预留整页全部 proofs，再加载各 target 元数据及其 decision；整个 materialization 仍共用 4/16 MiB 和响应 1 MiB，超限 422，由用户显式减小 page_size，不静默截断 |
| 历史 provenance GET | H、完整 review plan、A/B Manifest、目标/decision 共用每遍 4/16 MiB、两遍总 8/32 MiB，0 snapshot/0计算；固定 V1 检查与当前读权限不因历史 pending 状态改变 |

令 I 为目标有效完整输入数、F 为实际参与比较的静态字段数；正常可比较样本每遍实际循环 `N=I*F` 次 normalize、`E=2*C(I,2)*F` 次 equality（raw 和 normalized 各一次）。完整双空字段仍计真实循环，只是不进入指标分母。缺失/不可比较的提前返回不冒称已经完成正常计分。整个目标 cohort 的成本不随 pair 展示、分页、review 筛选或抽审比例下降。

| 例子 | 单遍 N/E | 一次成功 context 或 fresh POST 的两遍总计 |
| --- | --- | --- |
| I=2，F=1，含仅格式差异 | 2 / 2 | 4 / 4 |
| I=3，F=2 | 6 / 12 | 12 / 24 |
| I=20，F=20 | 400 / 7600 | 800 / 15200 |

每次合格且成功取证的 `eligible=true` context、fresh dispatch、fresh link 恰好两次 hydration 和两次计算，外部 `scorer_calls=0`；正常成功复用 `field_arbitration.processing` 的量纲/两遍语义。200 eligible=false 的预检分支为零 hydration/calculation、零 per_pass/total，不套用“成功就两遍”的 ACK 规则；取证中途 409/422 的失败记录保留真实 0/1/2 遍工作，不伪报零或完整两遍。预览再提交是两个请求，最高 1600 normalizations / 30400 equalities；显式打开原仲裁详情另计。

交接历史打开组合为原 B 详情请求（最多 800 normalizations / 15200 equalities、两次 snapshot hydration、原详情预算）加最后的 provenance 请求（额外两轮、最多 8/32 MiB metadata、零 snapshot/计算），不能把后者漏计或说成单一原详情请求。普通 history、capability、候选人、原键恢复也没有 snapshot/计分，不代表零 SQL/JSON。

同输入 link 复用一份 prepared cohort，不嵌套两个完整详情 API，不计算 review 全候选或整个 FieldBatch。计算依赖的响应大小在实际渲染后检查；真实 instrumentation 保留失败前已发生的工作，不把超限失败说成零成本。不能通过少算一遍、截短 cohort/Manifest、减分母或伪造 zero-cost ACK 达标。固定历史 V1 存储包络与可收紧的当前 fresh 处理上限分开，原键恢复仅使用自身本地包络及当前输出限额。

## 8. 页面与异步状态

沿用既有审核批次完整入口 → 交接历史 → 明确派发/关联 → 原字段仲裁工作台 → 返回原批次/页/筛选/焦点。`ReviewBatches` 为 field 增加独立 `can_read_field_handoffs` 能力提示，服务端仍逐请求校验；GET 能力只看当前门禁/权限，不计分。普通/分类/NER 批次和入口不受字段开关影响。

字段 flow 显式声明 field routes/schema、field link 参数、field capability/viewer、历史 provenance reader 和 field ACK/cost validator，适配缺失或 kind/schema 不符即拒绝。共享工作台把 viewer 期望、source-unavailable 遮罩及“详情后最终 provenance 通过才接受”的 hook 变为 typed adapter 能力，保留分类/NER原契约；FieldArbitrations 的 `retainDetail` 冻结输入/来源 hash 及 generation 接受后才写缓存的规则继续有效。初次 B 详情成功而 provenance 失败不能污染缓存，后续同 viewer 的合法读取仍应可接受。

经理 context 展示冻结来源 revision、冲突原因、现有同输入目标与当前可用动作；字段答案只由已有字段证据/仲裁详情提供。独立审核员从既有受派列表进入；Manager 显式关联不赋予自己采纳权。页面分别标记“来源审核状态”“交接已保存”“仲裁状态”，resolved 不显示 Approved/已返工/可交付。

所有 read/intent 绑定 project/batch/Submission/plan/input/viewer/kind/mode/target/version；成对 capability/context/history 不同 viewer 或 scope 不能组合。blur、撤权、identity/provider/组织切换清除敏感证据和旧意图；迟到 Promise、旧页/旧目标响应不得覆盖新上下文。返回只保存导航，不保存答案或权限，重新 GET。

提交中防双击。网络超时或 ACK 缺 key/hash、错误 actor/viewer/schema/provenance/status/version/cost 保留原 body/父 UUID 为 UNKNOWN，只允许显式原命令恢复；刷新 GET 不 POST，不自动换键/模式/目标。恢复响应的 current viewer 可更新，但必须绑定同一已验证 owner 和原请求 hash。同 viewer 后续 GET 失败不撤销已确认保存事实，identity 改变则丢弃旧 intent，禁止跨账号恢复。

## 9. 兼容、实施顺序和验收出口

未来实现只增加 field typed ledger/迁移/路由/flag/adapter 与必要窄 helper；不编辑既有迁移，不重写旧 plans/Manifest/hash/回执，不扩大既有答案/文件/媒体/导出/批量 API 权限。关闭字段交接仅隐藏/拒绝字段新接口，原独立字段仲裁和分类/NER 交接保持自身开关语义。保留 #128 SQL NULL 修复：缺失 legacy JSON keys 的普通/分类批次仍可见；任一 algorithm/schema/kind 阳性 marker 的损坏字段 plan 不能漏过保护。

实施顺序建议为：typed proof/command 和有界元数据 loader → 字段父台账/PG与SQLite guards → 原子 dispatch/link/恢复与最终 replay → typed 共享流程及双语适配 → 独立连接并发、预算和所有既有回归 → 隔离双语浏览器。实施前需用户另行确认；本设计批准不等于推送、PR、合并、部署、运行开关启用或 Issue 关闭授权。

验收按[矩阵](field-review-handoff-v1-acceptance.md)逐项保留真实夹具、before/after、日志/SQL与调用计数、run/job/attempt/SHA及两语截图。不得以测试定义、模型存在、workflow YAML、PR virtual merge CI 或上个切片的 main CI 充当新实现证据。后续新 workflow 使用独立 disposable PostgreSQL/合成数据，避免把新增全链路叠进长 Enterprise job；是否发布或执行托管 CI 受当轮授权约束。

本轮仅文档审查、链接/范围/算式检查。没有运行功能、PostgreSQL、SQLite、浏览器或远端 CI；全部维持 NOT_RUN。Fork-only，#81/#84 OPEN，Docker/#76 与其他 worktree/services/data/session 不操作。保留 `OLD_AND_NEW_CHANGED`、`CHANGED_DATABASE_CONTAINER_REPLACED`、cause `UNKNOWN`、`NOT_RUN_AFTER_DOCKER_INTERRUPTION`、未完成全库比较、既有保护 worktree 缺失记录和共享 Biome 基线；设计或隔离 CI 不证明保留运行时/数据库一致性。
