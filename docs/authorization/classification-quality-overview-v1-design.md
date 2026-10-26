# 单个冻结文本分类批次质量概览：首版设计（#81）

状态：**APPROVED / 用户已确认首版设计；实现与验收结果单独记录**。

核对基线：main `ea16def511ff68874a8b9b28c7473bbf22b5ed5a`，tree
`c5ee044599415adb6e533847fb39fadcd969a23e`；当前设计 worktree 的源码树一致。
#81、#84 继续打开。本提案不构成合并、部署、启用功能或 Core 联调授权。

## 1. 最小范围与现有契约

- 仅分析一个既有 ClassificationBatch；“项目概览”准确称为“本批次项目概览”，不代表项目全量、跨批次去重、趋势、绩效或正确率。
- 包含批次汇总、标注员分页统计，以及回到原冻结任务对照的入口。没有跨批次排行榜、阈值、黄金答案、置信度策略或新仲裁命令。
- 来源固定为旧批次的 task/cohort/assignment/Submission/revision/hash。保留 `active-assignment-latest/v1` 已作出的选择；读取概览不重新执行 latest 选择。
- 完全匹配仍使用 `classification-exact-sets/v1`；部分匹配仍使用 `classification-jaccard-nonempty/v1`。不修改旧计算结果形状、Manifest、calculation hash、projection hash、原键恢复、仲裁身份或冲突优先排序。
- 新 schema 为 `classification-quality-overview/v1`，聚合规则为 `classification-batch-quality-task-macro/v1`。规则与旧指标分别标识。

架构边界：批次已持久冻结完全匹配结果；现有部分匹配是独立、临时的签名读取回执。概览不能把新增指标塞进旧 Manifest，也不能把逐项部分匹配回执当成永久聚合账本。首版新增只读归约和独立概览回执，不建表、不迁移、不回填。

## 2. 范围、分母与权重

先沿用 `batches.visible_items` 得到当前操作者可见的完整任务集合 V，之后才能加载来源、计算指标、统计原因、形成标注员集合或分页。Manager 使用完整批次；Reviewer 使用排除自身 cohort 后的可见子集。

在该可见集合中定义：

- N = 可见冻结任务数；C = 冻结结果为 comparable 的任务集合；U = N - |C|。
- H = C 中所有参与者在所有控件上完全一致的任务数；D = |C| - H。
- 覆盖率为 |C| / N。不可比较原因按旧冻结结果统计，每个任务只计一次；不按当前队列页或 agreement/disagreement 筛选缩小分母。
- 分数分母仅为 |C|；U 永远保留在范围和覆盖率中，既不记 0 分，也不记 100%。|C|=0 时分数为 null、状态 unavailable。N=0 沿用批次不可见的 404。

对一个可比较任务 t：n_t 为完整冻结 cohort 的独立标注员数量，K_t 为有效控件数量，P_t = n_t(n_t-1)/2。令 A_itc 为标注员 i 在控件 c 的已验证、非空标签集合。

```text
e_ijt = 1，当且仅当 i、j 在该任务的全部控件上标签集合相同；否则为 0
j_ijt = (1/K_t) × Σ_c |A_itc ∩ A_jtc| / |A_itc ∪ A_jtc|

E_t = (1/P_t) × Σ_{i<j} e_ijt        # 与旧任务 agreement 完全相同
J_t = (1/P_t) × Σ_{i<j} j_ijt        # 与旧部分匹配 score 完全相同

批次完全匹配一致率 E = (1/|C|) × Σ_t E_t
批次部分匹配程度   J = (1/|C|) × Σ_t J_t
```

任务等权；任务内无序参与者对等权；Jaccard 的控件等权。完全匹配不是各控件匹配率的平均：必须全部控件相同，e_ijt 才为 1。

H/|C| 若展示，名称必须为“全体一致任务占比”，不能与 E 混称“一致率”。参与者对总数及一致对数仅作解释计数，不用汇总后的 pair 分母替换任务等权公式。不同冻结配置的任务仅各自在任务内比较，不跨任务对齐标签或控件。

### 标注员维度

- 行身份 a 使用冻结 cohort.assignee_id；可比较输入要求其与冻结 submitter 对应，继续复用旧身份检查。缺失提交、空 submitter、重复身份等仍导致整个任务不可比较，不猜测归属。
- A_a 为 V 中 cohort 包含 a 的不同任务数（同任务只计一次）；C_a 为其中属于 C 的任务数；U_a=A_a-C_a；覆盖率 C_a/A_a。
- 每任务标注员分数先与该任务其余 n_t-1 人等权比较，再对该标注员的 C_a 个任务等权：

```text
E_at = Σ_{j≠a} e_ajt / (n_t-1)
J_at = Σ_{j≠a} j_ajt / (n_t-1)
E_a = Σ_t E_at / C_a
J_a = Σ_t J_at / C_a
```

- C_a=0 时两个分数均为 null；不可比较任务数、原因和覆盖率仍可解释。没有独立来源的标注员不能借其他人的配对获得分数。
- 可显示参与任务数、可比较数、不可比较数、涉及分歧的任务数和配对计数。涉及分歧仅指至少存在一个不一致的同任务对比，不表示该人答错。
- 不把标注员行再平均来计算项目汇总；每个人的任务集合可能不同。分页、姓名或标注员选择均不改变批次项目汇总。
- 身份键及计算不依赖当前姓名或当前项目成员列表；首版使用“标注员 #ID”。不为此新增任意用户查询权限。

所有比例以约分后的 numerator/denominator 十进制字符串返回；复用 Python Fraction，不把浮点数或显示百分数写入摘要哈希。UI 沿用两位小数百分比展示。H/D、分歧状态及所有判断均依据精确值，不能把四舍五入显示的 100.00% 当作全体一致。

### 固定验收算例

T1：a、b 均为 {A}，E1=J1=1。
T2：a={A,B}、b={B,C}、c={B}，E2=0，J2=(1/3+1/2+1/2)/3=4/9。

- 项目 E=1/2（50%）、J=13/18（72.22%）；不能用全部配对汇总得到的 1/4 充当项目 E。
- a、b 的 E_a=1/2，J_a=17/24（70.83%）；c 的 E_c=0，J_c=1/2。
- 再加入一个冻结时缺提交的任务 T3 后，N=3、|C|=2、U=1、覆盖率 2/3，E、J 不变。T3 不因后来补交变成可比较。

## 3. 不可比较与当前来源不可用

区分“冻结时不可比较”与“现在无法核验来源”：

| 情况 | 首版输出 |
| --- | --- |
| 冻结时人数不足、缺提交、空/缺失值、配置不支持或不等价 | 保留原 U 和原因；不补人、不回退修订、不删除控件；可对其余 C 按上述明确分母计分 |
| 当前任一可见冻结来源丢失、来源图不符、快照/hash/旧 calculation hash 不符 | 整份新概览 availability=unavailable；项目分数 null、标注员行不返回；只保留经完整 Manifest 和权限验证的范围计数及可见来源问题，不缩小 C 重算 |
| Manifest 自身损坏或绑定错误 | 409/404；不把损坏内容当作正常 unavailable 概览 |
| 新 revision、后续正常审核或仲裁决议 | 不替代来源，不影响仍完整的冻结输入和指标；继续使用现有来源有效性检查 |
| 隐藏任务的来源损坏 | 不加载、不影响该 Reviewer 的可见子集概览，也不报告隐藏数量/原因 |

单个成员/控件无效，不能只留下其余参与者/控件凑成可比较任务。沿用现有非空 Jaccard 边界，空/空不是 100%。第一次读取即发现当前来源不可用，不签发新概览回执。

## 4. 冻结回执与重放

- 对来源验证完整的概览签发独立 Django signing 回执；即使 C=0，也可冻结范围、不可比较原因和 null 分数。无数据库写入、无 Idempotency-Key。
- 回执固定 schema、聚合/基础指标版本、组织/项目/批次、当前可信 viewer 绑定、可见集合及完整 cohort/source pins 的摘要、旧 origin reference、首次 summarized_at、整个逻辑概览（项目汇总及全部标注员行）的 result_hash。
- Reviewer 只使用可见 projection reference，回执不含隐藏 task/cohort、隐藏总数或完整批次 Manifest hash。签名不是加密，也不是授权。
- 重放先验证当前权限，再恢复同一可见范围、精确来源和规则；正常重放的结果、分母、回执及 summarized_at 不变，observed_at 更新。batch.calculated_at 仍是旧批次的计算时间，不倒填新增概览的生成时间。
- 页面/姓名展示、observed_at 和分页切片不进入 result_hash；身份行、计数、分数、规则、完整任务引用及不可比较原因进入哈希。排序固定为 task ID、actor ID 的升序及原控件顺序。
- 角色、principal/actor 映射或可见范围变化，旧回执不能重绑新范围；按现有拒绝语义返回 403/404，仍有读取权但回执上下文变化时返回 409，清除旧展示后由用户显式读取新概览。
- 重放遇来源不可用，保留原回执作为引用但遮蔽分数/标注员行；来源重新完整且哈希一致后才可返回原结果。证据变更/规则版本不符不能静默重算。
- 复用现有密钥保留机制；签名密钥不再受信时回执返回 400。用户可显式重新读取同一批次，但那是新 summarized_at 的派生回执。首版不承诺跨密钥退役的永久指标留存。
- 不新增永久账本、自动任务或回填。旧批次无需迁移即可显式读取；原 Manifest、原始创建时间和旧 key 恢复保持不变。

## 5. API、权限及响应原子性

新增一个只读端点：

```text
GET /api/projects/{P}/classification-disagreements/batches/{B}/quality-overview/
GET .../quality-overview/?frozen={receipt}&page=2&page_size=50
```

- 第一次请求仅允许 page=1（可省略）及 page_size；后续分页必须携带 frozen。page_size 默认 50、最大 100，page 为正整数。切换页大小也沿用同一回执，不重新生成统计范围。
- 只允许 frozen/page/page_size；重复参数、跨项目/批次回执、actor、指标、权重、阈值、修订选择及队列 state 等参数均拒绝。没有 POST/PUT/PATCH/DELETE。
- 响应包含 schema/rules、scope（batch/project ID、visibility、可见 reference）、源计算时间、summarized_at/observed_at、availability、N/C/U/H/D/原因、覆盖率和 E/J、分页标注员行、frozen/result_hash。行含 actor ID、A_a/C_a/U_a、覆盖率、E_a/J_a、解释计数及该人参与的可见冻结 task ID 引用；无原始答案或标签差异列表。

响应字段固定为以下分组，避免把冻结范围与当前核验状态混为一谈：

| 字段 | 约定 |
| --- | --- |
| schema / rules | schema 及 aggregation/exact/partial/selection 的明确版本 |
| scope | kind=batch、project_id、batch_id、visibility=project 或 visible_subset、reference_kind/reference_hash；只含当前可见范围 |
| scope_counts | tasks=N、comparable_tasks=\|C\|、unavailable_tasks=U、原冻结不可比较原因计数；来源失败时仍仅保留这组经 Manifest 验证的分母，不把 H/D 当作当前可核验分数展示 |
| source_status | verified / unavailable / integrity_failed；current_source_issues 按受影响的不同可见任务计数，同任务只取一个主原因，integrity_failed 优先于 unavailable |
| availability / reason | available 或 unavailable；reason 为 null、no_comparable_tasks、source_unavailable 或 source_integrity_failed |
| summary | 来源完整时的 coverage、unanimous_tasks=H、disagreement_tasks=D、exact_agreement=E、partial_match=J；来源失败时整个字段为 null |
| annotators / pagination | 来源完整时为当前页标注员行及 count/page/page_size/next_page/previous_page；来源失败时两者为 null，不能用 count=0 伪装成没有标注员 |
| frozen / result_hash / receipt_verified | 来源完整时冻结全部逻辑结果、返回回执及完整结果哈希，receipt_verified=true；源失败时 result_hash=null、receipt_verified=false，仅可回显请求的旧 frozen（首次失败则为 null），旧 token 中的哈希只是历史引用 |
| batch_calculated_at / summarized_at / observed_at | 原批次时间 / 回执首次生成时间（尚无回执则为 null）/ 当前核验时间；不互相代替 |

完整 result_hash 绑定全部标注员行而不是分页切片；换页只返回同一已核验逻辑结果的另一切片。可比较数为零但来源完整时，可以 receipt_verified=true、availability=unavailable、reason=no_comparable_tasks，返回带 null 分数的正常统计行。source_status 与 availability 必须分别展示。

- 默认关闭 `ANNOTATION_CLASSIFICATION_QUALITY_OVERVIEW_ENABLED`；同时要求现有分类、批次、部分匹配开关、支持的 PostgreSQL/SQLite 后端及精确的 org/project fork_review 模式。Core-only-review、未知模式和关闭开关均拒绝。
- 沿用可信 session/token 解析、当前 Manager/Reviewer 和 READ_WORK。不新增写 operation，不因 ORGANIZE_CLASSIFICATION、REVIEW 或仲裁权限推导新的读取权限。
- Reviewer 排除自己作为冻结 assignee/submitter 所在的整项任务；在统计、源读取、标注员成行和分页前过滤。不能只去掉自己的答案继续计算剩余 cohort。Annotator、越组织/项目、撤销身份及直接猜 ID 全部拒绝。
- 现有 batch capability 仅增加可选的 can_view_quality_overview 布尔提示；UI 隐藏不构成权限边界。
- 返回前在实际 JSON render 后再次核对 principal/actor/role、READ_WORK、模式/开关、Manifest/可见范围、来源及完整概览哈希。期间发生撤权/来源变化则丢弃响应，不能返回混合时刻的数据。
- 沿用 private/no-store、no-referrer。查询/签名不合法 400，范围权限沿用 403/404，证据或同次读取变化 409，预算超限 422，后端不支持沿用 503；稳定的当前来源不可用返回 200 + availability=unavailable + null 分数，不伪装为成功分数。

## 6. 预算、复用和查询成本

- 沿用每批 100 tasks、每任务 20 participants、总 2,000 cohort inputs、8 MiB 累计 canonical snapshot、64 KiB XML、20 controls、100 labels/control 的包含端点上限，不扩充旧限制。
- 新逻辑概览（汇总及全部标注员行，分页前）最大 1 MiB canonical UTF-8 JSON；独立回执最大 16 KiB、禁压缩。均在服务端增量计数，超限 422；不能靠分页绕过、截断或自动拆批。
- 最多 2,000 个不同标注员行；每行不同 task 引用不超过 100，全部行的任务参与引用总数不超过 2,000。整数/分数字符串同样受 1 MiB 业务数据预算约束。
- 一次归约最多 100×190×20=380,000 个 control/pair 单元，响应复核再执行一轮。拒绝非法范围/Manifest 及可由元数据判定的超限后，再使用现有有界快照加载器；逐行加载、超限即停止，不读取 live Task.data 或新修订。
- 服务端复用来源核验、规范化结果、Fraction、Django signing 和 canonical digest；不逐任务调用 HTTP，不为每个标注员重查来源，不逐页重选输入。每轮只加载当前可见的源集合一次，再流式归约，结束后按规定重新核验。
- 不为取得 Jaccard 均值构造全部逐对标签差异 JSON；复用现有计算边界做小型归约适配。若需要提取共享纯函数，必须证明旧 DTO/哈希/规则完全不变，不引入统计框架或新的指标库。

## 7. 页面最小流程

复用 ClassificationBatches 的“批次列表 → 已冻结批次详情 → 原任务冻结对照 → 返回”完整流程；在批次详情增加显式“查看质量概览”入口及只读概览区域，不新增创建向导或独立工作台。

- 首屏显示“仅本批次/仅当前可见子集”、N/C/U/覆盖率、E/J、规则及两个计算时间；标注员表分页，按 actor ID 稳定排列，不做质量排名。
- 行内任务引用只调用现有 showItem/冻结对照和既有来源验证。返回概览恢复其分页、原队列筛选/页码与焦点；不改变原队列接口和仲裁入口。
- 只读刷新携带原 frozen，不自动 POST、自动换批次、重新选择修订或创建替代回执；“重新生成概览”必须显式，使用同一批次重新验证后给出新的 summarized_at。
- 初次加载、合法零可比较、源不可用、权限撤销、请求失败和超预算分别展示。blur、切换项目/批次/身份或失败会清掉旧分数、人员行和其任务入口；generation/viewer/origin 绑定丢弃晚到响应。
- 沿用现有组件、语言切换、键盘焦点和百分比格式；不新增审批、仲裁或发布按钮，不退休任何原有入口。

实现接入说明：普通 SCSS 经过上游 `lsf-` 前缀构建，新概览必须使用现有 `cn`/BEM 助手绑定自己的局部样式。不能假设裸类名会应用批次共享样式；不在此切片重写其他审核工作区。

## 8. 必须满足的验收

| 类别 | 必须覆盖 |
| --- | --- |
| 公式和权重 | 上述混合人数算例；单/多选、多控件；全部控件相同的 exact 边界；task macro 与 pair micro/人员平均的区别；不同控件数/配置的任务等权；约分与两位显示 |
| 分母和无效值 | 全部/部分不可比较、仅一人、无提交、空集/空空/null/缺控件、重复来源、非等价配置；N/C/U、A_a/C_a/U_a 及 coverage 一致；无分母不出现 NaN/100% |
| 冻结和重放 | 新 revision/后续审核不换输入；原先缺提交后来补交仍 unavailable；原回执换页/刷新结果不变；原 summarized_at 保留；跨批次/跨项目、版本、签名、上下文及结果哈希篡改拒绝；旧批次零迁移 |
| 来源不可用 | 删除任务/assignment/Submission、快照/config/hash/calculation hash 损坏；整份分数遮蔽且不缩分母；隐藏任务损坏不影响可见子集；完整恢复才能重放原结果 |
| 权限负例 | Manager/Reviewer/Annotator；self cohort；跨组织/项目/批次/人员 ID；直接 API；成员、READ_WORK、账号/Token、角色及 actor 映射撤销；分页总数/原因/回执无隐藏信息；Core/未知模式拒绝 |
| PostgreSQL 并发 | 独立连接在 materialize/render 期间撤权、改变同角色身份映射、删源/损坏来源、改变模式；响应失败且无泄露/写入。并发新修订不改变固定源；旧批次同 key 恢复仍返回原批次 |
| 预算和成本 | 每个上限恰好到界与超过 1；UTF-8 多字节；不可分页绕过；超限即停止后续加载；无每人 N+1、无 HTTP 扇出、无隐藏来源读取；概览 GET 数据库业务写入数为 0 |
| 旧行为回归 | 原 exact/partial 输出和哈希、Batch Manifest/projection reference、原键恢复、仲裁身份/CAS/采纳结果、冲突优先排序/抽样种子及 ReviewDecision/Submission 均不变 |
| 两语 E2E | en-US/zh-CN：经理批次概览→标注员分页→冻结对照→返回；审核员可见子集；Annotator 拒绝；0 可比较；新修订后重放；源丢失/撤权/超预算与晚响应清空；恢复原页码/筛选/焦点 |

开发阶段使用独立合成数据、隔离 PostgreSQL 和相应测试服务。当前 Docker/#76 只读约束继续有效，不能为验收重启它们；本设计也不授权启用保留环境。实现验证计划与实际运行结果必须分别记录。

## 9. 实施顺序、历史状态与关闭条件

设计确认后，最小实现顺序为：有界纯归约与算例 → 独立 GET/签名回执/当前授权及来源复核 → 现有批次流程内概览适配 → PostgreSQL 负例与旧契约回归 → 中英文合成 E2E。实现、PR、合并与运行验收分别报告。

仅改 Fork；不改 Host，不启动 Core 联调，不部署、不合并、不启用保留运行环境。#81 仍包含 NER、bbox、文档字段等剩余范围；#84 仍有其他审核策略/二次复核等范围，完成本切片不关闭二者。

保留历史 runtime CHANGED、CHANGED_DATABASE_CONTAINER_REPLACED、原因 UNKNOWN，以及全库比对 NOT_RUN_AFTER_DOCKER_INTERRUPTION。#76 原 worktree 路径缺失的比对继续为 unavailable，不声称 PASS。本设计阶段未调用 Docker、未执行数据库/浏览器测试，没有重启、恢复或清理环境。

依据现有代码与文档：
[冻结批次](classification-batches-v1.md)、[完全匹配](classification-disagreement-v1.md)、
[部分匹配](classification-partial-match-v1.md)、[仲裁](classification-arbitration-v1.md)、
[冲突优先](review-priority-v1.md)、[审核到仲裁交接](review-arbitration-handoff-v1.md)。
