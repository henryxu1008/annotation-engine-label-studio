# 低置信度抽审 V1 验收矩阵（#84）

对应 [`review-confidence-v1-design.md`](review-confidence-v1-design.md)，基线
`1d4092c87d4e020dbd4f92aa63428679b44590e3`。**本文全部 A–G 功能案例当前均为 NOT_RUN**。
设计静态检查或独立文档审查通过，不等于 API/PG/SQLite/浏览器/真实 producer 通过。
本轮不写功能代码、migration、fixture 或 workflow，不启用开关，不启动服务。

## 夹具、取证与通过条件

未来只用独立原生 PostgreSQL/SQLite、真实认证的合成账号/token 和隔离双语浏览器；不用 Docker/#76
或其他 worktree/保留实例数据。域包括 Org A/B、Project P/Q、Manager M、专用 producer 审计账号 T、
Reviewer R/S、两位及多位 Annotator；transport B 可信映射 actor A 的身份提供者也须覆盖。

服务端登记 source profile V1/V2、相同显示标签但不同 descriptor digest 的版本、不同模型/校准语义的 profile，
专用 T 的 source grant 与独立 PROVIDE 操作。准备完整静态 Text/Choices Submission：
同分、低/非低、整数 0/1_000_000、缺事实、仅旧 revision 事实、通用 Prediction、重派、缺失 FK、
unsupported 配置、损坏 hash/receipt、可精确达到/超出各预算的事实和快照。

每条写案例保存：完整原 body/key/request hash、principal/source/actor、source profile/digest、九项 pins/
input/fact/plan/receipt hashes、实际查询和解码/哈希/排序/pass/模型调用计数、HTTP code、表行数与响应字节。
比较 Annotation/Submission/ReviewDecision/旧 batch/发布导出记录的前后快照，证明无业务副作用。
明确区分：确定性拒绝后零新行、已提交后 lost ACK 的 UNKNOWN、显式原键恢复。

PG 并发使用两条独立连接、barrier、事务提交顺序和 NOWAIT/锁等待证据。SQLite 顺序成功不能替代它。
stored bytes 必须度量 SQL 返回 JSON 文本的 UTF-8 字节，不能用字符数或压缩后物理存储大小替代；
spy 必须位于实际 JSON decoder / SQL hydration / scorer 边界，不只相信响应里的自报成本。
预算“界内/恰好上限/超 1”在夹具可实现时逐个执行；离散步长使用最近可实现超界值并记录原因。

## A. 身份、权限、范围与无扩权（14）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| A01 | 新旧 flag 各自关闭、缺失/unknown/Core-only mode、非 PG/SQLite | 所有新 input/ingest/priority 直接 API deny，decoder/写入/模型调用为零；旧路径按自身契约工作 | API/PG/SQLite |
| A02 | 普通 Manager 有 ORGANIZE_REVIEW，未登记 producer / 无 PROVIDE | 能在已允许 profile 上规划；不能 GET producer input 或 POST score，已知 ID 不能绕过 | API/token |
| A03 | T 的 source grant、PROVIDE、READ_WORK、项目/组织成员或 active account 分别撤销 | getter、fresh append、receipt recovery 均即时拒绝；无旧 token/旧浏览器豁免 | API/PG |
| A04 | 只有 PROVIDE、READ_WORK；撤回 ORGANIZE_REVIEW 或 REVIEW | PROVIDE 不授予规划/审核；规划不授予审核；当前拒绝后旧动作消失 | API/组件 |
| A05 | 伪造 actor/provider/producer/model/status/hash/完整 subject pins 等额外 POST 字段 | 严格 400，身份/descriptor 来源是服务端；无新行/泄露 | API |
| A06 | 替换 project/org/instance/profile/submission IDs；整数ID的bool/float/string/unsafe integer与UUID的非法编码分别测试 | project/submission等数字ID严格，instance/profile/event UUID须合法规范UUID字符串；scope/object/collection一致拒绝跨域且无存在性/答案泄露 | API/SQL |
| A07 | Reviewer 自己 assignee/submitter 与非自己候选并存 | 自己项在 count/queue page/live fact/snapshot 加载前排除；只为 hash 检验解码 bounded frozen metadata | SQL/API |
| A08 | 其他 Reviewer / Annotator / 未授权全局管理员直读、直 POST | 不得取得 producer 输入、创建事实/批次或获得审核权；当前合法 Reviewer 只读既有范围 | API/token |
| A09 | producer actor 是该 source 的 assignee 或 submitter | getter/fresh ingest 拒绝；不通过改 actor 请求或其他项目授权洗白来源 | API |
| A10 | 可信 principal A、session B；同角色 actor/source/provider/org 切换 | 使用当前 mapped A 授权与审计；前后身份不一致丢弃/rollback；旧 owner 不能借新映射恢复 | PG/token/组件 |
| A11 | 关闭 confidence 后列表含普通/分类/NER/字段批次与 confidence | 正 marker 归一化 SQL non-null bool；只过滤新受保护计划，旧缺键行 ID/count 保留 | PG/SQLite |
| A12 | 只含新 kind、schema 或 algorithm 的 partial marker；未知版本/坏 kind | direct/list/detail/queue/rework/批次上下文 review 均不落入普通/分类 fallback；限额后明确拒绝坏 proof | API/前端 |
| A13 | Manager/Reviewer 投影对比 | Manager 无 publisher principal/event/凭据/答案扩张；Reviewer 无 raw score/完整 source proof/其他答案；self rows 不可见 | API/组件 |
| A14 | 原 files/media/export/import/storage/bulk/draft/annotation/review 端点 | 新置信事实、selected/完成状态均不增加能力；既有批准/下载负例保持 | 既有安全回归 |

## B. 来源、版本、九项 pins 与事实摄取（18）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| B01 | 合法 registry + 专用 T 读取当前 latest pending 完整静态分类修订 | getter 返回确切 snapshot、九项 pins、完整 input_hash；非当前 Task/config 拼接 | API/单元 |
| B02 | 合法 producer 声明 exact input_hash + 严格整数 score | 仅 metadata 新 fact，server descriptor/owner/accepted_at 完整冻结；不复制答案、不触碰 Prediction/Task 更新时间 | PG/SQLite |
| B03 | 只存在通用 Prediction、人工导入 score/model_version、task agreement、冲突一致率 | 均不变成可信事实；候选为 unknown，预览不新增 fact、不调用模型 | API/单元 |
| B04 | 改九项 pins 中任一项、input_hash、snapshot.data/config/annotation result | source 完整性验证；伪造/损坏 409，不靠字段非空或 request hash 通过 | API/PG |
| B05 | 旧 Submission snapshot 未变，live Task.data / Project.label_config 后来变化 | 继续只读/绑定原 snapshot；不把当前值当历史输入；涉及结构/权限变更仍独立检查 | API/PG |
| B06 | GET input 后新 revision、review decision 或合法重派先提交 | fresh ingest 按 latest pending/assignee=submitter 规则拒绝；旧事实不能迁移到后继 | PG 双连接 |
| B07 | 相同 profile 标签/模型名，但 digest/artifact/校准/输入规范不同 | 视为明确不同版本；request 必须精确 digest；不猜 latest、不能混排 | API/组件 |
| B08 | descriptor 自报 digest 与 canonical 内容不符、重复profile+digest不同内容、缺artifact/score版本 | registry/fresh路径deny；内容变化必新digest；同名标签不替换旧版本，错误配置不悄悄回退 | 单元/API |
| B09 | 同 UUID/body/owner 在 source 已审核、删除、变大或 descriptor 退役后恢复 | 当前 source grant/操作仍有效时原 receipt 恢复，零 snapshot/model 读取；不得再摄取/改写 | PG/SQLite |
| B10 | 同trusted producer namespace下同event UUID改body/score/profile/digest/owner/provider，或actor同ID principal不同 | event唯一键独立profile/digest，明确KEY_CONFLICT；不同producer namespace的UUID独立；不能借profile切换绕过或返回他人的receipt | API/PG |
| B11 | 不同 event 对同 project/profile digest/Submission 事实；尝试更正 | BINDING_EXISTS，零替换；更正须明确新 digest + event，旧事实/计划原样 | PG/SQLite |
| B12 | PG/SQLite UPDATE/bulk/SQL 改 PK、score、proof、scope、时间或 receipt | guard 拒绝所有冻结字段变动；仅约定 convenience FK→NULL 可行，不能恢复或重绑 | SQL |
| B13 | FK NULL、资源重建/复用 frozen 数字 ID、跨项目移动 | fresh getter/append 不重绑；新计划 unknown/unavailable 或拒绝结构损坏；历史不换来源 | SQL/API |
| B14 | 非法 score：bool、float、字符串、负数、超百万、NaN/Inf、孤立surrogate | 400/strict序列化拒绝，无 clamp/转0/缺分当满分；零新行 | API/单元 |
| B15 | score 0/1_000_000、合法 UTC evaluated_at 与 server accepted_at | 精确定点值不漂移；producer 时间不决定权限、覆盖顺序或真实性证明 | API/单元 |
| B16 | URL/resolver/动态Choices/unsupported/混合配置，或 decoder 前无完整输入 | 不输出假置信声明；getter/fresh append 拒绝，抽样候选保留为 unsupported unknown | API/单元 |
| B17 | 真实 producer 未接入，只有合成 issuer/stub | 仅记协议/绑定边界通过；真实推理/模型质量/平台接入 NOT_RUN，不宣称认证概率 | 验收报告 |
| B18 | instance 克隆/namespace 变化或 source grant 撤销 | 跨实例 event/input 不复用；当前 grant 撤销拒绝 producer 恢复；不更改旧冻结证据 | API/运维合同 |

## C. 抽样、分母与确定性（13）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| C01 | 同 source profile/digest、候选、seed、rate 重复预览 | 完整 plan/hash/选中集合一致；两个 materialization 均纳入 proof | 单元/API |
| C02 | global 与 per_submitter；比例边界1/100、rate100、seed变化 | 每组 ceil 原配额、requested/actual rate 明确；seed只影响合法tie/fallback，不改分母 | 单元/浏览器 |
| C03 | package 重叠、submitter 过滤、无效/跨项目 package、取消 assignment 留 pending | 复用既有交集/去重和pending语义，不新建候选规则或缩分母 | API |
| C04 | 低分多于quota、同分tie、低分少于quota | low 按单位值升序、同分seed；未覆盖low/补足数准确；不静默扩样 | 单元/组件 |
| C05 | known-not-low 与 unknown 混合补足 | 同bucket seeded顺序；unknown不视为高置信或自动排除，所有候选仍在分母 | 单元/API |
| C06 | threshold=0、阈值处 score、threshold=1_000_000、score满分 | 严格 `<`；0不命中；边界等值不命中；无浮点舍入 | 单元 |
| C07 | 0份合格事实、全unknown、0条low | 允许明确种子补足，覆盖0/unknown说明可见；不声称风险已覆盖或批准 | API/双语 |
| C08 | 仅旧 revision 有 fact；新 latest pending 同 task/assignment | 不转移 score；新候选 unknown，老preview因候选改变失效 | API/PG |
| C09 | 单批次混 profile/model/calibration，客户端附带多来源/score | 严格拒绝；既有三种 conflict 与confidence单一判别union，不能伪造组合key | API |
| C10 | 计划冻结后新增 fact/new revision/更正版本/descriptor退役 | 选择flags/hash/分母不变；历史as-of与live source health分离，不刷新排序 | API/浏览器 |
| C11 | 合法重派/源FK丢失与内部fact hash损坏分别发生 | 前者明确unknown/unavailable；后者409整次拒绝，不用unknown掩盖损坏 | API/SQL |
| C12 | 绕过guard伪造相同subject多事实、重名模型/latest乱序 | 无任意first/min/latest选择，proof invalid拒绝；正常唯一约束仍防止API插入 | SQL/API |
| C13 | 原普通/分类/NER/字段请求、计划hash与原键恢复 | wire/hash/算法/行为不变；无 confidence 输入时不附加新默认字段或改旧数据 | 兼容回归 |

## D. 字节、SQL predecode、CPU 与输出预算（10）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| D01 | 1000/1001候选与source union，过滤/页数/按人拆分 | 先限count、无截断；union去重且不通过分页或分组重置预算 | SQL/API |
| D02 | 单snapshot SQL2MiB/canonical256KiB，旧text/config更小界限 | 界内/上限/超界；SQL超限前decoder=0，canonical超限不进入hash/model，旧更小上限不提升 | SQL/spy |
| D03 | union SQL64MiB/canonical8MiB，多行分别界内但总量超限 | 同次完整材料化累计拒绝；只测单行/最终JSON不算通过 | SQL/spy |
| D04 | inspection后snapshot单行或分散多行增长 | 同一hydrating SELECT CASE+累计window门禁生效，超界JSON不进入decoder | PG 双连接 |
| D05 | fact8/32KiB、descriptor4KiB、body16KiB、metadata union canonical4MiB/SQL16MiB；多行fact/receipt各自界内但总量/并发增长超限；profiles20/21个、capability128KiB | 实际UTF8累计门禁在同一hydration SELECT内；分块body在JSON decode前限额；profiles过多新策略unavailable而非截断；旧capability保留 | API/PG双连接/spy |
| D06 | plan canonical4MiB/SQL16MiB、响应4MiB含包装开销 | 准确边界；后算输出超限rollback新写，不声称已做工作为零 | API/PG |
| D07 | 混合confidence/NER/field列表页、缓存命中、partial marker超大JSON | 新页总预算和旧更小预算同时有效；未hydrate未授权快照，无新marker旧路径额度不变 | SQL/API |
| D08 | ORM/defer/通用serializer不慎载入snapshot/大plan/fact | instrumentation抓到并失败；先SQL stored门禁，不靠Python取出后计数 | SQL/decoder |
| D09 | 两次计划构造+渲染后第三次完整source验证及早期拒绝 | snapshot三遍合计最大192/24MiB，metadata三遍48/12MiB；第三遍同大小JSON变更且hash/size列不变也拒绝，无再排序/替换输出；producer两遍单输入单独计费，模型/外部scorer调用0 | spy/PG |
| D10 | producer原事件、batch原键恢复；历史selection读/普通队列答案读 | 前两者不取live source/confidence snapshot；历史风险无模型；正常授权答案走自身bounded读，无偷加载其他来源 | spy/API |

## E. 并发、原键恢复与响应末尾（14）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| E01 | preview无fact；摄取先提交，create后获Project fence | create计划含新事实，原expected_plan_hash冲突，零新batch | PG 双连接 |
| E02 | preview无fact；create先冻结提交，摄取后写 | 批次历史unknown保持；新preview可见fact；锁等待证据表明不存在absence旁路 | PG 双连接 |
| E03 | 同event同body两个producer请求 | 唯一一事实/receipt，另一个恢复或明确冲突后原键恢复；无重复写 | PG 双连接 |
| E04 | 不同event/不同score争同subject/profile | 一个成功另一个明确binding conflict，不能“成功返回新分数” | PG 双连接 |
| E05 | 同batch key同body与同key异body/owner竞争 | 一份batch与正确receipt；异body/owner冲突；原请求hash旧策略不变 | PG 双连接 |
| E06 | newrevision / approve/reject / reassignment在getter→ingest或preview→create间提交 | 新事实fresh拒绝或create preview_changed；旧source不被替换 | PG 双连接 |
| E07 | org/project/member/account/operation撤销先取得相关锁 | 等待方获锁后按当前身份拒绝；保存表和实际响应一起检查无泄露/新行 | PG 双连接 |
| E08 | Producer grant或descriptor在计算/渲染前撤销/换digest | 最后授权/版本检查失败并rollback；明确外部配置最后检查线性化，不宣称DB锁锁住配置 | API/barrier |
| E09 | JSON rendering最后改actor/source/proof/score/plan；snapshot同大小篡改但result_hash/size不变，或删除/增长源 | batch在两次计划构造后第三遍完整source验证，producer在初次构造后第二遍完整source验证；分别识别实际内容变化并rollback新batch/fact，原key可再次正常使用 | PG/注入 |
| E10 | NOWAIT探针在响应序列化期间检查Project fence | 真实锁仍保留；所有观察事实写入口同序Project→members→Task→Assignment→Submission→fact→batch | PG 独立探针 |
| E11 | create已提交后lost ACK / transport错误 / Native Response失败 | UNKNOWN不当作rollback或成功；原body/key保持，显式恢复得到原记录 | API/前端/浏览器 |
| E12 | source/descriptor删除、profiles发现列表为空/超20或source预算变大后batch原键恢复；当前人类权限撤销 | can_recover/owner hash独立fresh列表，合法owner恢复不查live source；当前权限撤销仍deny，无换key/重建 | PG/spy/组件 |
| E13 | 不相关IntegrityError / SQL timeout / 连接错误 | 不以宽泛catch装作幂等成功；仅allowlist约束在rollback后映射409；UNKNOWN按实际记录 | PG/单元 |
| E14 | 两个正常写路径相反顺序压力，含当前普通提交/审核 | 新路径不反向加锁或死锁；既有提交/审核权威和CAS/唯一decision保留 | PG 双连接 |

## F. UI 整流程、两语与旧兼容（15）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| F01 | Manager 原ReviewBatches capability取得有界profiles→选profile/digest/阈值→预览→创建→队列 | 复用完整页面/步骤；无规划权/Reviewer不取得profile registry；权限/版本响应末尾复验；显示覆盖/unknown/低分未覆盖和实际比例 | en-US/zh-CN |
| F02 | 每个表单字段/项目/profile/digest/身份变化，旧预览晚到 | 预览失效、generation/viewer不匹配丢弃；无旧expected_plan_hash创建 | 组件/浏览器 |
| F03 | Reviewer单审→驳回理由→原annotator新revision→另行审核→返回 | 旧修订/旧决定不变；风险不自动批准/返工；新revision不加入旧样本 | 双语端到端 |
| F04 | queue/detail/capability/list/ACK/rework viewer交叉、salt-only轮换、同角色actor/provider变化 | 新owner指纹仅作比较非授权；salt变更清旧读但同owner可保留UNKNOWN原key显式恢复；真正owner变化清intent，迟到响应不污染 | 组件 |
| F05 | unknown algorithm、新kind/schema/algorithm部分标记、坏capability类型 | 新受控分支显式校验；不能用priorityViewMatches的null/普通bounded fallback放行 | 组件/API |
| F06 | ACK key/request/plan/actor/project/owner_hash/viewer/schema/kind/algorithm缺失或不匹配 | 不确认创建；保留UNKNOWN原intent供明确恢复，不静默换body/key | 组件/浏览器 |
| F07 | 创建确认后detail或capability GET失败，或恢复读失败 | 已确认事实与读失败区分；只读重试，无重复POST；新身份清理敏感intent | 组件/浏览器 |
| F08 | 全unknown、非低但未批准、无权限、空过滤、无待审、source unavailable | 独立文案与可访问反馈；无任何“全批已批准/模型保证正确”的误导 | 双语/组件 |
| F09 | 回退/关闭比较/取消创建/筛选分页/焦点与窄屏长文本 | 保留合法导航状态、焦点可达、键盘顺序明确；取消无写；敏感数据离页清除 | 真浏览器 |
| F10 | 双击、blur、source切换、卸载后晚读/晚恢复 | 无重复命令/旧evidence残留；页面只保留导航或明确未知intent，不缓存旧授权 | 组件/浏览器 |
| F11 | 恶意文本/HTML、版本label/unknown reason超长、非法JSON scalar | 转义/严格DTO与限额，无脚本/原始答案写入日志；错误关联清晰 | 组件/API |
| F12 | 新开关关闭与各旧NER/field/普通门禁组合 | capability disabled wire不扩字段；旧列表ID/count、原key/hash、分类/NER/字段交接均保留 | PG/SQLite/组件 |
| F13 | 既有普通非批次review、省略review_batch context | 继续自身当前授权合同，不从confidence获得角色或发布能力；合法普通审核不被当旁路禁掉 | 安全回归 |
| F14 | 项目只允许Core内部读，已知Fork老链接/已批准数据/producer token | 所有新producer/priority路径deny，不伪造Core decision或外部交付 | API/token |
| F15 | trusted producer GET/POST HTTP超时与owner/source切换 | 没有人类score上传页面；机器客户端保留原event/body，owner变化不得借旧event恢复 | 协议端到端 |

## G. 工程与阶段验收（5）

| ID | 场景 | 必须证明 | 层级 |
| --- | --- | --- | --- |
| G01 | 未来实现scope/migration/旧文件和依赖检查 | 仅必要additive事实表/adapter/门禁；无旧migration、ReviewDecision、Prediction及Host改写，无新依赖/商业组件 | 源码独立review |
| G02 | 未来本地PG/SQLite/前端/build/i18n/定向lint及旧安全/三类交接回归 | 当前精确源码/fixture hash、状态和命令留档；已有lint基线不冒充新缺陷或通过 | 本地隔离 |
| G03 | 未来PR与actual merge/main | exact head/tree、review、run/job/attempt/真实checkout绑定；PR虚拟合并不当actual merge；不削弱旧CI | 远端另授权 |
| G04 | 真实producer/运维registry/启用/平台实跑 | 另授权、使用实际受控输入和producer版本；合成协议通过不等同模型执行/质量或部署 | NOT_RUN |
| G05 | #84整体收口、二次复核与保留范围 | 二次复核另行设计；#81/#84继续OPEN；无Docker/#76/其他worktree/Host/Core操作；保留库一致性UNKNOWN不被测试覆盖 | 边界审计 |

## 本次设计审查的静态要求

逐条核对：来源信任锚与当前mapped身份、descriptor digest/九项pins、原始Prediction拒绝、整数阈值及完整排序、
有界SQL hydration/最终渲染、absence竞态双序、源无关原键恢复、整条UI typed适配、旧NULL列表回归、
一修订一决定和发布权威分离。设计本身要有源文件索引、矩阵映射、相对链接、唯一ID和完整版本/未执行声明。
独立 reviewer 给出文档行号、问题/修复建议和最终精确文件hash；发现问题由唯一Writer修订后再审。

未来执行记录必须逐案例填 SUCCESS / SKIPPED（解释条件）/ NOT_RUN / UNKNOWN，并给出实际计数与证据路径；
本次 **89 条功能要求均未执行**。静态自检与独立设计审查结论仅记录在外置设计报告，不改写这份历史矩阵为功能成功。
