# 单冻结 NER 批次质量概览：首版设计（#81）

状态：**CONFIRMED / 2026-10-28 用户已确认**；实现契约见 [NER 概览](ner-quality-overview-v1.md)，实际验收另行记录。设计核对基线：
`main@427d74bf62a2d57210a3e7bcf08f377748d956d0`。
设计与实现位于独立 Fork worktree，分支 `codex/issue81-ner-quality-overview`。

## 1. 最小范围与既有契约

只对一个既有 `NerBatch` 的当前可见冻结任务集合提供：批次项目汇总、标注员分页汇总、返回原任务 NER 冻结对照的入口。这里的 F1 表示标注间严格实体匹配程度，没有黄金答案，不代表正确率或人员绩效。项目汇总也只覆盖该批次，不代表整个项目。

- 保持 `ner-strict-entities/v1`、`ner-overlap-explanation/v1`、`nervaluate/0.2.0/strict-correct-counter`、`active-assignment-latest/v1` 的原语义。
- 新增独立 schema `ner-quality-overview/v1`、聚合规则 `ner-batch-quality-task-macro/v1`；在 `rules` 中分别固定 aggregation / strict / explanation / engine / selection 版本。
- 不改旧成对指标、agreement/disagreement/unavailable 分类、旧 Manifest/calculation hash/projection hash、修订选择、原键恢复、审核、仲裁、抽样、发布和交付语义。
- 不提供微平均切换、自定义权重、标签级排行榜、跨批次趋势、NER 仲裁写操作、模糊匹配或新的实体有效性标准。
- 无新表、迁移、回填、后台作业或业务写接口；#81、#84 保持打开。

现有架构有两处接入限制，不能直接套用分类概览：

1. NER Manifest 保存来源 pins、计数和计算哈希，没有保存完整配对分数。必须通过 `ner_batches.project_items` 核验并重放原修订，从其内部 `_prepared.logical.pairs` 归约；不能仅根据 outcome/count 猜分数、读取 live Task.data 或重新选择 latest。整批每轮只读取一次源集合，不逐标注员/逐配对调用接口。
2. `FrozenBatchWorkspace` 的概览出口当前直接渲染 `ClassificationOverview`，其公式、DTO、签名和请求都属于分类。增加显式 `adapter.renderOverview` 出口及 NER 独立 transport/schema；NER capability 的布尔值不能直接触发原分类组件。适配器缺少该出口时禁止回退到分类接口。分类默认流程保持原样，不泛化指标计算器。

后端复用现有 NER 来源验证、预算和响应检查；聚合使用标准库 `Fraction`，严格实体计数继续复用已固定版本的开源 nervaluate。不增加指标库或另写实体匹配引擎。

## 2. F1、聚合分母与权重

先验证批次，再通过 `ner_batches.visible_items` 确定当前操作者的**完整可见任务集合 V**，之后才加载源、统计、形成标注员行和分页。队列 state、页码或所选标注员不缩小 V。

对任务 t，n_t 为原冻结 cohort 人数，P_t=n_t(n_t-1)/2；缺提交者仍占原 cohort 位置。仅当整个任务输入通过原 NER 验证时才基于实体集计算配对；全空输入合法，但没有有效配对。令 E_it 为输入 i 的有效实体集合，M_ijt 为边界和标签均严格相同的实体数：

```text
f_ijt = 2 M_ijt / (|E_it| + |E_jt|)，当分母 > 0
f_ijt = null，                         当双方均为空（no_entities）

S_t = 有定义的无序配对集合；q_t = |S_t|
C   = {t ∈ V : q_t > 0}；N = |V|；U = N - |C|

F_t     = Σ_(i,j ∈ S_t) f_ijt / q_t
F_batch = Σ_(t ∈ C) F_t / |C|
```

任务内有效配对等权，任务间等权。实体多、人数多的任务不会自动获得更大任务权重。只做任务内配对，不跨任务对齐文本、标签或实体。不用全批累计匹配实体数计算 micro F1，也不用所有配对直接合并求均值。

必须同时返回下列解释分母，禁止把“有效”冒充“全部”：

- 任务数 N、有效任务数 |C|、不可比较任务数 U、原冻结不可比较原因；任务覆盖率 |C|/N。
- 总配对数 P=Σ_t P_t、有效配对数 Q=Σ_t q_t、无分数配对数 P-Q；配对覆盖率 Q/P。P=0 时配对覆盖率为 null，不能写 0/0。
- 无分数配对原因：可比较任务中的双空对计 `no_entities`；整体不可比较任务的 P_t 对均计该任务原冻结原因。一个任务原因只计一次，配对原因则按配对数计，二者不能混用。
- |C|=0 时 F_batch=null、availability=unavailable、reason=no_comparable_tasks；N>0 时任务覆盖率仍可为 0。没有可见任务沿用 404。
- 原 agreement/disagreement 任务数仅复用旧冻结 outcome，作为解释计数；不能由新平均 F1 或四舍五入百分比反推。

所有比例使用约分后的 `{numerator: 十进制字符串, denominator: 十进制字符串}`。服务端以精确整数分数计算和哈希；UI 沿用两位小数百分比，null 显示“不可计算”，不显示 NaN/100%。

### 标注员维度

行键 a 使用原冻结 `cohort.assignee_id`。来源存在时继续验证可信 submitter、assignment、revision 及 pins；不用客户端 actor 字段、当前姓名或当前成员表重新分配归属。

```text
A_a  = V 中包含 a 的不同任务集合
S_at = S_t 中涉及 a 的配对；q_at = |S_at|
C_a  = {t ∈ A_a : q_at > 0}

F_at = Σ_(涉及 a 的有效对) f_ajt / q_at
F_a  = Σ_(t ∈ C_a) F_at / |C_a|
```

每个标注员先对同任务的有效 peer 对等权，再对自己的有效任务等权。不能把双空对纳入 q_at，也不能因同任务其他人之间有分数而给无独立来源者赋分。原规则下，整体缺提交/无效会使整个任务不可比较；不保留剩余人的局部成绩。

行包含参与任务数 |A_a|、有效数 |C_a|、不可比较数及原因、任务覆盖率、总 peer 对数 Σ_(t ∈ A_a)(n_t-1)、有效 peer 对数 Σ q_at、无分数 peer 对数及原因、有效 peer 覆盖率、F_a、按升序排列的可见冻结 task ID。peer 分母为 0 时覆盖率为 null；|C_a|=0 时 F_a=null。

同一 task 只计一次；同一 cohort 里不得有重复身份。首版显示“标注员 #ID”，按 ID 排序，不增加任意用户资料查询、排名或阈值。项目分数不能由这些人员行再平均；分页不影响汇总。

### 固定公式验收算例

T1：a、b 各有同一个实体，F1=1。
T2：a={X,Y}、b={X,Z}、c={}，三个有效配对为 1/2、0、0，F2=1/6。

- 两任务 F_batch=7/12（58.33%），不能是全配对平均 3/8。
- a、b 的 T2 peer 均值为 1/4，因此 F_a=F_b=5/8（62.50%）；F_c=0。不能把人员均值 5/12 当作项目分数。
- 加入 T3：a={}、b={}，原 P 增加 1，Q 不增加；N=3、|C|=2、任务覆盖率 2/3，配对覆盖率 4/5，F_batch 仍为 7/12。
- 独立任务 a={}、b={}、c={X}、d={X}：六对中一对双空无分数，其余为 0、0、0、0、1，F_t=1/5、配对覆盖率 5/6；四个人的 peer 均值为 0、0、1/3、1/3，不能将其平均 1/6 当作 F_t。
- 冻结时缺提交的任务增加 N/U/P 及相应原因，不增加 C/Q；以后补交仍不改变原批次分母和分数。

## 3. 双空、缺失、源不可用与权限过滤

| 情况 | 首版行为 |
| --- | --- |
| 明确提交、未取消、合法空实体列表 | 空集合；与非空比较为 0；与空比较为 null/no_entities |
| null、缺 result、缺提交、取消/不可用修订、无效实体、文本/配置不一致或不支持 | 继续由原选择和验证规则决定整个任务 unavailable；不当作空实体列表，不剔除该人后计分 |
| 冻结时已不可比较，当前所有已固定源仍可核验 | 保留原 U 和原因；可以对其余 C 计分；可签发包含 null 分数的回执 |
| 当前任一可见固定源丢失、assignment 重绑、可信 submitter/pin/hash 不符 | 沿用 NER 的 source_unavailable；整份新概览 summary/annotators/pagination=null、availability=unavailable；保留经 Manifest 验证的原范围分母，不删除坏源后重算 |
| Manifest 损坏、跨项目绑定或重放逻辑与原 calculation hash 不符 | 沿用 404/409 拒绝；不转成正常的零分或缩小范围概览 |
| 隐藏任务的源不可用 | 不读取其源，不参与可见统计、不泄露其数量/原因，不影响可见子集的有效结果 |
| 新修订、正常审核状态变更、仲裁或新项目任务 | 不换源、不改变原选择/指标；原来源有效性检查仍须通过 |

当前 NER 来源投影不细分“缺失”和“源损坏”，均为 `source_status=unavailable`；首版保持该契约，不借用分类接口的 integrity_failed 枚举或猜测根因。完整 Manifest 检查和 calculation hash 不匹配仍明确拒绝。

权限沿用：可信 session/token → 当前 Manager/Reviewer → READ_WORK → 精确 org/project `fork_review` 模式 → 支持的 PostgreSQL/SQLite。新增默认关闭 `ANNOTATION_NER_QUALITY_OVERVIEW_ENABLED`，还必须满足既有 NER 和 NER batch 开关；不依赖分类/部分匹配开关。

- GET 不要求 ORGANIZE_NER，不新增 operation；创建、审核、仲裁、发布的权限不能代替 READ_WORK。
- Reviewer 若是原 cohort 的 assignee 或 submitter，排除**整个任务**；在源 SQL、计数、原因、人员行、分页和签名之前过滤。不能只删掉自己的答案继续计算。
- Manager 返回批次 manifest reference；Reviewer 只返回可见 projection reference，不含完整 Manifest/hash、隐藏总数或隐藏人员。全部隐藏沿用 404。
- Annotator、跨组织/项目、撤销账号/Token/READ_WORK、未知模式及 Core-only-review 均拒绝；客户端换 task/batch/actor ID、旧回执和 UI 缓存不能绕过。
- 继续保留项目 creator 的现有 Manager 规则；只改 creator 的 membership role 不等于撤权。验收使用实际组织/账号/operation 撤销，不能拿无效撤权制造错误预期。

## 4. 冻结回执、重放与旧批次

新增独立 Django signing 回执及 salt `ner-quality-overview/v1`，不用 NER 手动对照、preview 或分类回执替代。它是只读派生证据，不是新的永久指标账本。

回执固定 schema/rules、组织/项目/批次、可信 viewer、可见集合与完整 cohort/source pins 的 source_set_hash、旧 manifest/projection reference、首次 summarized_at、**全部逻辑汇总和全部人员行**的 result_hash。仅存摘要，不把原始文本、实体、隐藏来源或全体 pins 列表复制到 token。viewer 延续 NER 的可信身份和受支持密钥 fallback 校验；签名不授予读取权。

- 初次来源全部可核验时签发；即使 C=0 也可以冻结合法 unavailable 结果。首次源不可用时 frozen/result_hash/summarized_at 均为空。
- 重放先验证当前权限和开关，再验证回执范围、原可见集合/源、规则和完整结果；不得用最新修订、当前队列页或现有成员替代。
- 正常重放回执、summarized_at、分母、分数及 result_hash 不变；只更新 observed_at。batch_calculated_at 仍为原批次时间。
- 页码/页大小和显示百分比不进入逻辑结果哈希；全部人员行及其任务引用进入。固定 task ID、actor ID、pair 来源 ID 的排序。换页不能重新生成统计范围。
- 旧回执遇源不可用可回显原 token/summarized_at，但 receipt_verified=false、result_hash=null、整份分数和人员行遮蔽；恢复原源且所有 pins/hash 一致后才能重放原结果，不允许“恢复”为另一份修订。
- 身份/角色/可见集合变化不能把旧回执重绑新范围；当前无权沿用 403/404，仍可读但上下文/结果变更返回 409。先清除旧展示，再由用户显式读取新概览。
- 版本不符、签名损坏、压缩 token、超长 token 等返回 400；密钥退役不能静默重签成原回执。显式重新读取可生成新的 summarized_at，但仍使用同一冻结批次。
- 已有 `ner-batches/v1` 无需迁移即可显式读取；只接受其已知完整规则组合，不为未知版本猜公式。旧 Manifest/哈希/创建时间/原键恢复均不变。不把聚合规则回填到旧 Manifest。

## 5. 最小 API 与响应契约

```text
GET /api/projects/{P}/ner-disagreements/batches/{B}/quality-overview/
GET .../quality-overview/?frozen={receipt}&page=2&page_size=50
```

只允许 frozen/page/page_size，重复或未知参数拒绝。page 是正整数；page_size 默认 50、最大 100；初次只能 page=1；后续分页必须携带 frozen。拒绝 actor、任务筛选、指标/权重/阈值、修订选择、队列 state 等参数。没有 POST/PUT/PATCH/DELETE、Idempotency-Key 或自动重试创建。

| 字段 | 约定 |
| --- | --- |
| schema / rules | 独立 schema、aggregation/strict/explanation/engine/selection 规则 |
| scope / viewer / source_set_hash | kind=batch、project_id、batch_id、visibility、reference_kind/reference_hash；只绑定当前可见集合和可信身份 |
| scope_counts | N/C/U、冻结不可比较任务原因；P/Q/P-Q、无分数配对原因。源不可用时仍保留原冻结分母，不把当前未核验误写为新冻结结论 |
| source_status / current_source_issues | verified 或 unavailable；受影响的不同可见任务数及 source_unavailable 原因计数 |
| availability / reason | available 或 unavailable；null、no_comparable_tasks 或 source_unavailable |
| summary | 来源完整时的 task_coverage、pair_coverage、strict_f1、旧 agreement/disagreement 任务计数；当前源不可用时整个字段 null |
| annotators | 当前页的冻结 actor_id、任务/peer 分母和覆盖率、不可比较原因、strict_f1、可见 task_ids；无原始实体或文本。源不可用为 null |
| pagination | count/page/page_size/next_page/previous_page；count 是过滤后的全部人员数。源不可用为 null；合法无人员是 count=0/空行，不混同源失败 |
| frozen / result_hash / receipt_verified | 冻结整个逻辑结果；完整核验后为 true，包括合法无分数概览；源失败只可保留请求的旧 token，不返回已验证哈希 |
| batch_calculated_at / summarized_at / observed_at | 原批次计算时间 / 首次概览回执时间 / 本次核验时间；不能相互代替 |

原子读取沿用 NER 两轮 materialize 和实际 JSON render 检查：

1. 授权、批次/回执/可见集合校验；元数据预算 → 有界加载整批可见源 → 原逻辑和 calculation hash 核验 → 纯归约、全量逻辑预算及哈希 → 分页响应、签名及实际 JSON 字节/表示检查。此阶段不调用 scorer。
2. 通过 `NerAPI.respond` 的 validate 回调核对第一轮 prepared 的开源计数器；verify 回调重新取得当前上下文、整批 materialize，比较完整逻辑结果并核对第二轮计数器，最后再次核对权限/模式/开关及响应表示。
3. 期间发现撤权、源/范围变化或结果不一致则丢弃响应，不返回混合数据。GET 不调用创建/preview，不添加创建锁路径或业务写入；不能把读取测试扩成创建权限。

200 表示成功读取，合法不可比较/当前源不可用仍须显式 unavailable/null。参数/回执 400，权限/范围沿用 403/404，证据或同次读取变化 409，预算 422，支持后端/指标引擎失败沿用 503；引擎失败不降级为自算结果。沿用 private/no-store、no-referrer。入口能力仅新增 `can_view_quality_overview` 布尔提示；服务端授权始终生效。

## 6. 整批预算与调用成本

不提高既有限制；换页、重放、只看一名标注员均不能绕过完整可见集合预算。

| 项目 | 包含端点上限 |
| --- | --- |
| 冻结任务 / 每任务参与者 / 总 cohort entries | 100 / 20 / 2,000 |
| SQL 存储快照 / 累计 canonical 快照 | 64 MiB / 8 MiB，沿用同 SQL CASE/window 并发增长防护 |
| 单输入 XML / 文本、标签/标签字节、实体 | 各 64 KiB；100 / 256 B；200，沿用原 NER 规则 |
| 冻结 Manifest logical / SQL stored | 1 MiB / 4 MiB，旧批次验证限制与当前处理预算保持分离 |
| 累计 W / scorer calls | 每轮 ΣW≤5,000,000；一次请求两轮合计≤380，不能每任务/每人各有 380 |
| 新逻辑概览 / 实际分页响应 | 各 1 MiB UTF-8；逻辑概览在分页前含全部人员行，实际响应包含 token/包装 |
| 独立回执 | 16 KiB、不压缩；沿用既有 32 KiB Nginx 请求行承载，不放宽代理配置 |
| 人员行 / 任务参与引用 | 最多 2,000 行；每行≤100 task IDs，全部行任务引用合计≤2,000 |

纯归约每轮最多遍历 100×190=19,000 个结构配对及 2,000 个 cohort entry，即使全是双空也要计算结构预算。只遍历现有 normalized/pair 证据，不复制整批文本/差异到概览，不生成每个人的完整成对实体 JSON。

来源完整且请求成功时，设 Q 为可见冻结有效配对数，则实际 nervaluate 调用为 2Q；双空、整体不可比较任务为 0。整批准入要求 Q≤190：190/轮、380/请求通过；191/轮、382/请求必须在首个 scorer 前拒绝。当前源不可用时只会核对仍验证成功的 prepared 项，成本按这些项的实际有效对计，不能宣称整请求零调用；失败中断允许少于两轮，不能报告大于实际的成功调用数。

首轮完整逻辑/实际响应/回执及可预判的整批工作预算必须先于 scorer；第二轮也先校验其新预算再调用。源字节加载累计有界、超限即停。无人员 N+1、逐对 SQL/HTTP、隐藏源加载、截断、采样、自动拆批或异步兜底。GET 业务表写入为 0；必要的认证基础设施行为不冒充本功能写入。

复用 `Fraction`，其输入规模由上述实体/配对/任务数量和总 JSON 预算约束；分数字符串也计入字节预算，不以浮点近似规避超限。当前预算降低时概览可拒绝 422，但不得影响既有原键元数据恢复的“先找原记录、后续不重新算分”契约。

## 7. 页面与中英文流程

复用完整的“NER 冻结批次列表 → 批次详情 → 显式读取概览 → 人员分页 → 原任务/配对冻结对照 → 返回”流程；不建新工作台或创建向导。

- 直接复用 `FrozenBatchWorkspace` 的列表/详情/筛选/返回和 `NerResult` 原对照；通过显式 `renderOverview` 适配 NER DTO、API 和文案。可复用分类概览的布局、分页/回执导航经验和现有基础控件，不能继承其计算、接口或仲裁能力；没有需要退休的原入口。
- 显示“仅本批次 / 仅当前可见子集”、任务与配对分母/覆盖率、严格实体 F1、规则和三个时间。说明“任务等权；双空不计分；没有黄金答案”。人员按 ID 排列，入口只进入原冻结 task，不把人员选择当作重算范围。
- 返回恢复概览 receipt/人员页、原队列 state/页码、任务和触发焦点；不改变原抽样/仲裁入口。复用现有 Button、locale、BEM/cn 样式和窄窗口布局。
- 刷新携带原回执；仅显式“重新读取概览”才允许生成新的 summarized_at。无自动 POST、换修订或未知结果自动重试。
- 首次加载、合法全空/缺失无分数、当前源不可用、撤权、引擎失败、预算拒绝分别展示。blur、切换 project/batch/viewer、失败先清空旧分数/人员/任务入口；generation、独立 NER namespace 和 origin 绑定拒绝晚到响应。
- 回执仅通过上述 GET 的 frozen query 传输；不复制到页面路由、地址栏/历史、console 或持久浏览器存储，也不在新增应用日志记录 query/token。沿用既有受控请求、no-referrer 和代理能力；原始文本不进入概览响应。

| zh-CN | en-US | 语义 |
| --- | --- | --- |
| 本批次 NER 质量概览 | NER quality overview for this batch | 明确批次范围 |
| 严格实体 F1（任务等权） | Strict entity F1 (equal task weights) | 不称正确率/一致率 |
| 有效任务 / 有效配对覆盖率 | Scorable task / pair coverage | 两种分母分别可见 |
| 双方均无实体，无法计算 F1 | Both inputs have no entities; F1 is undefined | null，不显示 100% |
| 冻结时缺少提交 | Submission missing when frozen | 不等同空实体 |
| 冻结源无法核验，概览分数已隐藏 | Frozen sources cannot be verified; overview scores are hidden | 不缩分母重算 |

## 8. 实现后的必须验收（本轮尚未执行）

| 类别 | 最低验收要求 |
| --- | --- |
| 公式/权重 | 上述 7/12、5/8、1/5 算例；2/3/4 人和混合任务人数；task macro 与 pair pooling、entity micro、人员平均区别；精确分数/显示舍入；原实体边界/标签语义不变 |
| 空/缺失 | 双空全空、部分双空、单边空、零/一人、无 assignment、缺提交、null/缺 result、取消/不可用源、混合配置/文本；N/C/U/P/Q、peer 分母及原因守恒；0 分母无 NaN/100% |
| 冻结/兼容 | 旧 v1 批次零迁移；新 revision/补交/后续审核不换源；换页/刷新原回执和 summarized_at/哈希相同；签名、版本、来源、viewer、跨项目/批次篡改拒绝；未知版本不重解释 |
| 源不可用 | 删除/重绑 task、assignment、Submission 或损坏 pins/snapshot/hash；整份概览遮蔽且冻结分母不缩水；隐藏源不读；原源完整恢复才重放；Manifest/计算哈希坏明确拒绝 |
| 权限 | Manager、Reviewer self-cohort、Annotator；跨组织/项目/批次、直接 API、actor/reviewer 参数、Token/账号/组织/READ_WORK 撤销、角色映射变化、关闭开关和 Core/未知模式；人员/分页/原因/回执无隐藏信息；无 ORGANIZE_NER 的合法只读者可读 |
| PostgreSQL 真并发 | 独立连接在 materialize/render/两轮核验之间撤权、变更身份/模式、删除或损坏源；失败响应无旧分数；并发新修订不换冻结源；同键创建/恢复保持旧合同，GET 不创建替代批次；快照增长仍先 SQL 拒绝再解码 |
| 预算/成本 | 每个上限到界/越界、UTF-8 多字节、全空仍受结构预算；190 对/380 calls 通过、191 对/382 在零 scorer 时拒绝；页大小不能绕过全量逻辑上限；仪表验证成功 2Q/失败实际调用、每轮一次整批源加载、无 HTTP 扇出/人员 N+1/隐藏源、业务写入 0 |
| 异步/协议 | 页面/身份/批次/回执切换、blur 后晚响应、分页乱序、源不可用后恢复、结果/实际 render 表示篡改、代理可承载合法 token；错误和合法空结果可区分 |
| en-US / zh-CN E2E | 经理列表→概览→人员分页→冻结任务/配对→返回且页码/筛选/焦点恢复；Reviewer 子集；Annotator 拒绝；混合双空/缺失、全空、补交后重放、源丢失/恢复、撤权、预算拒绝及晚响应清空；关键数字和 unavailable 文案两语一致 |
| 旧语义回归 | 手动 NER、原成对 F1/差异解释、批次 Manifest/calculation/projection hash、原键恢复、分类概览及其回执/指标、审核、仲裁、冲突优先确定性排序/抽样种子均不变；无 Submission/ReviewDecision/发布策略修改 |

PostgreSQL 并发与中英文浏览器验收必须在获准的合成、临时、隔离环境中实际执行，可复用现有临时 CI runner。SQLite/纯函数测试不替代 PostgreSQL；定义了测试也不等于执行通过。当前本地 Docker/#76 只读约束不因设计确认自动解除；不能为验收启动、重启或迁移保留环境。

## 9. 最小实施顺序与本轮状态

确认后按顺序：纯归约/算例和整批成本 → 独立 GET/回执/两轮授权与来源复核 → NER adapter 概览出口和两语页面 → PostgreSQL/协议负例及旧行为回归 → 两语隔离 E2E。以一个小 PR 交付，不拆出新存储服务或跨系统接口。后续开发、推送/PR、合并、部署及 Core 联调仍分别受用户指令约束。

设计阶段仅新增文档和索引。离线 `fractions.Fraction` 算术核对：1/6、7/12、3/8、5/8、1/5 通过；不算功能、PostgreSQL、浏览器或运行验收。该阶段尚未执行上述测试、推送或创建 PR；后续开发实际结果单独报告，不回写为设计阶段已通过。

仅 Fork，不改 Host；不合并、不部署、不启动 Core 联调，#81/#84 保持打开。未调用 Docker、未操作 #76。持续保留旧 `CHANGED` / `CHANGED_DATABASE_CONTAINER_REPLACED`、新运行态 `CHANGED`、原因 `UNKNOWN`、保护 worktree 缺失及全库比对 `NOT_RUN_AFTER_DOCKER_INTERRUPTION`；没有新环境一致性 PASS，也不据离线设计抹平历史异常。

依据：[NER 严格实体对照](ner-disagreement-v1.md)、[NER 冻结队列](ner-batches-v1.md)、[分类概览设计](classification-quality-overview-v1-design.md)；后端 `ner.py` / `ner_batches.py` / `ner_api.py` / `ner_batch_api.py` / `overview.py` / `overview_api.py`；前端 `NerBatches.jsx` / `ClassificationBatches.jsx` / `ClassificationOverview.jsx`。
