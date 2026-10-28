# NER 冻结修订仲裁：首版设计（#81）

状态：**设计已确认，本地实现与验收完成，2026-10-28**。实测结果见[实现验收记录](ner-arbitration-v1.md)；未推送或创建 PR。
基线：main / PR #120 实际 merge SHA `3001f8f3618100ec737c33d713c78f9d02a25407`。已完成的 NER 对照、队列、概览继续使用原契约。

## 1. 产品目标与最小范围

补齐「NER 冻结分歧 → 经理派发 → 独立审核员阅读完整冻结依据 → 采纳一份既有修订 → 查看裁决/取消记录」。
输入只来自一个持久 `NerBatch` 的一个原始 `disagreement` 任务；不从临时对照回执、live Annotation、当前任务正文、模型预测或最新修订临时生成依据。

- 经理明确选择一名当前独立 Reviewer；仅该 Reviewer 可提交裁决。
- 只允许整份采纳原 cohort 中一份完整 Submission 修订，并填写理由。空实体修订若属于一个有效分歧 cohort，仍可被整份采纳。
- 任务原本是 agreement、unavailable、缺提交/最新修订不可用，或者所有配对均双空，不可派发；不把这些状态变成零分分歧。
- 首版只有派发、读取、采纳、取消；没有改派、批量、手工编辑/拼接实体、自动裁决、自动返工、二次审核、NER 抽审优先或审核转仲裁交接。
- 新记录是 Fork 本地仲裁依据，不是审核批准、黄金答案、正确率或发布资格。
- 不改原 Submission、Annotation、ReviewDecision、NER 指标/概览、NerBatch Manifest、修订选择、抽样/交付策略或 Host。#81、#84 保持打开。

## 2. 必须隔离的现有边界与复用

现有 `arbitration.py` 绑定 ClassificationBatch、分类 controls 和 `classification-arbitrations/v1`；
现有 LegacyProjectOperationProvider 也只在分类仲裁开关开启时赋予仲裁 operation。直接把 NER ID 接进该服务会混淆类型、权限和冻结哈希，不能作为复用方式。

| 范围 | 首版做法 |
| --- | --- |
| 生命周期与事务 | 复用已有 project-first fence、CAS、UUID 原键恢复、不可变账本及 JSON 实际渲染校验模式；只抽取确实共用的小 helper，不建立通用仲裁框架 |
| NER 依据 | 复用 ner_batches.checked、NER SQL 字节保护、ner.prepare/public/check_engine、valid_source 与 pin 验证；新增仅针对目标 task 的证据适配器 |
| 存储/命名空间 | 新 NerArbitrationTask / NerArbitrationDecision；不迁移、改写或放宽原分类表、触发器、API 或 schema |
| 页面完整流程 | 复用现有仲裁列表 → 派发 → 详情 → 确认 → 回执/恢复流程；用明确的 NER adapter 承接 transport、Zod schema、证据渲染和文案 |
| 展示 | 复用纯展示 NerResult、原矩阵/实体 span 对照、Button、现有布局/locale/焦点及未知结果恢复控件 |
| 不允许的复用 | 不把分类组件按原样挂载而触发分类 API、handoff 或别的写能力；不调用旧 NER 对照端点作为新仲裁的权限回退 |
| 退休 | 首版没有需要删除的旧入口；所有既有分类和 NER 流程保留 |

继续使用标准库摘要/JSON、DRF 验证、Django ORM 与固定 nervaluate 0.2.0；不新增计分引擎或替代开源依赖。

## 3. 独立开关、操作权限与 Core 边界

拟新增：
- `ANNOTATION_NER_ARBITRATION_ENABLED=false`；
- `ANNOTATION_NER_ARBITRATION_MODES={}`，仅精确 `organization_id:project_id -> fork_review` 放行；
- `ORGANIZE_NER_ARBITRATION=organize_ner_arbitration` 与 `ARBITRATE_NER=arbitrate_ner`。

所有端点还要求既有 NER 对照和 NER batch 开关，以及既有 NER mode map 的同一精确 scope；概览开关不是仲裁门禁。
Core-only-review、未知/缺失 map、停用账号/组织成员、无可信 scope、关闭任一必需开关均拒绝；不增加 Host/Core 调用、Token 类型或第三方凭据。

Legacy provider 仅由新 NER 仲裁开关授予两个新 operation；不能借分类 ORGANIZE_ARBITRATION/ARBITRATE 或 ORGANIZE_NER 推导授权。
自定义 provider 必须显式允许新 operation；已有分类许可完全不变。

| 动作 | 当前权限 |
| --- | --- |
| capability | 当前项目 Manager/Reviewer；返回当前布尔能力与 viewer_hash，无答案 |
| 列表/详情 | READ_WORK；Manager 可读项目内账本，Reviewer 只读当前分配给自己且非冻结参与人的记录 |
| 候选 Reviewer / 新派发 | Manager + READ_WORK + ORGANIZE_NER_ARBITRATION；不要求 ORGANIZE_NER，也不创建分析 |
| 新采纳 / 采纳原键恢复 | 当前被分配的独立 Reviewer + READ_WORK + ARBITRATE_NER |
| 安全取消 / 取消原键恢复 | 当前 Manager + 所有 feature/mode/scope 门禁；使用 SECURITY_CANCEL fence，允许 READ_WORK/组织仲裁操作被策略撤回后阻止待决任务继续执行 |

安全取消仅改变一个已知、项目内、pending 的本地仲裁任务，不能读取答案、采纳、恢复其他人的回执或发现不可读列表；
不绕过账号/组织/角色撤销或 Core/未知模式。READ_WORK 关闭时，首版仅保留已知 ID/hash 的直接取消 API；常规 UI 清空不可读数据，不增加紧急搜索或旁路读取端点。

Reviewer 必须是同组织、当前有效项目成员，当前角色确为 Reviewer，且不属于冻结 assignment owners / submitters；
排除项目创建者与派发经理本人。Manager/组织管理员身份不自动赋予 Reviewer 采纳权。
过滤先于 SQL count/分页及任何源加载；源或用户删除不能扩大可见范围。身份只来自现有 resolve_actor，禁止请求携带 actor、principal、source、reviewer、角色或 approval 字段。

## 4. 账本、冻结身份与哈希

### 4.1 两张独立表

`NerArbitrationTask` 保存：
- project、可空 created_by / assignee FK，可信 creator_principal / creator_source、actor_id_frozen / assignee_id_frozen、created_at；
- ner_batch_id_frozen、task_id_frozen、participant_ids（冻结 assignee/submitter 并集）；
- origin JSON / origin_hash、input_set_hash、创建 UUID / request_hash；
- status、version、可空 cancel_key / cancel_request_hash / cancellation 回执。

`NerArbitrationDecision` 保存：
- 同项目、OneToOne arbitration、可空 actor FK 与冻结 actor_id、可信 principal/source、created_at；
- input_set_hash、`adopt-exact-ner-submission/v1`、winner 的完整冻结 source pin、trim 后 reason、UUID / request_hash / payload_hash。

源 batch/task 只保存冻结 ID，不使用会使来源删除级联清除账本的 FK。project 删除继续遵循既有本地 cascade；这不是永久审计保留服务。
业务入口不提供用户 FK 清空或改绑能力；冻结 ID 和身份不变。数据库触发器拒绝非空改绑，但兼容 Django `SET_NULL` 的清空：
Django collector 在删除用户前先执行置空，数据库无法把这一步与普通置空区分。因此 FK 是便利链接，不能作为授权或回执主体；
权限始终用冻结 ID 与当前服务端身份/成员资格判定。不能把此约束描述成数据库已证明“仅因删除才置空”。

后续独立迁移新增两表、唯一/partial/check 约束及 PostgreSQL/SQLite immutable-update guards；
不改历史 0004/0005/0006 迁移或分类记录。guard 保护 PK、scope、完整冻结字段、回执和终态；
pending→resolved 必须有匹配 decision，decision 项目/输入集必须匹配任务。终态不能再改写，普通 API 无 DELETE。
直接 SQL、nullable FK 清空及 project cascade 行为需要分别验收。

### 4.2 origin

`schema=ner-arbitrations/v1`；origin 固定：
- source_kind=ner_batch、source_schema=ner-batches/v1、组织/项目、ner_batch ID、完整原 manifest_hash；
- 原批次枚举/计算时间、原 task item 全部 cohort、selection_reason、outcome/counts/calculation_hash；
- 原 selection、strict / explanation / engine 版本及决议规则。

完整 source pin 沿用 submission_id、assignment_id、assignee_id、task_id、revision、submitter_id、result_hash、config_hash、task_data_hash。
不复制原文本、实体答案、可变 Annotation 或远程地址，不重新选择 latest。
新修订、正常审核状态变化或 assignment 取消按现有 NER 历史来源规则处理；不能自动换源、清空原参与人或赋予编辑/审核权。

### 4.3 两类哈希

- origin_hash：覆盖完整原始 provenance，含原 batch ID/hash/时间。
- input_set_hash：域隔离 `ner-arbitration-input/v1`，覆盖组织/项目/task、NER source schema、selection/strict/explanation/engine、完整有序 cohort 与 calculation_hash；
  排除 batch ID/name/time、派发者、受派人和裁决结果。

因此，同一个输入集合来自不同名字/时间的 NER 批次时仍只有一个 pending/resolved 仲裁；
取消后使用新 UUID 可明确重新派发，新修订必须先形成新的冻结分析。旧原键不等于重新派发。
DB partial unique 为 project + task_id_frozen + input_set_hash（pending/resolved）；同键并发不依赖 UI 防重。
分类与 NER 具有不同表、schema、输入域、decision rule 和 operation，不复用回执。

Manager 可得自身授权的原 batch hash；Reviewer 只得目标 task 的 source pins、input_set_hash 与 task_evidence_hash，
不能得到其他任务的 cohort、全批次 manifest/hash 或 creator 的完整 origin JSON。
task_evidence_hash 不含全批次 hash；服务端仍核验完整 origin。

## 5. 状态与唯一性

| 当前状态/version | 新动作 | 成功状态/version |
| --- | --- | --- |
| 无任务 | 显式派发 | pending / 1 |
| pending / 1 | 指派 Reviewer 整份采纳 + 理由 | resolved / 2 |
| pending / 1 | Manager 安全取消 + 理由 | cancelled / 2 |
| resolved / 2 或 cancelled / 2 | 任意新采纳/新取消 | 409 TERMINAL；原键恢复例外 |
| cancelled / 2 | 新派发 UUID | 新记录 pending / 1；旧记录不变 |

resolved 不允许取消/重写；同一语义输入不能用另一批次或另一 UUID 再做一份权威决议。
同版本的并发采纳/取消只有一个胜者；不同 winner、不同 key 的第二个命令不能覆盖结果。

## 6. API 与严格请求

Base：`/api/projects/{P}/ner-arbitrations/`。不扩展分类路由；新请求使用 `ner_batch_id`，不接受旧 `batch_id` 字段。
即使两种 batch 的整数 ID 相同，也必须查 NerBatch 并验证独立 schema、scope 与预期哈希。

| 方法 / 路径 | 参数或 body | 结果 |
| --- | --- | --- |
| GET capability/ | 无参数 | schema/rule、viewer_hash、当前能力、预算 |
| GET assignees/ | ner_batch_id、task_id、expected_manifest_hash、page/page_size | 当前合格 Reviewer；只读 frozen metadata，不加载源/计分 |
| GET collection | state=pending/resolved/cancelled/all，page/page_size | 先过滤权限的元数据列表，不返回答案 |
| POST collection | ner_batch_id、task_id、expected_manifest_hash、assignee_id | 新派发 201；原创建回执恢复 200 |
| GET {A}/ | 无参数，或成对 left/right | 元数据 + 当前核验的冻结 NER 依据，或明确 unavailable |
| POST {A}/decision/ | expected_version、expected_input_set_hash、winner_submission_id、reason | 新采纳 201；原回执恢复 200 |
| POST {A}/cancel/ | expected_version、expected_input_set_hash、reason | 新取消 201；原回执恢复 200 |

所有 POST 要求 UUID Idempotency-Key。body 严格字段集；重复/未知 query、单边 pair、非 cohort pair、额外身份/答案/批准字段拒绝。
ID 为正安全整数，拒绝 bool/float/coercion；query ID 用 ASCII 数字解析。page 从 1 开始，默认每页 50、上限 100。
reason 使用 DRF 原有 NUL/孤立代理字符及长度验证；trim 后 1..2000 字符，展示存 trim 值，request_hash 固定合法原始 body，不能静默归一化原键意图。

写回执包含 schema、action、项目/资源、principal/source、actor、UUID、request_hash、
status_at_commit/version_at_commit、server time、payload_hash；新派发还明确返回 id、input_set_hash、原 ner_batch_id。
创建恢复后任务可能已取消/解决：原 receipt 仍为 pending/1，current_status/current_version 单独返回，不能复活旧状态。
决议/取消恢复返回原提交时的终态回执。当前状态、原提交事实和当前 evidence 可用性分开表示。

无权/跨 scope/关闭开关/Core 模式沿用 403/404；格式 400；CAS/原键冲突/终态/冻结依据变化 409；
预算 422；后端或 nervaluate 契约失败 503。错误码使用 NER_ARBITRATION_ 命名空间。
详情源不可核验用 200 + source_status=unavailable + reason=source_unavailable，comparison=null，原文/实体不返回，不伪造零分/100%。
所有响应 private/no-store、no-referrer；实际 JSON bytes 和 representation hash 都须校验。

### 6.1 响应 DTO 与证据字段

| 响应 | 固定结构 |
| --- | --- |
| capability | schema、decision_rule、actor_id（服务端解析的本地 actor）、viewer_hash、can_read/create/decide/cancel、limits；项目级能力不替代对象权限 |
| 列表 metadata | id、schema、decision_rule、ner_batch_id、task_id、assignee_id_frozen、input_set_hash、task_evidence_hash、status/version、created_at、calculated_at、viewer_hash、对象级 can_decide/cancel；分页 count/next/previous |
| 候选名单 | 分页的 id/name、固定 ner_batch_id/task_id、expected_manifest_hash、input_set_hash、viewer_hash；候选资格不承诺源在提交时仍完整 |
| 详情 evidence | metadata + observed_at、source_status/reason、frozen_counts、inputs、comparison、decision、cancellation；不重复另建 question 字段，原文只来自 comparison.selected_pair |
| 写 acknowledgement | 固定 id、schema/action、input_set_hash、原 UUID/receipt、replayed、current_status/current_version；派发 echo 原来源/受派人，决议 echo 原 winner ID/revision |

source_status=verified 时 inputs 是整个目标 cohort 的原 pins，comparison 直接采用已有 ner.public 的配对/选定 pair 形状；
left/right 只改变当前展示的 pair，不改输入集或计分范围。source_status=unavailable 时 inputs=[]、comparison=null、对象 can_decide=false；
frozen_counts 仍来自原冻结任务，不用空 inputs 重新计算分母。decision/cancellation 仅保留合法可读的裁决元数据，不带答案或其他任务 provenance。
GET 不返回其他人的 principal/source/请求全文；原键匹配所需的完整身份保存在服务端不变回执中，写 acknowledgement 只关联当前动作。
前端必须把 capability 的 actor_id/viewer_hash 与数据、回执 scope 交叉绑定；只校验类型或 HTTP 状态不足以确认写入。

## 7. 原键恢复先于可变条件

每个动作在项目级命名空间保存 UUID；唯一性分别约束创建、decision、cancel。
request_hash 的域含 schema/action/project/resource 与原始合法 body；同一 UUID 在不同动作不能交换回执。

顺序：当前身份/组织/角色/operation/mode → project fence → 查对应动作原键 → 校验原回执与不变账本 → 返回。
原键命中必须匹配同资源、原 principal **及 source**、resolved actor、完整 request_hash；否则 409 KEY_CONFLICT。

命中后不再进行：
- CAS/是否 pending 的新命令判断；
- 源 batch/Task/Submission 是否仍存在、受派人是否仍合格、最新修订选择；
- 当前降低后的源处理、W、参与人数、scorer-call 预算或重新计分。

原键恢复仍要求当前动作授权，不能让已撤权者取回答案或回执；恢复响应仅含元数据，不夹带源答案。
不变账本/回执损坏及不支持的 V1 envelope 要拒绝，不能用“恢复”掩盖损坏。
只读 metadata 仍按固定 V1 envelope 防护，不能无限加载被 SQL 篡改的大 JSON。

取消同样先恢复原 cancel receipt；新取消仅校验当前对象/版本/input hash，不加载/评分源，所以源丢失或处理预算降低后仍可执行。

## 8. 事务、来源重放与 PostgreSQL 竞态

新派发/采纳使用现有 shared project-operation fence；锁顺序为：
Project → 相关 OrganizationMember/User/ProjectMember → 仲裁行 → 来源 NerBatch → Task → TaskAssignment → Submission，
每类按 PK 有序；源用独立 base-table SELECT FOR UPDATE，不能对 nullable join 直接加锁。
新派发锁当前受派人的资格；所有写保持 atomic/fence 到 JSON 实际渲染、当前身份/权限、源/受派人及回执最后复核完成，失败整体回滚。

派发检查整个冻结 task cohort、原 calculation_hash 与实际 disagreement；
采纳再次验证相同完整 cohort，winner 必须恰好匹配其中一个完整 source pin。
不同 batch/同输入去重在 project fence + DB partial unique 下成立，不能先写来源再异步补账本。

读取不加写锁：每轮仅加载目标 cohort 一次、规范化/比较完整原逻辑；
两轮 counter 都作用于已界限化的原 preparation，**最后 counter 之后再重放来源/结果并复核当前权限**。
继承 #120 的修复，不把来源检查放在最后一次可能耗时的计分之前，不增加第三次加载。

新写也在计分之后完成最终源校验；源锁覆盖直到提交。应区分线性化顺序：
- 外部撤权/源变更先提交：本请求拒绝/回滚；
- 本写已持有相关锁：外部写等待，提交后撤权只禁止后续请求；测试不能要求锁内等待的外部 writer 同时先完成。
- GET 无锁读：独立 writer 可在 render/最后 counter 内提交，最终重放必须拒绝混合结果。
- 同连接 renderer 篡改、权限映射变化、实际响应改变或 scorer 失败：写回滚，UUID 不得被失败命令消费。

origin、当前源图与现有 valid_source/pin 都要通过；不回退旧源、不只核验 winner、不按 task ID 把旧分歧套给新 revision。
复制 origin 损坏/版本未知返回 409；来源 batch 删除、任一源删除/损坏/assignment 重绑等使详情整体隐藏答案，新派发/采纳拒绝。
恢复原源后可重新核验既有记录；不重新生成 origin 或改写裁决。

## 9. 预算边界与实际调用成本

预算均为 inclusive。命令、不可变存储 envelope、源处理和响应分层检查：

| 层 | 首版上限/要求 |
| --- | --- |
| 合法写请求 canonical JSON | 16 KiB；NUL/代理字符在 UTF-8 编码/哈希之前拒绝 |
| 新 origin canonical / SQL-stored | 64 KiB / 256 KiB；固定 V1 envelope，元数据读取 SQL CASE 保护后再解码 |
| 其他账本 JSON/Text | participant_ids 1 KiB / 4 KiB、winner pin 2 KiB / 8 KiB、cancellation receipt 16 KiB / 64 KiB（canonical / stored）；decision reason 的 stored UTF-8 <=16 KiB 后再按 1..2000 trim 字符检查 |
| 来源 NerBatch envelope | 原 V1 的 100 tasks / 20 participants per task / 2000 entries、1 MiB canonical / 4 MiB stored，不改旧检查 |
| 目标 task 参与人与字节 | 当前 NER 上限：20 人、64 MiB 累计 stored snapshots、8 MiB canonical snapshots |
| 配置/原文/实体 | 原 NER 单项 64 KiB XML/text、100 labels、单标签 256 B、每人 200 entities；绑定/Unicode/CRLF/重叠规则不变 |
| 工作量 | 每轮 W <= 5,000,000；不按人员/页/配对重复扩大预算 |
| counter | 一次请求两轮合计 <=380；只计分可定义配对，双空零调用 |
| 输出 | 全逻辑结果与实际响应各 <=1 MiB；分页不能规避逻辑上限 |

先对完整来源 Manifest 做原 V1 envelope/hash/schema 校验；该固定安全边界与当前计分预算不同。
新派发/新采纳/详情再对**已授权目标 task 的完整 cohort**做当前人数/2Q metadata 预检，超限 422、零源加载/零 counter；
随后只加载这一完整 cohort，检查当前实际字节/实体/W 并重放。不能用其他隐藏 task 的 W/Q 改变 Reviewer 的处理结果或泄露其统计。
这是单仲裁任务的证据处理，不是原批次整批读取成功；原 NER batch API 的整批可见集合预算和拒绝行为完全不变。
metadata/取消/原键恢复不套当前处理预算，但仍验证固定 V1 安全 envelope。
固定 envelope 必须覆盖所有 JSON/Text 列，不只保护 origin；来源被删除也不能让 malformed/超大本地回执无限解码。
participant_ids 必须等于 origin 的真实冻结参与人并集；Reviewer 可见性 SQL 也先以有界、类型检查过的参与人集合做过滤，不能先展开任意大 JSON 再按页丢弃。

来源 envelope 与实际目标成本必须分开记录：
本文“源加载”特指 Submission.result_snapshot 的整组 guarded hydration，不是声称没有 metadata SQL；
每轮可有大小预检与权限/元数据查询，但只能有一次完整目标 snapshot hydration，不逐人加载或重新解码原 JSON 字段。
- 原 batch 的全部固定 V1 counts/W/Q 需自洽且在当时 V1 envelope 内，但不能因此假称本请求执行了全批次计分；
- 当前 380 调用上限应用于本次完整目标 cohort 的 2Q_target；不会执行其他 task，也不证明其他 task 通过当前处理预算；
- 实际只加载目标 task 两轮，不加载其他任务的 Submission/正文；不要调用整批 project_items 再从结果截取目标；
- 令 Q_target 为目标冻结且可比较的配对数，成功、来源完整的新派发/采纳/详情实际 counter **恰好 2Q_target**；
- capability/候选受派人/列表/元数据/取消/原键恢复实际源加载和 counter 均为 0；
- 失败按实际已执行次数记成本，可能在第二轮之后被最终重放拒绝；不承诺失败一律零调用；
- 原逻辑、origin、命令/回执或实际响应超限必须在第一次 counter 前拒绝。新写回滚记录且原键未消费。
- 任一目标源不可核验时不对剩余输入组成较小 cohort 计分；只返回 unavailable/拒绝，不缩分母。

只对目标配对运行已有 ner.prepare / nervaluate；不为每个标注员、选定 winner 或显示页再计分。
切换 left/right 可发起新只读详情请求，该次仍对完整目标 cohort 计费/预算，不能只验两侧绕过全体来源。
首版不新增持久成本/统计表；通过真实 scorer spy、SQL hydration guard 和独立连接测试计数验证，不靠 mocked“成功总数”。

算例：2 人均非空时 Q_target=1、成功请求 2 次 counter；3 人均非空时为 3/6；20 人均非空为 190/380。
3 人中两人为空、一人非空时只有 2 个可定义配对，调用 4 次，双空那一对不调用。
全体为空时 Q=0，原任务 unavailable，派发预拒，不能创建一份“100% 一致”的仲裁。
如果当前调用上限降到 1，原 Q=1 的新处理预拒且无加载/计分，但已提交原键恢复和安全取消仍为 0 次来源加载/0 次 counter。

## 10. 页面完整流程、回执与异步状态

1. Manager 在既有 NER 冻结任务详情看到明确的「派发 NER 仲裁」入口；仅原 disagreement + 当前 capability 允许。
2. 进入沿用的仲裁派发视图，固定 NER batch/task 来源，选择当前合格 Reviewer；不混在批次列表内编辑。
3. 提交前明确「只新增仲裁任务，不改变原标注、审核或分数」。成功进新任务详情，保留可回到原 batch/queue/page/filter/focus 的导航上下文。
4. Reviewer 从仅自身分配的仲裁队列进入；复用 NerResult 的矩阵与完整实体高亮。选择某修订时显示其 ID/revision、实体数及整份采纳含义。
   若该 source 不在当前 pair，先显式读取包含该 source 的 pair；校验成功后才开启本次采纳按钮。选择空修订仍明确显示“0 个实体”，不自动选分数高的一侧。
5. 理由必填，明确确认采纳；完成后显示原决议、操作者/时间/winner pin 和当前状态。Manager 可在 pending 记录明确确认取消。
6. 取消关闭表单不提交命令；安全取消任务是单独写动作，不能混淆。窄窗口主操作/返回可达，键盘和焦点沿用现有组件。

新的 NER transport/schema/renderer adapter 负责复用；不能仅给原 ClassificationArbitrations 改标题。
NerResult 需要的展示 envelope 由目标任务的独立 DTO 明确适配：用 task_evidence_hash 及目标 pins，不传全批次 manifest/hash，也不伪造手动 NER 回执。
matrix 的 GET 只调用新 NER 仲裁详情，不调用分类、handoff 或旧 NER 对照接口作为回退。
分类原入口、NER 原只读队列及概览入口保持原行为，不新增同职责平行工作台。

每次命令固定 UUID + 原 body + project/resource + viewer 上下文；提交中禁止重复点，结果未知时保留原 intent，
仅提供同资源只读确认及用户显式「恢复原请求」，不自动 POST、不自动换 key、不因刷新失败重做裁决。
回执必须匹配 action/id/key/input hash/winner/提交时状态版本，不能仅凭 HTTP 200 或刷新成功判断已保存。
确认保存但详情刷新失败时保留保存事实，清空失效证据，显示同一记录的只读重试；不把新状态的出现当成当前人的命令回执。

project/batch/任务/身份变化、blur、撤权、错误先清空答案和写控件，generation + viewer_hash + scope guard 拒绝晚到响应。
viewer_hash 是 NER 仲裁独立 salt 下的当前 principal/source/actor/org/project/role HMAC，只用于关联 GET；不是授权，不写入 origin/回执，不接受 POST 回传。
同身份的未知 intent 可在内存保留用于显式原键恢复；账号/身份/scope 改变则丢弃，不能转交下一账号或跨资源恢复。
不写入 localStorage/shared cache 以跨登录保留答案或命令意图；页面关闭后不承诺恢复未保存 key。

两语至少包含：NER 仲裁 / NER arbitration、派发 / Dispatch、采纳此冻结修订 / Adopt this frozen revision、
原请求结果未知 / Original request outcome unknown、恢复原请求 / Recover original request、
冻结源无法核验 / Frozen sources cannot be verified。静态文案进入现有 catalogs/literal guard；用户理由/源文本作转义数据，不作为 HTML。

## 11. 验收矩阵

以下是后续开发要求，**本轮未运行、不可标记 PASS**。

| 类别 | 必须覆盖 |
| --- | --- |
| 规则/输入 | 2 人/3 人分歧；一侧空可采纳、双空/缺失不可派发；旧严格 F1/差异/calculation hash/概览完全不变；不读 live/最新修订 |
| 类型隔离 | 分类与 NER 相同整数 ID、错误 manifest/schema、分类字段/API/旧回执拒绝；两组开关/operation 权限不能互授；概览开关不授权 |
| 权限 | 跨组织/项目/记录，直接 API、actor/source 注入，非分配/自身参与者/Annotator/Manager 采纳拒绝；组织/账号/READ/新操作/角色/映射/Token 撤销；分页/count/候选名单先过滤 |
| 冻结完整性 | 全 cohort 与 winner、source/config/task/hash/assignment 图、origin/calc/version；batch/源/受派人删除及 hash 恢复；旧 approved/rejected/superseded 状态不被当成分数；历史参与者不因删除而消失 |
| PostgreSQL 并发 | 真独立连接：同键/异键派发、跨批次同语义输入；同/不同 key 采纳、不同 winner；采纳/取消两个顺序；锁等待/撤权的线性化；并发原键恢复；失败渲染/源校验写回滚 |
| 不可变 SQL | 两库 unique/check/partial/trigger、PK/origin/principal-source/pins/终态/decision 改写拒绝；nullable 用户 FK 清空与 project cascade 不破坏固定身份 |
| 恢复 | 创建后已取消/已解决、来源丢失/预算降低/受派人失效仍恢复原 metadata；body/principal/source/actor/目标变化冲突；撤权拒绝恢复；不同 key 不覆盖终态；失败不占 key |
| 预算/真实成本 | 各上限恰好值和 +1、目标完整 cohort metadata 预拒零加载/计分、SQL stored 并发增长先拒后解码、canonical/W/origin/实际 output、20 人 190 配对 380 调用；双空 0；元数据/取消/恢复 0；不加载旁任务；其他隐藏任务变化不影响目标处理，旧整批预算保持原行为 |
| 读竞态/成本 | render/最后 counter 内独立源变更拒绝，正常新增修订保持原 pins，当前来源不可用全体隐藏；引擎失败 503；无第三轮 load/counter；GET 无业务写 |
| UI / 英中 E2E | Manager 派发、Reviewer 矩阵/完整修订采纳、取消后新派发、原键恢复/未知结果/保存后刷新失败；self/未分配/撤权/源丢失与恢复、跨账号晚响应、返回位置/焦点/窄窗口；新仲裁流程没有分类/handoff/旧 NER 回退调用 |
| 旧行为 | 分类仲裁/抽样/返工/交接、NER 手动/批次/概览、原 Token/回执/原键恢复、Submission/ReviewDecision/release 均不变 |

后续 PostgreSQL 与英中 E2E 仅用合成数据、独立 CI/测试资源；本地 Docker/#76 继续只读且不重启，不借本设计启用服务或扫描保留库。

## 12. 实施切片与设计轮历史交付

确认后按一个独立实现分支推进：
1. 独立 schema/模型/不可变约束、scope/mode/新 operation 与元数据回执；
2. 来源完整 V1 envelope 校验、目标完整 cohort 预算预检/证据适配、NER 校验与精确调用成本；
3. 派发/决议/取消事务、CAS/原键恢复与 PostgreSQL 并发负例；
4. 完整仲裁流程的窄 adapter、NerResult 和两语文案；
5. 合成英中 E2E 与旧行为回归，另行获得推送/PR/合并授权。

以下保留设计轮当时的范围记录，后续实现以单独验收记录为准：
设计轮仅新增本设计和 authorization-plan 索引，没有应用代码/迁移、开关启用、测试服务、提交、推送或 PR；
当时没有运行实现验收，只做源码/契约交叉核对与离线文档一致性检查。
设计分支基于实际 main merge，不改主 checkout 或 #76；设计轮没有调用 Docker 或数据库。
保留旧/新 CHANGED、CHANGED_DATABASE_CONTAINER_REPLACED、原因 UNKNOWN、保护 worktree 历史缺失及全库比对 NOT_RUN_AFTER_DOCKER_INTERRUPTION。

依据：[分类仲裁](classification-arbitration-v1.md)、[NER 冻结队列](ner-batches-v1.md)、[NER 对照](ner-disagreement-v1.md)、
[NER 概览](ner-quality-overview-v1.md)、[审核交接](review-arbitration-handoff-v1.md)。
