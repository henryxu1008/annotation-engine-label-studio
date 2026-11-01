# 字段审核到仲裁交接 V1 验收矩阵（#84）

对应[设计](field-review-handoff-v1-design.md)，基线 `5757b5a5b65f1637923ad47aba29df915fa267cd`。**本表是未来实现的验收要求，所有功能项当前为 NOT_RUN**；设计静态审查通过也不得勾选功能 PASS。只使用自建隔离 PostgreSQL/SQLite、真实认证的合成用户和两语浏览器，不使用 Docker/#76、真实 Core 或其他 worktree 的保留数据。

## 夹具与取证规则

固定合成域包含 Org A/B、同组织 Project P/Q、Manager M、独立 Reviewer R/S、两至二十位 Annotator、transport B 映射 actor A 的可信身份与真实 Token。P 生成静态 TextArea 字段配置、有效 assignment 与 immutable Submission：raw/normalized 同值、纯 CRLF/ASCII-trim 格式差异、不同字段值、完整空值、全空、缺提交、配置不等价等；单独域生成已有分类/NER和无 priority 的旧批次。

来源 A 是完整冻结 FieldBatch 和匹配 field priority ReviewBatch；B 是不同 ID/名称/时间但完整 item 语义相同的 FieldBatch 及原生字段仲裁。另造同 task/同分但九项 pin 或规则不同的伪近似 B、pending/resolved/cancelled、FK NULL、恢复/损坏/超限来源。篡改/直接 SQL 仅作用于合成数据库；不可改保留实例或生产记录。

每个写用例保存父 body/key/hash、principal/provider/actor/viewer、A/B plan/Manifest/input/origin hash、目标/关联数量及状态、所有原业务快照 hash。fresh 失败前后比较零新台账和原键可复用；成功前后证明原 Annotation/Submission/ReviewDecision/发布/指标不变。所有回复须有可断言的状态/错误码，拒绝案例也检查无泄漏。

PG 并发必须两条独立连接，使用明确 barrier 和锁等待证据，覆盖两种提交次序；SQLite 顺序成功不能代替 PG 并发。预算用 SQL/JSON decoder/normalize/equality instrumentation 记录真实成本，不能只断言响应中的自报计数。边界值采用可实现夹具，在界内与超 1 都取证；有离散步长时取最近可实现的超界值并记录原因。

## A. 权限、资源范围及保密

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| A01 | 每个必需 flag 分别关闭；各 mode 缺失/unknown/Core-only；数据库不支持 | 新接口及直接 POST 拒绝，零源答案读取/写入；既有路径按自身门禁工作 | API PG/SQLite |
| A02 | Manager 有 READ_WORK+ORGANIZE_FIELD_ARBITRATION，独立撤回 ORGANIZE_REVIEW/ORGANIZE_FIELD/REVIEW/ARBITRATE_FIELD | 能使用已有合格源交接；独立撤回 READ_WORK 或组织字段仲裁则写/恢复拒绝 | API |
| A03 | 只有分类/NER组织权、Annotator、未授权/全局管理员、伪造 actor/reviewer/role/provider | 不获得字段交接或采纳能力；严格字段拒绝、身份来自服务端 | API/Token |
| A04 | 改 P/Q/Org、batch/Submission/handoff/field target ID，含 bool/float/string/unsafe integer | direct/list/detail 一致拒绝；无跨 scope 存在性/摘要/答案泄漏 | API |
| A05 | Reviewer 是任一冻结 cohort 的 assignee/submitter，另有非本人候选 | 整组排除且 own candidate 隐藏发生在 count/page/proof/target加载前 | SQL/API |
| A06 | R受派、S未受派，后续撤销项目/组织成员、角色或账号 | 当前 history/detail/旧浏览器/真实Token立即拒绝；无法借已知ID读答案 | API/PG/浏览器 |
| A07 | 新 assignee 为 Manager/creator/participant/跨组织/disabled/deleted成员 | 派发拒绝；当前有效独立Reviewer可派发 | API/PG |
| A08 | link 的受派人后来失效，或请求者具Manager身份 | link不改派/恢复采纳权；原目标端点can_decide仍受当前范围约束 | API |
| A09 | review或target FK NULL；re-created同号资源；跨project task/assignment移动 | Manager可见的本地关联明确 unavailable；Reviewer不列出；不得按冻结ID重绑 | SQL/API |
| A10 | files/media/export/import/storage/bulk/draft/annotation 直接端点 | 新关联不增加任何能力；既有授权/批准策略负例仍通过 | 既有回归 |
| A11 | transport B可信映射actor A、provider变化、组织切换但role不变 | 以当前A授权/审计；owner/provider/organization变更不能恢复旧回执 | Token/PG |
| A12 | READ_WORK已撤回、已知pending目标原SECURITY_CANCEL | 原字段安全取消仍可按既有契约执行；新handoff读/写不能借此绕过 | API |
| A13 | Reviewer历史/provenance最小投影与Manager fresh ACK | Reviewer不新增完整A/B Manifest hash、target origin_hash、cohort/principal/provider或答案；使用已有受派详情可见input/task_evidence摘要联合绑定 | API/前端 |

## B. 冻结来源、指标语义与同输入关联

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| B01 | selected frozen conflict、精确九项pin、当前最新pending | context合格；dispatch原子创建一目标一关联，原业务数据不变 | API |
| B02 | 普通/分类/NER/旧无field proof、unselected/agreement/unknown三个reason | context不合格；直接fresh POST拒绝，不能升级成field交接 | API |
| B03 | raw为CRLF/trim差异，normalized100% | 依旧conflict、允许交接；不增加错误归责、分数或新阈值 | 纯函数/API/浏览器 |
| B04 | 两人/三人分歧、完整空值对非空、全空/缺提交/配置不等价 | 完整空值可采纳；no_values/缺失/不可比较不可派发，不能当agreement | 指标/API/浏览器 |
| B05 | 九项pin逐项篡改及重算外层hash，含config/task_data只变一项 | 所有错配拒绝；不得降级为unknown或选新来源 | API |
| B06 | plan/Manifest的algorithm/source_schema/selection/metric/explanation/engine/queue_rule/结果/cost/hash逐项变更 | 完整固定V1包络拒绝，即使目标task局部看似有效 | API |
| B07 | 原null后补交、其他cohort新revision/后续review、source后来丢失/损坏/精确恢复 | null不替换；其他cohort不换最新；fresh仅完整原源可用时成功，恢复精确源才恢复证据 | API/浏览器 |
| B08 | 选中S先获ReviewDecision、superseded或新增更高revision | fresh拒绝；已存历史/原键不重派，不改分母或重新抽审 | API |
| B09 | A/B不同批次但完整item语义相同；pending v1/resolved v2 | 两侧完整来源均验证后允许显式link，保留A/B provenance及原winner | API |
| B10 | B同task/同分/同pair但cohort、pin、版本、计算hash不同 | hash+完整输入比较拒绝，不能仅凭分数或task复用目标 | API |
| B11 | B源缺失/损坏而A完好，及反向 | 不用另一来源替代；新link拒绝，历史不冒称源verified | API/浏览器 |
| B12 | cancelled目标、新key对同一关联、active target已存在的dispatch | 显式409，无自动link/redispatch/改派；旧key另按恢复语义处理 | API |
| B13 | 历史来源已正常审核或出现新revision，A/B原冻结来源仍完整；随后只删A而B完好 | 专用provenance不要求pending/latest，前者可联合原B详情显示；后者unavailable并遮罩，绝不借B替代A | API/浏览器 |

## C. 原子性、恢复和数据库约束

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| C01 | 成功dispatch；成功link；原键重放 | dispatch只新建field target+handoff；link仅handoff；同父键返回相同201/200事实 | PG/SQLite |
| C02 | 相同父键更改body/route/project/source/principal/provider/actor | 409或当前权限拒绝，不泄漏原回执、不借子任务回执 | API |
| C03 | 父UUID与既有standalone UUID碰巧相同 | typed表/命令命名空间隔离；child用server-private独立UUID，父响应无child key | API/DB |
| C04 | 目标取消/裁决/删除、review batch/FieldBatch删除、源超限、处理阈值收紧后原键恢复 | 只验证当前写scope和固定本地proof/输出，0 snapshot/0计算/0目标或实时源JSON读取；仍返回原关联事实 | API/SQL |
| C05 | 原owner同body但旧expected_viewer_hash；当前owner/provider/组织/权限改变 | 合法同owner恢复先于fresh viewer校验；变更主体/组织/撤权拒绝 | API/PG |
| C06 | 原关联proof/hash/strict ID/org/target关系被破坏 | 本地固定包络拒绝，不借实时来源重建；当前响应仍受上限约束 | API/DB |
| C07 | 在insert/render/第二遍计算/最终hydration/表示复核/输出限额处注入失败 | target+handoff全回滚、无假成功/孤儿，父key可用于后续合法重试 | PG |
| C08 | SQL UPDATE PK/scope/frozen refs/identity/time/proof/key/hash，NULL→non-null及非空重绑 | PG/SQLite guards均拒绝；合法便捷FK SET_NULL保留冻结证明 | PG/SQLite |
| C09 | project-key、source-target关联、active语义输入唯一冲突 | 无重复活动目标/重复关联；约束错误转换为明确冲突，不500或误成功 | PG/SQLite |
| C10 | 项目cascade、user便捷FK清理 | 遵循既有删除语义，冻结actor不变，不宣称永久审计或重新授权 | PG/SQLite |

## D. 独立连接并发与最终复核

| ID | barrier / 双向提交次序 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| D01 | 相同父键相同dispatch或link并发 | 一次创建，另一方原回执200；同一目标/关联/主体 | PG双连接 |
| D02 | 异父键同语义dispatch；standalone↔handoff | 一个201另一个409，正常锁序无死锁；不自动改模式 | PG双连接 |
| D03 | 两个link同一binding，不同键 | 一个关联，另一方409；同键则恢复 | PG双连接 |
| D04 | decision/cancel→link；link→decision/cancel | 前者旧CAS拒绝；后者保留link时版本，目标正常终态；取消后旧父键不重派 | PG双连接 |
| D05 | 普通审核/新revision→fresh；fresh→普通审核/新revision | 前者拒绝精确旧源；后者不自动创建ReviewDecision或阻止正常后续业务 | PG双连接 |
| D06 | 成员撤权/账号禁用/provider/组织切换→fresh或恢复，及写持锁先提交 | 真实锁等待顺序可证，先撤权就拒绝；不要求被锁writer先完成 | PG双连接 |
| D07 | FieldBatch A/B删除、task/assignment移动/改派、source/config/task_data变动 | 最终proof九项pin/组织及源图重验捕获，不接受旧响应或换源 | PG双连接 |
| D08 | 最后一次equality期间变源/换组织，最终hydrating SELECT期间增长 | 第二次算完仍有最终当前source检查，SQL CASE拦增长，无第三次计分 | PG/instrumentation |
| D09 | render-time mode/READ_WORK/组织权/actor/target/representation变动 | fresh双记录回滚，GET丢弃初次表示，恢复也需当前权限 | PG/injection |
| D10 | 两个不同origin顺序相反的link/独立字段decision/cancel混合 | 固定target-before-sorted-batches→source行锁序，无反向锁/nullable-join FOR UPDATE | PG双连接 |
| D11 | 从交接打开B详情的最后equality期间删除A Manifest/改变review plan；B自身完整 | 随后的最终provenance核对拒绝/不可用，答案从未进入可见状态或retention缓存；不额外计分 | PG/前端/浏览器 |

## E. 预算、SQL与真实成本

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| E01 | 完整plan/Manifest/candidates/tasks/cohorts在界与超界；非目标task坏包络 | 解码/目标投影前完整包络guard，不截短或借局部有效源绕过 | PG/SQLite/decoder |
| E02 | metadata单值/行/页/剩余额度边界，多字节UTF-8及SQL实际存储膨胀 | CASE同时拦截，proof/target/winner/reason累计，隐藏JSON不解码 | PG/SQLite/decoder |
| E03 | history先有整页proof，再遇多目标origin/participant/cancel/resolved decision | 先预留全部proof，共享4/16MiB，winner+reason虽不输出仍计费，无N+1漏账 | SQL/instrumentation |
| E04 | 同对象重复/不同对象，helper verified后仍读取manifest，最终metadata replay | 同遍去重有明确table/PK/field；重复实际decode不漏账；最终新上下文取当前源 | SQL/instrumentation |
| E05 | snapshot64MiB存储/8MiB canonical及增长窗口、默认ORM/refresh/行锁路径 | 两次hydration各有SQL aggregate CASE，任何提前快照解码均失败用例 | PG/SQLite/decoder |
| E06 | 2人1字段、3人2字段、20人20字段及raw/normalized双空 | 单遍2/2、6/12、400/7600；两遍4/4、12/24、800/15200，双空真实循环未抹零 | 纯函数/API/instrumentation |
| E07 | 缺失/不可比较/全部no_values及超字段/值/text/XML/CPU | 业务拒绝，实际已执行工作可追踪；CPU上限第一遍前预检，不伪报零成本 | 指标/API |
| E08 | eligible=true且完整成功的context/fresh dispatch/fresh link以及preview+POST | 每成功取证请求恰好两次hydrate/计算；最后replay不第三次score；0外部scorer，link不双算A/B | instrumentation |
| E09 | 切pair、review筛选/比例、history分页 | 不降低目标cohort计分上限，不加载全review候选答案/整FieldBatch答案 | SQL/instrumentation |
| E10 | response/proof/body输出上限，计算后响应才超限 | 实际渲染后拒绝并回滚；失败前计数保留；不截数据或少算来通过 | API/instrumentation |
| E11 | history/capability/assignees/原键恢复及历史provenance | 0 snapshot/0计分；provenance完整A/B元数据也累计4/16MiB每遍、8/32MiB两遍；恢复不依赖新fresh限额且不调用provenance | SQL/instrumentation |
| E12 | history page100总proof超4/16MiB或输出1MiB | 明确422；用户减page_size后可重试，无静默截断/改变count或伪空页 | API/浏览器 |
| E13 | unselected等预检不合格、合格完整成功、初次hydrate后source不可用三条context路径 | 分别200 false且0/0、200 true且2/2、明确409或预算422且失败证据保留实际部分工作；不伪报零/两遍或误判ACK | API/instrumentation |

## F. 双语完整流程、协议与兼容

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| F01 | en-US/zh-CN：审核页→交接历史→dispatch→独立R整份采纳→返回 | 原审核状态未变、原hash不变、仲裁独立resolved；页码/筛选/焦点恢复后fresh GET | 真浏览器 |
| F02 | 同输入pending/resolved link、cancel/显式redispatch、完整空值winner | 正确typed field流程；原来源A/B完整保留，无自动审批/返工/改派 | 真浏览器 |
| F03 | UNKNOWN超时/lost ACK、坏schema/kind/key/request/plan/input/actor/viewer/assignee/target/status/version/cost ACK | 保留原body/key，显式原键恢复；GET绝不POST，不换key/target/mode | 前端单元/浏览器 |
| F04 | 已确认保存后同viewer GET失败；身份/组织/provider切换 | 前者保留已确认事实；后者清敏感状态/旧intent，迟到响应不写新缓存 | 前端单元/浏览器 |
| F05 | blur、source切换、双击、历史和capability交叉返回、恢复晚于导航 | generation/viewer/source绑定；无误命令/误展示/重复写；typed未知类型拒绝 | 前端单元/浏览器 |
| F06 | history可读但source未复验、只丢A或只丢B、config/taskdata变动 | 普通history标not_checked；原B详情后最终provenance验证A/B才联合展示，任一unavailable遮罩comparison/inputs/decision/reason与动作 | 前端单元/浏览器 |
| F07 | field handoff body及无SubtleCrypto的HTTP测试origin | 包含expected_viewer_hash，使用现有noble SHA256，服务端/客户端typed command hash一致 | 前端单元/浏览器 |
| F08 | 关闭field handoff；NER/分类/普通批次与原字段独立仲裁 | 原wire/hash/key/permission不变；原回归全通过，字段开关不隐藏legacy缺键行 | PG/SQLite/前端/浏览器 |
| F09 | #128 SQL NULL回归，所有NER/field gate组合、partial algorithm/schema/kind marker | 普通/分类真实list IDs/count保留，损坏protected plan不解码/不混入旧路由 | PG/SQLite |
| F10 | keyboard/narrow viewport/两语长文本、字段含HTML或脚本 | 清晰操作顺序/焦点，文本转义，状态不混淆，无页面异常或答案写入日志 | 真浏览器 |
| F11 | 全量既有classification/NER/field/review安全回归、迁移检查、build/定向lint | 不削弱老测试，不重写旧迁移/依赖锁；共享既有Biome基线单独记录 | 本地隔离/未来CI |
| F12 | 未来PR与main验收 | exact head/tree、独立review、run/job/attempt和checkout逐一绑定；实际merge/main CI另验，不以旧#128结果代替 | 未来远端 |
| F13 | B详情成功，最终provenance超时、错viewer/H/target/input/task_evidence_hash/field_batch_id/task_id/状态/版本或晚到旧generation | 不显示/缓存答案；project/H依intent绑定，不读取B DTO不存在的origin_hash；同身份后续合法读取可恢复；该GET不POST/计分 | 前端单元/浏览器 |

## 完成判定与当前状态

实现验收必须逐项记录 SUCCESS、明确有条件的 SKIPPED、NOT_RUN 或 UNKNOWN，并解释环境、失败和证据位置；不把未执行项算通过。设计独立review必须核对权限、九项pin/全版本、完整包络、累计metadata、两遍计算后最终hydration、父子回执、双向锁序与旧契约。

当前：设计文档与矩阵已提供；功能实现、PG/SQLite功能/并发、浏览器、托管CI、部署、保留库比较全部 NOT_RUN（保留运行时/数据库一致性为 UNKNOWN）。后续授权分阶段取得，#81/#84保持OPEN，Docker/#76及其他worktree不操作。设计审查结论和最终文档hash另见本轮外置报告，不把审查自身变成上述功能项的执行证据。
