# NER 审核到仲裁交接最小设计

本方案补齐 #84 的 NER 显式交接：经理从已抽中的冻结 NER 冲突修订，明确派发一个仲裁，或关联已有的同输入仲裁；独立审核员继续使用现有 NER 仲裁流程采纳一份完整冻结修订。仲裁结果与原审核状态分别展示，互不代替。

这是 2026-10-29 已确认设计的冻结快照。下文 owner、未修改代码和 NOT_RUN 均描述设计时点；实现契约和验收边界见 [NER 交接契约](ner-review-handoff-v1.md)。设计基线为 Fork main 1cd29dd0d310696f0453b9b49e9f8e8ae040c804，即 PR #122 的实际 merge SHA。实现前重新核对 owner、main 和开放 PR，并建立独立 Fork worktree；运行验证仅使用自建合成资源。

## 产品范围

首版每次处理一个来源，支持两种明确动作。

| 动作 | 结果 |
| --- | --- |
| 派发新仲裁 | 建立一个现有类型的 NerArbitrationTask，并原子保存交接关联 |
| 关联已有仲裁 | 关联完整冻结输入相同的 pending 或 resolved NER 仲裁，保留两侧原始来源 |

只允许 selected=true、冻结依据为 conflict、完整 source pin 匹配、当前仍为 assignment 最新 pending 且没有 ReviewDecision 的 Submission 新建交接。冻结 cohort 中其他修订继续遵循既有历史来源验证，不临时换成它们的最新修订。

普通随机批次、分类优先批次、旧版不含 NER 依据的批次、未抽中、unknown、not_covered、修订错配、已审核、已被新修订替代或源不可用，均不能新建 NER 交接。历史关联仍可按当前权限读取，不因此重新派发。需要处理新修订时，应明确创建匹配的新冻结分析和审核批次。

有效分歧 cohort 中的真实空实体修订可以被完整采纳；缺失修订、全部配对双空、样本不足、不可比较配置不能据此创建仲裁。首版不增加自由文本交接理由，展示已有冻结冲突依据；裁决和取消继续使用原有理由字段。

不增加手工改写或拼接实体、批量交接、改派、二次审核、低置信度策略、自动裁决、自动批准、自动返工、发布策略或 Host/Core 行为。原 Submission、Annotation、ReviewDecision、抽样排序、NER 指标、概览及冻结批次保持既有语义。#81、#84 保持打开；此切片不构成任何一项 Issue 的关闭条件。

## 复用边界与必要架构调整

现有 ReviewArbitrationHandoff 的外键直接指向分类 ArbitrationTask，现有交接页面和 transport 也绑定分类 schema。仅放开 NER 按钮会造成类型、权限和回执混用，不能作为实现方案。

| 部分 | 最小改动 |
| --- | --- |
| 持久化 | 在 review_batches 新增明确关联 NerArbitrationTask 的 NerReviewArbitrationHandoff；保留分类表、历史迁移、约束和回执 |
| NER 派发 | 从现有 ner_arbitration_api.ListAPI.post 提取小型事务内 fresh-create 服务，独立派发和交接共同调用；不嵌套调用 HTTP 端点 |
| 冻结验证 | 复用 ner_priority、ner_batches 和 ner_arbitration 的完整来源验证及 input_hash；不新造指标或修订选择器 |
| 页面流程 | 复用审核批次、完整交接流程、NerArbitrations 和 FrozenArbitrationWorkspace；仅抽取带明确类型的来源与 transport 适配 |
| 权限 | 复用服务端身份、项目 operation policy、project-first fence 和 NER 独立审核员范围；不增加另一套权限存储 |
| 依赖 | 沿用 Django/DRF、标准 JSON/摘要、PostgreSQL/SQLite 约束及固定 nervaluate 0.2.0；不引入商业组件、通用工作流引擎或自建计分器 |

不使用 GenericForeignKey、以整数 ID 猜测仲裁类型、分类 API 回退、另一套仲裁决议台账或新的工作台状态控制器。现有独立 NER create 请求、schema、命令 hash、UUID 恢复及 decision/cancel CAS 必须保持兼容。涉及升级的改动在实现时记录到 docs/authorization/authorization-plan.md。

## 冻结关联与来源完整性

新表保存项目、可空 review_batch / ner_arbitration / created_by 外键，冻结批次、Submission、仲裁和 actor ID，服务端 principal、provider source、创建时间、父 UUID、请求 hash、origin 与 origin_hash。

origin 只含元数据和摘要，不复制文本、实体答案、可变 Annotation 或媒体地址。它固定以下关联：

1. 审核来源：ReviewBatch ID、plan_hash、算法、selected/conflict 依据、完整 candidate pin、calculation_hash。
2. NER 来源：源 NerBatch ID、完整 manifest_hash、task ID、selection / metric / explanation / engine 版本和输入 hash。
3. 目标仲裁：NER 类型、目标 ID、其自身 origin_hash / input_set_hash、原 NerBatch ID、创建者及受派人冻结 ID、创建时间、关联时 status/version。
4. 命令主体：可信 principal/provider source/actor、模式、父 UUID、请求 hash、时间与完整 provenance 摘要。

candidate pin 沿用 submission_id、assignment_id、assignee_id、task_id、revision、submitter_id、result_hash；NER cohort 还保留现有 config_hash 和 task_data_hash 等完整源身份。请求只能表达期望，身份及全部冻结 pin 必须由服务器重新取证。

新建唯一约束为 (project, idempotency_key) 和 (project, frozen review batch, frozen Submission, frozen NER arbitration)。数据库不可变 guard 覆盖 PK、scope、冻结字段、主体、命令和 origin。可空外键只允许清空，不允许非空改绑；不得凭 frozen ID 查回同号对象来重绑。SET_NULL 不证明清空一定源于删除，授权始终使用冻结身份与当前服务端权限。项目删除仍遵循现有 cascade，不承诺项目外永久审计保留。

链接已有目标时，同时完整验证审核来源 A 和目标自身来源 B；复用现有 ner_arbitration.input_hash(project, item)，并比较完整 cohort、冻结 pin、规则及计算依据。不能只比较 task ID、一对修订、F1 或 conflict 标签。A/B 的批次 ID、名称、创建时间可以不同，但语义输入必须完全相同，两份来源各自保留。

B 缺失或损坏时，不允许拿 A 替换 B 来修复目标。pending v1 和 resolved v2 可以关联；cancelled 不可以。关联不改派、不恢复被撤销的裁决权限、不覆盖既有 winner。

## API 与回执

新路由均位于 /api/projects/{P}/review-batches/{B}/ 下，明确使用 NER 命名。

| 方法和相对路径 | 行为 |
| --- | --- |
| GET sources/{S}/ner-arbitration-context/ | Manager 当前资格、冻结期望值、完整来源验证、已有目标和可用动作 |
| GET sources/{S}/ner-arbitration-handoffs/ | 当前可见的分页交接历史，默认 50、最多 100 |
| POST sources/{S}/ner-arbitration-handoffs/ | 显式 dispatch 或 link_existing |
| GET ner-arbitration-handoffs/{H}/ | 不可变关联及有权限的当前目标状态元数据 |

新 schema 拟定为 review-ner-arbitration-handoff/v1，kind 固定为 ner；分类 review-arbitration-handoffs/v1 和所有分类路由保持原契约。

POST 采用严格字段集合，并要求 UUID Idempotency-Key：

| 模式 | 字段 |
| --- | --- |
| 两种模式共有 | mode、expected_plan_hash、expected_input_set_hash、expected_viewer_hash |
| dispatch | assignee_id |
| link_existing | ner_arbitration_id、expected_arbitration_version |

不接收 actor、principal、provider source、角色、reviewer、winner、答案、approved 或任意状态字段。新命令必须匹配当前服务端 NER viewer_hash；使用现有 NER viewer 摘要规则，保持 capability、context 和回执身份可对齐。

请求 hash 使用新 typed schema/action 命名空间，覆盖 project、路由上的 review batch / Submission 和严格规范化的完整 body；父 UUID 另受项目内唯一约束。更改路由 ID 也属于不同命令，不能凭同键恢复其他来源的回执。

可读取但业务不合格的 context 返回 eligible=false 与明确原因；不能跨 scope 探测。非法字段 400，未知/不在范围对象 404，撤权或门禁按既有 403/404 约定，冻结期望/CAS/重复关联冲突 409，处理或输出预算超限 422。不会用空结果或更小 cohort 掩盖错误。

创建 ACK 绑定新 schema、父 UUID、请求 hash、project/batch/source、plan/input hash、目标 NER ID、mode、actor 和当前 viewer。前端使用独立的 handoff ACK 校验器，不合成一个假的独立 NER create 回执。响应记录 status/version_at_link，后续 GET 才表示当前目标状态。

历史 list/detail 是元数据接口：不重载答案、不重新计分；source_integrity 明确为 not_checked，链接缺失明确 unavailable。它们不能声称当前源已验证，也不返回未经复核的裁决答案。用户打开现有 NER 仲裁详情后，才按原端点预算和来源校验读取完整证据、winner 和理由。原键恢复也只返回原交接回执，不伪装为当前状态。

## 权限与 Core 边界

新增独立默认关闭开关 ANNOTATION_NER_REVIEW_ARBITRATION_HANDOFF_ENABLED=false。还必须同时满足既有 review、NER priority、NER disagreement/batch/arbitration 门禁及各自要求的精确 organization:project -> fork_review 映射；不引入或借用分类开关和分类仲裁 operation。

| 行为 | 服务端授权 |
| --- | --- |
| context | 当前 Manager + READ_WORK；展示当前能否写，不以 UI 状态代替授权 |
| dispatch / link / 各自原键恢复 | 当前 Manager + READ_WORK + ORGANIZE_NER_ARBITRATION，完整门禁和 scope |
| Manager 历史读取 | 当前 Manager + READ_WORK；可以看到保留的 unavailable 关联元数据 |
| Reviewer 历史读取 | 当前 READ_WORK、review candidate 可见性和现有 NER 目标受派范围的交集 |
| 实际采纳 | 保留现有 assigned independent Reviewer + ARBITRATE_NER 与原源验证 |
| 安全取消 | 保留现有 Manager SECURITY_CANCEL fence 和原取消端点，不增加交接读取依赖 |

Reviewer 的整组 self-cohort 排除、有效成员资格及目标受派过滤必须先于 count、分页和源加载。原审核批次或目标 FK 缺失时不向 Reviewer 展示该行；Annotator 无权使用新接口。Manager 身份不赋予 Reviewer 采纳权，分类 ORGANIZE_ARBITRATION 不赋予 NER 组织权，组织分析/抽审权限也不能代替组织仲裁权。

新派发沿用现有 assignee 校验：同组织、当前有效项目成员和 Reviewer 角色，排除完整冻结 cohort 的 assignment owners / submitters、项目创建者和派发经理。关联已有目标不重新分配受派人；其当前采纳能力继续由原 NER 服务判定。

现有安全取消可在 READ_WORK/组织仲裁权限撤回后按其既有契约阻止已知 pending 目标继续执行；新交接功能不能削弱这一通道，也不增加搜索或旁路读取。

停用账号、组织/项目撤销、未知身份/provider、缺失/未知 mode、Core-only-review、任一必需开关关闭或不支持的数据库均 default deny。每次读取、写入、渲染验证和恢复都重新核对当前身份与权限。既有文件/媒体/导出/批量接口不从新交接关联获得任何额外能力。

## 幂等恢复与并发事务

父 UUID 只属于新 NER 交接命令。fresh dispatch 为 NerArbitrationTask 生成一个独立、服务端私有的 child UUID；不拿父 UUID 借用或伪造现有独立派发的回执。两条记录在同一事务里创建，child UUID 不在交接 API/证明中暴露，不是用户恢复入口。UUID 隔离不能代替服务端主体与 action/body 校验。

同一项目、父键、body、principal、provider source、actor 的重试，在当前写权限通过后先读取并验证原本地回执，再考虑实时来源和当前处理预算。源/目标删除、后续修订、终态变化或处理上限收紧不应破坏合法原键恢复。历史 v1 存储包络仍须验证，当前响应预算仍适用。

fresh expected_viewer_hash 校验在原回执 owner/body 验证之后执行；不能让旧 viewer 期望阻断同一 owner 的合法回执恢复。客户端身份切换仍必须清空旧意图，不能跨 viewer 自动提交或恢复。相同父键不同主体/provider/body 冲突，不向调用者泄露原回执；不同父键但相同关联也冲突。

| 并发次序 | 结果 |
| --- | --- |
| 两个相同父命令 | 一次提交，另一方恢复同一回执 |
| 不同父键同时派发同一输入 | project fence 和现有 active uniqueness 保证最多一个目标；另一方明确冲突 |
| 独立派发先完成，交接再 dispatch | 不能借用独立派发回执；返回已有目标冲突，需用户新建明确 link_existing 命令 |
| 交接派发先完成，独立派发再请求 | 现有独立端点按原 active uniqueness 冲突 |
| 裁决/取消先于 link | 不匹配 expected_version 或终态不允许，409 |
| link 先于裁决/取消 | 保存当时版本；后续只读展示独立目标的最新状态 |
| 审核、新修订或撤权先完成 | 新交接拒绝，不选择替代源 |
| 旧目标取消后重试旧父键 | 返回原关联，绝不重新派发；新派发必须新命令/键 |

锁顺序沿用项目 fence；关联时先锁现有 NER 目标，再按 ID 排序锁所需 NerBatch，随后 Task、TaskAssignment、Submission。不另加与既有写路径冲突的 ReviewBatch 锁顺序。权限变更、普通审核、修订提交和 NER 仲裁写都须通过现有项目 fence；数据库唯一约束作为最后防线。

最终渲染前后重新检查身份、源 proof、pending/latest、target 版本及表示一致性。最后一次官方计数后仍要完整重放源验证。来源、权限、预算或响应表示变化时，新 target 与 handoff 一起回滚；没有已提交但无关联的孤儿目标。取消和恢复不重建源，不改写旧关联。

## 预算与实际调用成本

先做权限和元数据限额，再读取目标 cohort。每个源完整 Manifest 先验证，之后才只投影目标 task；不能把完整 Manifest 截短后宣称校验通过，也不能为交接加载 1000 个审核候选的答案。

| 范围 | 首版上限及规则 |
| --- | --- |
| ReviewBatch | 固定 v1：1000 candidates，canonical plan 1 MiB，SQL JSON 4 MiB |
| 每个 NerBatch | 固定 v1：100 tasks、每 task 20 participants、2000 entries；canonical manifest 1 MiB、SQL JSON 4 MiB |
| 单请求去重输入元数据 | canonical 4 MiB、SQL JSON 16 MiB；包括审核 plan、至多两份源 Manifest、目标 origin 和新 provenance，按相同对象去重 |
| 目标 cohort | 完整且至多 20 个冻结来源；A/B 不同 pin 直接拒绝，不加载两组不同答案 |
| 单次 snapshot hydration | SQL 字节总和 64 MiB、canonical snapshot 总和 8 MiB，解码前检查实际 SQL 字节和总量 |
| 两次源 hydration | 初始取证和最后重放；物理读取上限分别相加，最多 SQL 128 MiB、canonical 16 MiB，不冒充只读一遍 |
| 工作量 | 现有 W <= 5,000,000 / 计算遍；文本/XML 各 64 KiB、100 labels、每标签 256 bytes、200 entities 等既有上限不放宽 |
| 请求与持久证明 | body 16 KiB；新 origin canonical 64 KiB、SQL JSON 256 KiB，固定 v1 包络 |
| 响应与历史页 | 渲染响应 1 MiB；历史默认 50/max 100，逐项先做 SQL guard，整页 canonical 元数据 4 MiB/SQL JSON 16 MiB；超限明确拒绝，由调用者明确减小页大小，不静默截断 |

令 Q 为一个完整目标 cohort 的可比较 unordered pairs 中至少一侧有实体的配对数。context 完成验证、新 dispatch、新 link 都复用同一份已准备 cohort，在既有 render 前后各调用一次官方 strict counter，实际合计 2Q，最多 380；全空配对不调用 counter。

例如 2 个非空来源 Q=1、两遍 2 次；3 个非空 Q=3、两遍 6 次；20 个非空 Q=190、两遍 380 次；两个空加一个非空 Q=2、两遍 4 次。全部双空 Q=0 且业务不可派发。任何错误发生前已花费的调用也必须记入实际计数，不能只统计成功项。

context 和新 POST 是独立请求，各自最多 380 次；一个完整“预览再提交”最多 760 次。显式打开现有 NER 仲裁详情另按其既有 2Q 成本计算。元数据历史、capability、候选人和原键恢复均为零答案快照与零计分，不代表零 SQL。

fresh link 同时核验 A/B 元数据，只有冻结 item 完全等同时复用一个 cohort；不得嵌套调用两个已自带两遍计数的详情端点而放大成 4Q。验证时必须用真实 counter instrumentation 和 SQL/解码监测证明成本，不能只根据公式推断。

固定 v1 存储验证上限与可收紧的当前处理上限分开；新计算遵循当前上限，原键恢复不能重新扫描实时超预算源。所有溢出在新记录提交之前拒绝，绝不截断 cohort 或少算一次来通过。

## 页面流程与异步状态

审核批次页面的合格 NER 来源增加明确“交接仲裁”入口；旧分类入口保持原样。经理进入已有完整交接流程，查看冻结来源、资格、已有目标，选择派发或关联。已经存在活跃同输入目标时，提示明确关联；不把 dispatch 自动改成 link。

复用 NerArbitrations 的受派列表、证据详情、整份采纳和取消流程。新 handoff transport 只负责交接 context/history/create；后续 decision/cancel 始终走原 NER 端点、独立命令键和 CAS。适配缺失或 schema 类型错误直接拒绝，不能回退分类或临时 NER 对照。

页面分别显示“来源审核状态”和“仲裁状态”。历史页只显示未复验的关联摘要；完整 winner/理由在当前授权且源验证成功的既有仲裁详情显示，源不可用时遮罩完整证据。仲裁完成不会出现“已批准/已返工”成功反馈。

返回恢复原批次、页码、筛选和焦点，然后重新 GET；离开时只保留导航上下文，不保留答案或权限。表单及请求绑定 project/batch/source/plan/input/viewer/mode/target/version；成对读取的身份、来源或版本不一致不能组合使用。过期响应、blur 和迟到 promise 不能覆写新上下文。

提交中防双击。网络不确定或 ACK 缺键、错 viewer、错 provenance 时，保留原父 body/UUID 为 unknown，只允许显式原命令恢复；GET 不触发 POST，不自动切键、切目标或切模式。同 viewer 的已确认保存不因随后 GET 失败退回未保存；viewer 切换清空 pending/unknown/saved 和证据。

en-US/zh-CN 文案同时完成；键盘可达、状态/错误可辨、返回焦点、390px 窄屏主操作均须验收。

## 实施顺序与验收

设计确认后，先重新核对活动 owner、worktree、open PR 和 main，建立新的 Fork 独立 worktree。实现分为一个可审查切片：

1. 新模型/迁移/不可变 guard、typed service/API 及原 NER fresh-create 小型提取。
2. typed handoff schema/transport、完整流程复用和两语文案。
3. 权限、冻结、预算、PostgreSQL 并发与真实两语 E2E；更新授权设计文档。
4. 另行按用户指令推送/创建 PR/核验最终 head CI；本设计不授权合并或运行环境更新。

| 验收组 | 必须覆盖 |
| --- | --- |
| 权限负例 | 直接 API、跨 org/project/batch/source/target/type、伪造 actor、真实认证 Token、读取/渲染/写入/恢复时撤权；分类 operation 不能替代 NER；Core/未知 map/关闭 flags 拒绝 |
| 可见性 | Reviewer 整组 self-cohort 与目标受派交集先于 count/page/快照加载；账号、成员、角色、READ_WORK、ARBITRATE_NER 撤回；旧安全取消仍按原契约可用 |
| 冻结来源 | 全 Manifest、完整 pin、plan/calculation/rule/engine hash；2/3 人分歧、真实空实体、全双空、缺失/非等价；旧/随机/分类批次、源错配/不可用、最新 pending 边界 |
| 关联来源 | 不同批次但相同完整 item 可关联；不同 cohort/config 不能关联；A/B 原始来源分别验证、各自保留，B 失效不能被 A 替换；resolved 可关联但不改 winner |
| 原键与存储 | 相同 owner/body 恢复，跨 provider/source/actor/body 拒绝；父/child/独立键隔离；删除/SET_NULL/禁止改绑；固定 v1 包络；源丢失和处理 cap 收紧后恢复零计分/快照 |
| PostgreSQL 独立连接 | 同键/异 body/异键派发关联，独立派发竞态，裁决/取消 CAS 两种次序，审核/新 revision/撤权竞态，渲染或最后计数时源/身份/表示变更，原子回滚无孤儿 |
| 预算与成本 | 20/21 人；380 次可通过、当前上限收紧为 379 时先拒绝；W=5,000,000/+1、64/8 MiB 及各 metadata/proof/body/output 临界；实际 SQL 增长先拒绝后解码；两遍真实 counter、源去重、不加载全部 review candidates |
| 异步前端 | 丢失实际已提交 ACK 后原父键恢复；缺/错 key/viewer/input/provenance ACK；成对上下文漂移、身份切换、迟到响应、保存后读失败、无 GET->POST 或错误类型回退 |
| 中英文 E2E | 合成数据、真实隔离 PostgreSQL；经理 dispatch/link、独立 Reviewer 采纳完整/空修订、审核状态不变、cancel 后显式新 dispatch、撤权拒绝、历史 unavailable、返回上下文/窄屏/键盘 |
| 既有回归 | 分类交接、独立 NER create/adopt/cancel、分类/NER/普通抽样、返工修订对照、导出与发布权限；原指标、审核、仲裁和抽样语义不变 |

采用专用 NER handoff CI job/workflow 跑 PostgreSQL 与 en-US/zh-CN E2E，不继续向接近时限的 Enterprise E2E 单体堆积用例；既有相关 suites 仍运行。实现验收绑定最终具体 head、实际 run/job/attempt，不拿旧 SHA 或 PR test-merge 替代实际合并验收。

## 关闭条件与环境边界

本切片完成只代表 NER 审核到仲裁的受控关联可用。#81 仍需 bbox/文档字段指标与来源契约、对应产品链路和整体验收；#84 仍需低置信度策略契约、独立二次复核及整体关闭核对。不得把本地交接、仲裁采纳或 CI 通过解释为审核批准、交付许可或 Issue 关闭授权。

所有后续数据与 E2E 仅用合成夹具和隔离资源；不连接 retained #76 或真实 Core，不修改 Host。当前 Docker/#76 继续仅只读且不重启，设计阶段不执行 Docker 操作。若未来验证需要新的运行服务，先在当时环境中落实隔离方案，不能借本设计去启动 retained 服务或覆盖此前只读限制。

保留 OLD_AND_NEW_CHANGED、CHANGED_DATABASE_CONTAINER_REPLACED、既有保护 worktree 缺失记录、原因 UNKNOWN 及 NOT_RUN_AFTER_DOCKER_INTERRUPTION 全库比对记录；源码和 CI 成功不能覆盖这些运行态历史。此次状态为：代码未改、实现 NOT_RUN、PostgreSQL NOT_RUN、E2E NOT_RUN、Docker NOT_RUN、Host NOT_CHANGED、Core NOT_RUN、Issue 均保持打开。

## 设计依据

基线代码树为 9f710e36872e83bbee9d93332bab1a77a57fa543；当前保留 worktree 的 cf027b8c57cc74eee0c279d06b715d25349d6368 与已合并 main 的代码树相同。以下文件用于确认复用和隔离边界：

- docs/authorization/review-arbitration-handoff-v1.md 与 label_studio/review_batches/handoff.py、handoff_api.py、models.py。
- docs/authorization/ner-arbitration-v1.md、ner-arbitration-v1-design.md 与 classification_disagreements/ner_arbitration.py、ner_arbitration_api.py。
- docs/authorization/ner-review-priority-v1.md 与 review_batches/ner_priority.py、service.py。
- classification_disagreements/ner.py、ner_batches.py 的既有版本、成本和固定预算。
- ReviewBatches.jsx、ReviewArbitrationHandoff.jsx、ClassificationArbitrations.jsx、NerArbitrations.jsx 的完整流程、typed adapter 和 viewer/input fencing。

owner 核对范围为本会话可见的最近 50 个普通会话和全部 pinned、6 个已登记 worktree 及 GitHub 全部开放 PR。未发现并行 Fork handoff owner/实施，当前仓库无开放 PR；本会话保留 owner。历史 worktree 注册/缺失记录不修复、不移除、不据此推断全部环境完好。

设计时点的 main、Issue #81/#84 和开放 PR 已只读刷新，Issue 均为 open。机器可读快照和文档核对结果保存在本地报告目录 ls84-ner-handoff-design-20261029；新产品能力没有在设计阶段宣称 PASS。
