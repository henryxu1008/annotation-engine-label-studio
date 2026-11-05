# #84 受限 V1：受控启用验收矩阵

基线 `d814ab63f205fb2ef4ecff007525c836e928bb82`；tree `c4af2313a80360487522afa6b634c1bf692c4f07`。**本轮仅设计；下列72项执行状态全部 NOT_RUN。**

范围：Fork-only、静态单Text/static Choices、supervisory_only、固定脱敏。设计静态验证和独立review在外部报告单列，不是72项运行通过。

[设计与准入](issue84-v1-closeout-activation-design.md) · [当前状态索引](issue84-v1-closeout-current-state.md) · [机器可读逐项前置/状态](issue84-v1-closeout-activation-matrix.json)

## 使用方法

每项须先取得对应阶段/组件操作的单独权限。E必须通过E0资源及启动支撑准入，可用获准synthetic provider；非测试provider是P关口。缺相应前置则停止，不运行settings、migrate、seed或worker。旧合成CI只作历史源证据。

矩阵里的未来源码负例、时钟合同与真实RQ链分别给回执；不能把mock/同步generate/旧READY包或受控时钟当真实dequeue/真实31分钟。平台SKIP、经批准NA、UNKNOWN、已证BLOCKED不计成功。真实producer/Host/部署/Issue关闭另外分层。
“无字节”限source答案或审计文件内容，通用拒绝JSON/headers可保留；“无写入”限新业务写入，原有安全拒绝审计/会话维护不据此被禁止。

阶段字段：`acceptance_phase` / `blocking_for` 与每行[E/P/O]一致。**E为69项隔离合成验收；P为L03/L04两个真实准备关口；O为L06实际操作授权关口。**
E只汇总其获准范围，P/O待满足不算E失败或已通过。E可通过但真实启用仍no-go。L03阻断需新op的真实组件；L04只阻断真实低置信度业务，不自动阻断另获授权的独立二审/audit。局部组件准备不称完整业务V1就绪。

## A：范围、基线与效力

依据：[docs/authorization/issue84-v1-closeout-current-state.md](../../docs/authorization/issue84-v1-closeout-current-state.md)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| A01 [E] 精确来源 | 只读核对获准source/tree、已合并PR与历史CI引用。 | 准确绑定本基线；历史PASS不写入本轮执行栏；Issue仍OPEN。 | source/tree/ref、历史seal引用和分阶段状态表 | NOT_RUN |
| A02 [E] 五项收口映射 | 逐项追踪原#84五个验收目标到现行实现/回归入口。 | 任务包、concern和审计不再误列缺失；不修改历史设计/Issue。 | 五项目标到代码/测试/历史回执/延期项的映射 | NOT_RUN |
| A03 [E] 支持类型 | 在获准隔离源中分别提交static单Text/Choices及NER/字段/bbox/动态或多Text。 | 仅已声明静态分类进入本流；不支持类型明确拒绝，无generic-export回退。 | 每种输入的typed响应及无新包/无答案旁路证明 | NOT_RUN |
| A04 [E] 监督效力 | 对approved/rejected分别记录confirmed/concern和两种followup。 | 初审、Submission、assignment、release/#79/#80资格与既有字节不被新增意见改变。 | 前后领域状态/内容hash、原意见和新receipt | NOT_RUN |
| A05 [E] 固定脱敏 | 真实下载新建audit ZIP并独立列举四个entry/manifest/图计数。 | 固定profile/effect；范围记录或明确gap，无原文、答案、自由理由或实名。 | ZIP/entry digests、结构验证、脱敏negative扫描与范围计数 | NOT_RUN |
| A06 [E] 阶段授权 | 对D/E/P/O、默认开关、scope和各类证据作状态核对。 | 设计通过不等于运行、provider质量、实际启用/部署/Host验收通过。 | 授权边界表；NOT_RUN/UNKNOWN/DEFERRED与历史PASS分开 | NOT_RUN |

## B：环境所有权与保护

依据：[docs/authorization/issue84-v1-closeout-activation-design.md](../../docs/authorization/issue84-v1-closeout-activation-design.md)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| B01 [E] PG所有权 | 任何Django import/migration/seed前，直接只读核验新cluster/DB实际身份与空catalog。 | 目录/进程/DB/port/system_identifier均匹配获准owner，随后才准入；loopback/名称不是证明。 | 允许资源清单、实际server属性和零表证据，凭据不入报告 | NOT_RUN |
| B02 [E] Redis独占 | 检查新独占Redis实际run_id、PID/目录、地址/DB与空keyspace。 | 不复用保留/共享Redis，不因新DB号或queue名即通过；检查前不得清空服务。 | owner manifest、非敏感server身份和空keyspace摘要 | NOT_RUN |
| B03 [E] 私有文件根 | 解析DB/data/exports/static/temp/report的canonical路径及重解析关系。 | 全在新run allowlist内，与主仓/其他worktree/保留数据和服务web根不重叠。 | 规范路径/owner与protected排除表；private root不可公开访问 | NOT_RUN |
| B04 [E] 进程与配置一致 | 逐个核对web/RQ PID、source和脱敏配置/secret-reference version/formatter。 | 相同scope/DB/Redis/root/instance/policy；缺少worker配置桥接不得开始E5。 | 逐进程启动代次、非敏感配置摘要和实际连接身份 | NOT_RUN |
| B05 [E] 合成身份与作用域 | 在空资源建立至少两个scope和独立角色/排除集。 | 不复用真实账户/业务数据；所有初审/二审/handler/cohort独立性可明确验证。 | 私有actor-role-source roster、空业务前置与fixture manifest | NOT_RUN |
| B06 [E] 拒绝未知或脏环境 | 给定非空DB、错误cluster、外部root或未获准资源证明。 | 在migrate/seed/worker启动之前停止；不修复/清空/绕过，不误报全套E通过。 | 只读拒绝回执及目标数据/资源前后未变证明 | NOT_RUN |

## C：配置依赖与提供者

依据：[label_studio/core/settings/base.py](../../label_studio/core/settings/base.py)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| C01 [E] 逐组件gate | 分别缺失/关闭batch/confidence/second/followup/audit的开关和各自mode。 | 使用真实依赖与拒绝语义；batch flag不是总开关；不能靠开followup补齐second gate。 | 每组件配置代次与入口响应，缺失op/未知/Core-only mode负例 | NOT_RUN |
| C02 [E] 稳定来源配置 | 核对instance UUID、profile digest、namespace/grant及model/evaluator/calibration版本。 | 与exact revision/input相绑定；只有operator配置可选；合成assertion不冒充质量校准。 | 已批准的非秘密descriptor/grant和版本摘要 | NOT_RUN |
| C03 [E] 受信operation映射 | 审查所用provider的来源、scope、版本、allowed_operations和角色门。 | Legacy缺新op仍拒绝；fixture仅合成scope；生产准备不能使用测试provider替代；paused不等于撤op。 | provider代码/交付review引用与逐operation允许/拒绝矩阵 | NOT_RUN |
| C04 [E] 真实队列配置 | 用现有approved_exports alias和APPROVED_EXPORT_REDIS_URL在新Redis启动独立worker。 | web与worker配置一致；不发明audit-only别名配置；现有mock/sync seeder不作transport证明。 | 非秘密endpoint/alias、独立进程和真实RQ job绑定 | NOT_RUN |
| C05 [E] 身份与密钥版本 | 完整审计UI用exact LocalIdentityProvider；核对SECRET_KEY引用版本/formatter相同。 | custom背景hook不等于collection支持；无独立audit签名key；不读/写secret值。 | 身份provider类型、接口能力、secret-reference版本和runtime指纹 | NOT_RUN |
| C06 [E] 冻结策略兼容 | 收紧limits、变policy version或formatter后读取/重试旧包，再恢复整份原配置。 | 旧policy精确匹配生效；拒绝不能靠改包/新UUID/新key绕过；兼容恢复后再按原契约访问。 | 原/变更/恢复配置摘要、原包hash不变与typed结果 | NOT_RUN |

## D：角色、操作与独立性

依据：[label_studio/access_control/project_operations.py](../../label_studio/access_control/project_operations.py)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| D01 [E] 服务端身份 | 通过认证会话执行并尝试注入actor/reviewer/effect/time/mode及不同principal来源。 | 身份只取可信mapper；新严格接口拒绝额外字段，旧初审可忽略未知字段但不采用伪造身份/效力，不为此新增parser。 | 按接口区分拒绝/忽略的direct请求结果、服务端actor与状态/owner不受伪造字段影响的证据 | NOT_RUN |
| D02 [E] 集合与对象隔离 | Manager/Reviewer/Annotator/admin/superuser、跨org/project和缺op分别访问list/detail/URL。 | 同时检查集合/对象；非scoped角色不因旧权限扩大可见答案、文件或新op。 | 允许/拒绝表、计数/分页与直接URL负例 | NOT_RUN |
| D03 [E] 自审和cohort排除 | 初审人、提交人、assignee、其他冻结cohort成员尝试二审或撤权后继续写。 | 完整排除集先于答案；Manager组织权不能替代被分派Reviewer决定权。 | 原cohort/assignment proof与无答案、无意见写入的拒绝回执 | NOT_RUN |
| D04 [E] 跟进四类权限 | 排除身份或无record op的Manager读metadata；分别测试source context、新record和原handler replay。 | metadata只需Manager/READ_WORK等原gate且无source/target snapshot；context/新写需record op与排除集；原键replay需当前record op和原handler/body/key，不读源或重做完整排除集。 | 四类入口的允许/拒绝矩阵、snapshot边界、source_integrity=not_checked与replay零源读 | NOT_RUN |
| D05 [E] 审计双人当前gate | 分别撤requester/creator角色、成员、op、账户或改变scope，再访问旧URL/HEAD/Range。 | 两个当前gate都成立才给包；creator tombstone不复活；可审计自身操作不等于自审。 | 每类请求的无字节拒绝、包完整性与当前身份/代际证据 | NOT_RUN |
| D06 [E] 安全取消边界 | READ_WORK/工作op撤销但Manager和second gates有效时取消；再关闭flag重试。 | 安全取消不授予答案；flag/mode/instance关闭后拒绝；终态永久占subject槽。 | 取消前后metadata、无snapshot输出和恢复后不重开的证据 | NOT_RUN |

## E：抽样与可信置信度

依据：[label_studio/review_batches/service.py](../../label_studio/review_batches/service.py)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| E01 [E] 比例/人员重放 | 全量、不同整数比例和per_submitter在同seed/来源重放，改变seed或来源再预览。 | sha256规则/冻结分母/rounding可解释；不通过分页或后续状态重新选样。 | request/plan/seed/denominator/selected flags和重放摘要 | NOT_RUN |
| E02 [E] 固定任务包 | 交集过滤同scope包和人员，重叠包及缺失/跨scope包。 | 同Submission只计一次；非法包拒绝；不授予assignment或泄露他人答案。 | 包membership与union去重/拒绝、plan计数 | NOT_RUN |
| E03 [E] 分类冲突优先 | 以固定独立cohort和规则输入构建分类风险batch，尝试混合priority类型。 | 输入/metric/rule版本冻结；不在V1偷偷合并多风险策略或重算当前源。 | cohort/input/result hash、策略discriminator和异常拒绝 | NOT_RUN |
| E04 [E] 置信度传输 | 可信合成producer经现有API提交exact input/nine pins的整数score assertion并重放事件。 | 验证当前grant/namespace/profile；主张不是模型推理或校准证明；不能用Prediction/导入分数。 | producer/input/profile/event receipt、score单位与效力标签 | NOT_RUN |
| E05 [E] 缺覆盖/历史grant | 令部分来源无observation、无可用profile；冻结后退役producer/grant再读历史batch。 | uncovered/unknown不是not_low；历史保持原profile/事实，不用当前grant重写选择。 | 覆盖/缺口计数、历史plan hash和退役前后只读结果 | NOT_RUN |
| E06 [E] 风险与预算异常 | 异常分数类型、超量/损坏来源、阈值边界和元数据超字节。 | 读取/decoder前gate；不clamp、不截断、不调用scorer补全；错误不得扩大可见性。 | bounded拒绝、SQL/byte观察和无模型调用证明 | NOT_RUN |

## F：受限端到端业务流

依据：[docs/authorization/review-sampling-v1.md](../../docs/authorization/review-sampling-v1.md)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| F01 [E] 初审与不可变性 | 对selected pending做approve/reject，重复/自审命令并比较旧修订。 | 单revision单初审；旧snapshot/hash/意见不被覆盖；未选/未审不是Approved。 | review receipt、旧数据hash、重复/自审负例 | NOT_RUN |
| F02 [E] 返工新revision | reject后原Annotator在原active assignment提交新revision并单独再审核；取消/改派对照。 | MyWork/Editor复用；旧batch不扩容；不复活assignment或新造任务授权。 | 新旧revision链、只读差异、任务权限和单独初审回执 | NOT_RUN |
| F03 [E] 二审两种意见 | 对approved/rejected分别派发独立Reviewer并记录confirmed/concern，随后出现更新修订。 | 新dispatch需合法latest；已派发旧exact仍按原proof；两种意见都不改变发布资格。 | 完整初审/dispatch/opinion proof和版本/来源变动前后结果 | NOT_RUN |
| F04 [E] concern两种处置 | 独立Manager分别no_change和合法更高latest target revision_linked，尝试第二次处置。 | 一次immutable处置；target九pins/初审proof准确；不称问题已解决、不自动返工/批准。 | source/target/status_at_record/hash、原key回执及二次写拒绝 | NOT_RUN |
| F05 [E] 分类仲裁边界 | 原pending分类batch交接合法cohort仲裁；approved/rejected concern尝试相同按钮/API。 | 原交接可用且冻结；concern不构造单人冲突cohort或新来源类型。 | handoff/arbitration frozen receipts和旧来源资格拒绝 | NOT_RUN |
| F06 [E] 双语与原生输入 | en-US/zh-CN完成全流；用可信Space/Enter，延迟响应、返回、切scope和撤权。 | 明确原意见/监督/跟进/审计效力；focus/异步不串scope；读失败清授权派生答案和链接。 | 两语原生输入/网络时间线、焦点与DTO验证证据 | NOT_RUN |

## G：真实RQ传输与兼容

依据：[label_studio/data_export/review_audit.py](../../label_studio/data_export/review_audit.py)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| G01 [E] 真实capture链 | 在API/浏览器中新建包，真实Redis入队、独立RQ worker消费并publish，HTTP下载。 | 未mock queue/ping/enqueue/generate；非seed预制READY；package/token/job/PID/文件digest全链一致。 | HTTP intent、DB提交、Redis/RQ job、worker启动/完成、ZIP离线校验 | NOT_RUN |
| G02 [E] 独立进程配置 | 使用实际web和worker启动清单；错误provider/mode/UUID/DB/root对照。 | 配置与owner proof一致才publish；无桥接/错误worker停止，不用同步fallback通过。 | 逐进程配置/连接指纹、拒绝状态和无新artifact | NOT_RUN |
| G03 [E] 队列故障与UNKNOWN | 已知提交前断队列、提交后丢dispatch/HTTP ACK和已确认后的新显式retry分别测试。 | 同一UNKNOWN命令保留原body/key；已有retry receipt的旧key只回receipt；新显式retry用新retry-command key且保持同package/T/input，不新capture。 | 提交前/后注入点、capture与retry namespace、旧receipt恢复和新attempt/RQ绑定 | NOT_RUN |
| G04 [E] 重复/旧token/lease | 重复投递真实job与旧token；另做明确标识的受控时钟lease合同或获准长时演练。 | 最多一次有效publish；queued30/running31资格不改业务时间字段；控时不冒充真实等待。 | backend PID/token fence和artifact唯一性；计时方法/预算单列 | NOT_RUN |
| G05 [E] worker中止/发布不确定 | 仅中止自有worker，覆盖未publish、publish DB结果不确定两阶段并恢复观察。 | 保留UNKNOWN原命令key/文件/lease/token；结果已确认后新显式retry可用新retry key、仍同package/T/input，不伪造cancel或重建capture。 | 注入/退出证明、DB/文件/receipt前后、命令namespace、合法lease恢复和原bundle digest | NOT_RUN |
| G06 [E] 既有导出共存 | 在同一全新隔离namespace运行合法#79/#80合成出口与audit，验证拒绝的来源。 | 共享alias/root/helper不混用领域guard；未审/rejected/concern不获得release资格，旧schema/字节权限保持。 | 两域前后receipt/digest、队列job类型、权限和文件回归 | NOT_RUN |

## H：冻结审计与下载

依据：[label_studio/data_export/audit_api.py](../../label_studio/data_export/audit_api.py)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| H01 [E] P/T与完整图 | P后修改来源再capture；另从完整合法批次冻结T，并核对selected/unselected/uncovered及lineage。 | stale preview拒绝；T范围有记录或显式gap；初审归属not_recorded不伪造batch内执行。 | preview/捕获回执、membership/version hashes、图闭包/缺口计数 | NOT_RUN |
| H02 [E] 缺源与版本 | T前缺类型proof、T后删除源/退役producer后worker/下载。 | 无法证明则拒绝新capture；T后只用frozen bundle/projection，不抓latest或同ID新源。 | 拒绝回执、旧bundle/ZIP稳定hash、gap/provenance和无hydration观察 | NOT_RUN |
| H03 [E] formatter漂移 | 更换不兼容runtime/formatter配置后启动worker或重试，再恢复原兼容配置。 | blocked_runtime或原typed拒绝；恢复原指纹才可同输入重试，不重建T/READY。 | 原/新/恢复formatter指纹、状态与输入/artifact绑定 | NOT_RUN |
| H04 [E] HTTP读权限 | 合法与撤权后的GET/HEAD/Range/If-Range/conditional逐入口读取。 | 最终recheck与同FD校验；屏障后新请求无source答案/审计文件内容字节，通用错误JSON可保留；旧流按J02单列，不宣称逐chunk撤权。 | 状态/headers/内容bytes/FD关闭和当前双gate证据 | NOT_RUN |
| H05 [E] 健康包与损坏包 | 错误artifact UUID、末端PermissionDenied，与真实文件摘要/ZIP损坏分别触发。 | 前两者不隔离健康包；实际完整性错误才quarantine且不可retry。 | 包/文件hash前后、typed码、quarantine状态和无字节证明 | NOT_RUN |
| H06 [E] 删除/代际复用 | 删除creator、Project或其他source actor，再复用数字ID尝试恢复/读取。 | creator tombstone/Project缺包不复活；其他源仅保留脱敏历史；不按数字ID水合新对象。 | 代际/namespace/tombstone、旧URL拒绝与冻结记录/孤儿文件状态 | NOT_RUN |

## I：并发、预算与资源

依据：[label_studio/data_export/audit_contract.py](../../label_studio/data_export/audit_contract.py)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| I01 [E] ACTIVE准入 | 新capture与blocked_auth/runtime retry在owner1/project2容量边界竞争，含原key/已ACTIVE恢复。 | 共享准入；拒绝无新receipt/token/input/allow-event/enqueue；不重复计算已有预留。 | 计数/状态/回执前后、拒绝副作用与成功释放后原命令结果 | NOT_RUN |
| I02 [E] 真实PG竞争 | 两owner抢最后Project槽、同owner retry竞争、capture与retry竞争。 | 不同实际backend PID和Project fence等待；只一个新预留；不mock COUNT，SQLite不代替PG结论。 | pg_blocking_pids/锁观察、两个响应、最终ACTIVE和队列job数 | NOT_RUN |
| I03 [E] 累积byte/SQL上限 | 达到和越过现行roots/assignments/revisions/cohort/byte/SQL/chunk/artifact/scratch界限。 | 所有上限同时生效；预读取gate、重复pass累计；不改限额/截断/跳测试求通过。 | 实际读取/预算观察、typed拒绝、无半包和完整默认上限快照 | NOT_RUN |
| I04 [E] 时间/清单预算 | 核对HTTP Redis短等待、worker时限、browser watchdog、完整CI清单与聚合。 | 区分3秒HTTP socket/1800秒worker/300+15秒call；不缩worker阻塞或扩UI限额；新增验收call另审预算。 | 逐层真实timing/timeout与调用/产物清单，队列时间分开 | NOT_RUN |
| I05 [E] 文件关闭与scratch | 成功、Range拒绝、流中断和worker故障关闭资源；保持READY/UNKNOWN lease与私有根。 | 关闭本响应/本attempt资源；健康错误不quarantine；不清已发布/不确定文件或全根。 | descriptor/进程/文件与lease前后，错误分类证据 | NOT_RUN |
| I06 [E] 来源/权限并发 | capture与合法删除/撤权、返回前authority/源变化、外部配置不同代次。 | Project-first兼容；最终复验失败回滚/明确UNKNOWN；不声称锁住外部配置或离线维护。 | 两个连接/进程的提交先后、回滚/回执与配置屏障观察 | NOT_RUN |

## J：停用、排空与回退

依据：[docs/authorization/issue84-v1-closeout-activation-design.md](../../docs/authorization/issue84-v1-closeout-activation-design.md)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| J01 [E] D_closed屏障 | 先在HTTP/gate有效时处理明确取消，再建入口屏障、drain，最后逐组件关gate/撤ops并确认进程。 | 只有配置代次收敛才定义D_closed；不先关全部HTTP再调用取消；paused/前端隐藏不算；拒绝无新业务写入，原有denial audit/session可发生。 | 取消/入口/配置屏障先后与进程代次、direct API及旧URL无敏感内容拒绝矩阵 | NOT_RUN |
| J02 [E] 在途流/命令 | 在最终recheck前、FD交出后和命令提交后分别停用，必要时仅终止获准自有连接。 | 区分回滚/已提交/UNKNOWN；流可排空，已发字节不可召回；不声称改flag原子截断。 | 各线性化点时间、已发送字节、commit/receipt及关闭证据 | NOT_RUN |
| J03 [E] 各作业状态 | 正常drain与紧急停用覆盖dispatch_pending/queued/running/ready/blocked/终态。 | 按源码观察，不统一叫cancelled；关二审前处理明确取消；没有audit cancel/reopen。 | 状态矩阵和worker再入/发布前gate，UNKNOWN保留 | NOT_RUN |
| J04 [E] 完整配置恢复 | 恢复原代码/formatter、schema、UUID、policy/profile/limits、身份和secret引用，逐进程开放；分别恢复UNKNOWN与发起新的retry。 | 无DB降级/新UUID/重签改包；UNKNOWN保留原命令key，旧retry receipt不再调度；新显式retry用新retry-command key且同package/T/input，旧READY不再生。 | 完整配置收敛、capture/retry命令和attempt身份、原bundle/manifest/receipt不变 | NOT_RUN |
| J05 [E] 不可逆事实与key | 取消二审/记录followup/creator tombstone/Project删除后恢复配置；评审secret轮换影响。 | 终态不重开/不复活；preview/cursor失效不等于owner原key必失效；本轮不轮换实际secret。 | 永久槽/代际/metadata证明、源代码key语义及未轮换声明 | NOT_RUN |
| J06 [E] 保护共享资源 | 停用时保持#79/#80、其他scope/worker、READY/UNKNOWN和原有worktrees/证据。 | 不FLUSH/广泛cancel队列/清根/删除lease；只操作授权自有进程；不声称物理擦除。 | 资源allowlist及前后保护快照、未授权对象无变更证明 | NOT_RUN |

## K：证据与兼容性

依据：[docs/authorization/issue84-v1-closeout-current-state.md](../../docs/authorization/issue84-v1-closeout-current-state.md)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| K01 [E] 逐项可追溯 | 每个case绑定scope、source、配置/进程、request/receipt/job/artifact与检查者。 | 历史来源、当前执行、环境前置分别记录；不能拿另一SHA/fixture/进程代替。 | 每项机器可读回执和内容摘要链 | NOT_RUN |
| K02 [E] 脱敏与两语证据 | 检查ZIP、公开报告及私有两语截图/日志分级。 | 公开不含答案/原文/自由reason/实名/凭据；合成答案截图仅私有；Unicode字节完整。 | redaction扫描、private/public manifest、en-US/zh-CN输入证据 | NOT_RUN |
| K03 [E] Core-only兼容 | 未知/Core-only/缺provider的新接口直接调用，结合原internal read/release回归。 | 新Fork入口拒绝且不冒充Host业务；原内部读取与独立发布策略保持，不连Host。 | 无Host调用的否定接口矩阵及原合同回归 | NOT_RUN |
| K04 [E] 延期范围 | 核对NER/字段/bbox、多风险、重开改派、多级/concern仲裁和发布门禁。 | 明确DEFERRED；不加入本次自动启用或把#81/#84整个关闭。 | 版本化scope/延期索引和无新增产品入口检查 | NOT_RUN |
| K05 [E] 状态分层 | 汇总D/E/P/O、缺资源、vendor skip、合成producer、真实质量和Host/部署。 | NOT_RUN/UNKNOWN/已证BLOCKED/SKIPPED不冒充SUCCESS；通过E不等于真实业务可用。 | 分阶段结果/限制、授权和未执行原因 | NOT_RUN |
| K06 [E] 独立复审与保全 | 绑定最终文档hash、源基线、review findings和全部原worktree/ref/旧证据。 | 修正后准确版本复审；只有新doc/matrix及设计报告变化，原4 worktrees/17分支/旧证据不动。 | review签收/内容hash、文档allowlist、旧seal和ref比较 | NOT_RUN |

## L：准入与交付决策

依据：[docs/authorization/issue84-v1-closeout-activation-design.md](../../docs/authorization/issue84-v1-closeout-activation-design.md)。所有case共用的前置是对应阶段/组件的另授权及资源/依赖准入；E使用获准synthetic provider不要求P已通过。

| ID / 阶段 / 名称 | 未来动作 | 必须观察到 | 所需执行证据 | 当前执行 |
|---|---|---|---|---|
| L01 [E] 设计授权边界 | 未来执行前核对D阶段命令/差异及新的执行授权，区别两阶段权限。 | D只允许文档与静态审查；实际迁移/测试/服务/启用/push/PR/merge/deploy须分别有新授权。 | D阶段操作/变更allowlist、NOT_RUN声明与新的执行授权 | NOT_RUN |
| L02 [E] 隔离执行授权 | 未来为明确环境/资源、fixture/启动配置支撑和操作范围取得授权。 | 没有实际所有权和授权不执行；支撑不足先另授权实现/review，不在本轮生成可运行启动器。 | 指定资源/owner/权限/支撑review及执行前置签收 | NOT_RUN |
| L03 [P] 真实权限提供者 | 真实启用前由权威所有者提供非测试op映射及其版本/隔离/撤权/背景能力审查。 | 缺provider真实启用阻断；synthetic suffix/TEST_ENVIRONMENT不能带进业务；custom collection不自动支持。 | 非测试provider交付/独立review和对应角色/接口能力验收 | NOT_RUN |
| L04 [P] 真实producer准备 | 仅真实低置信度业务启用前确认已授权producer的版本/工件/校准和source grant。 | 合成E协议通过不替代质量；缺producer只阻断真实低置信度使用，独立二审/audit组件按其另授权scope判定；不称完整业务V1就绪。 | 组件级依赖/授权与版本校准签收；未运行/已运行分开 | NOT_RUN |
| L05 [E] Fork V1收口 | 按获准E范围汇总69个E case与显式NA/vendor状态；P的L03/L04和O的L06另列。 | E必需项缺失给partial；E通过不把P/O gate算通过，也不因P/O待满足否定合成协议结果；无真实业务就绪/Issue关闭推论。 | E/P/O分组逐项汇总、NA理由、review、停用与组件级no-go清单 | NOT_RUN |
| L06 [O] 启用/部署边界 | 核对E后的下一步授权：真实实例、Host操作、部署和业务签收。 | 每项分别确认；本轮全部NOT_RUN；#81/#84保持OPEN，不碰Docker76/主仓/其他worktree。 | 阶段授权清单、明确未执行项和最终保护核验 | NOT_RUN |

## 结果与授权

本轮只可给出 DESIGN_REVIEW_PASS / DESIGN_STATIC_CHECK_PASS，执行结果仍NOT_RUN。
未来E签收逐项关联source/config/owner/receipt；E必需项缺失给partial/no-go，不能修改矩阵要求求通过。P/O各自仍NOT_RUN/UNKNOWN时不得声称真实启用就绪，也不将它们混入E通过数。
Fork V1 源码/隔离合成验收的通过不关闭#84，不扩展#81，不激活真实环境，也不修改Docker/#76或保留worktrees。
