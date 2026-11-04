# #84 审核审计结果下载 V1：80 项验收矩阵

基线 `78b6c9a0cfd0f85a69300d31276aa0d1daeed480`；对应[设计](review-audit-download-v1-design.md)。
**本地实施验收记录在收口时逐项更新。** 原设计副本单独封存；既有 #134/main CI 不替代新功能证据。
逐项测试及平台边界见[本地执行记录](review-audit-download-v1-local-acceptance.md)。远端CI、部署和Host验收保持NOT_RUN。
每个负例同时检查无新增包/不变的冻结行与源状态、无部分元数据/文件、合理错误及无敏感日志。
PG 并发必须真实独立连接与受控 barrier；SQLite skip 不能算 PG 通过。

## A. 效力、范围与完整关系图（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| A01 | ordinary / 分类优先 / confidence 三类静态分类批次 | 原规则/seed/quota/分母/selection与全部根candidate恰好一次，未选/未覆盖分列 | API/包解析 | SUCCESS_LOCAL |
| A02 | selected/unselected各含pending/approved/rejected/superseded | 批次coverage与source状态分列，无隐式Approved；READY只指文件 | API/UI | SUCCESS_LOCAL |
| A03 | 根assignment早期+原批次source+后继修订与每次初审 | 全部有界修订/意见/相邻差异完整；旧结果/审批字节不变 | PG/SQLite | SUCCESS_LOCAL |
| A04 | 同subject初审来自ordinary queue/另一个batch | association=subject_observed，batch执行归属not_recorded | API/包 | SUCCESS_LOCAL |
| A05 | 二审pending/confirmed/concern/cancelled及两种followup | 精确root/subject/proof绑定、effect固定supervisory_only，无新写命令 | DB/API | SUCCESS_LOCAL |
| A06 | 本batch分类handoff及仲裁pending/decision/cancel | 直接关系完整；外部cohort只作boundary ref，其他batch不扩图 | DB/包 | SUCCESS_LOCAL |
| A07 | 任一可用源是NER/字段/bbox/动态Choices/多text | 明确unsupported，无部分静态包；缺类型proof按H02拒绝 | parser/API | SUCCESS_LOCAL |
| A08 | #79 READY/#80 Manifest/legacy release与concern效力对照 | 包前后原hash/状态/权限/发布资格全不变，无Host/Gold写入 | 真实PG/#79/#80回归 | SUCCESS_LOCAL |

## B. 当前权限、身份与所有入口（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| B01 | active scoped Manager+READ_WORK+显式audit op | 可规划/创建/读；缺任一条件默认deny | API矩阵 | SUCCESS_LOCAL |
| B02 | Annotator/Reviewer/org admin/unscoped superuser/legacy未赠op | 所有collection/preview/command/detail/manifest/download不可绕过 | 直接API | SUCCESS_LOCAL |
| B03 | 替换org/project/batch/package/artifact IDs与跨范围cursor | 无存在/计数/内容泄漏，collection与object gate一致 | API负例 | SUCCESS_LOCAL |
| B04 | 客户端伪造actor/reviewer/effect/time/mode/Host字段 | 严格拒绝，服务端trusted mapper唯一身份来源 | parser/API | SUCCESS_LOCAL |
| B05 | stale浏览器在requester移除成员/disable/op撤销后重试 | list/detail/content/HEAD/Range/304/416/OPTIONS均先拒绝 | API/UI | SUCCESS_LOCAL |
| B06 | creator撤权/删除而requester仍有权、同ID新账户、policy/instance变更 | 双方gate及generation/tombstone/version生效；旧generation永久拒绝，所有原key/后台/metadata不可接管；仅未删除账户可恢复权限 | API/后台 | SUCCESS_LOCAL |
| B07 | source功能/producer grant退休但own audit policy仍合法 | 旧冻结包不读live源或refresh来源；audit开关关闭仍全拒绝 | SQL捕获/API | SUCCESS_LOCAL |
| B08 | Core-only/未知mode/无background hook/无private root/queue | fail-closed，无同步/通用export回退，不改变旧模式 | API/config | SUCCESS_LOCAL |

## C. 冻结时点和版本（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| C01 | P后、T前新增修订/意见/处置、preview signing key轮换 | opaque HMAC冲突，不产生包；未变源可重验，新preview才可创建新T | PG/SQLite | SUCCESS_LOCAL |
| C02 | 固定root ID但plan/creator/created_at/request绑定变化 | batch_binding拒绝，不能用旧ID的当前对象填历史 | API/DB | SUCCESS_LOCAL |
| C03 | 同名profile/model但版本/digest/producer不同 | 原namespace/descriptor/algorithm/receipt精确冻结，不重新评分 | confidence回归 | SUCCESS_LOCAL |
| C04 | 初审可观察proof与冻结二审/concern proof冲突 | SOURCE_INTEGRITY_INVALID，无“用当前reason修历史” | corruption负例 | SUCCESS_LOCAL |
| C05 | 两pass间成员集合/边/标量/hash/渲染数字类型变化 | 共享Budget的完整二次验证拒绝并回滚，无半记录/answers | 故障注入 | SUCCESS_LOCAL |
| C06 | T后worker前继续审核/返工/仲裁/改名 | 包仅固定T，不重新读取这些业务源，计数/摘要稳定 | SQL/worker | SUCCESS_LOCAL |
| C07 | 时间、rule/schema、canonicalization来源与原大整数 | 精确私有pin不被JCS/浏览器舍入替换；公开不暴露raw低熵hash | 跨语言向量 | SUCCESS_LOCAL |
| C08 | actor当过作者/Reviewer但当前显式audit权限合法 | 仅固定脱敏审计读取；原自审/二审cohort与写权限不变 | API权限回归 | SUCCESS_LOCAL |

## D. 缺源、删除和历史诚实性（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| D01 | root batch缺失/plan损坏/完整universe不可建立 | 新capture拒绝，不缩分母、不guess范围 | API/DB | SUCCESS_LOCAL |
| D02 | candidate已缺失但有exact typed frozen proof | 根位置保留、retained历史record+live-origin gap，不借latest | API/包 | SUCCESS_LOCAL |
| D03 | 缺源且无类型/身份proof或ambiguous numeric ID绑定 | INPUT_SCOPE_UNPROVABLE/identity拒绝；关系证据不足明确gap | API/ID复用 | SUCCESS_LOCAL |
| D04 | Submission approved/rejected但初审缺失、pending存在矛盾意见 | 缺初审为gap；矛盾/损坏拒绝；均不是“未审/零意见” | DB/API | SUCCESS_LOCAL |
| D05 | SET_NULL二审/handoff/followup与完整receipt仍在 | 历史绑定通过独立typed proof，不因FKnull全部丢弃、不重绑定 | PG/SQLite | SUCCESS_LOCAL |
| D06 | T后删batch/task/assignment/submission/其他source actor/原ReviewDecision | exact包按当前audit权限读取；零源查询；creator User删除例外永久拒绝；历史actor无代际proof只按occurrence关联 | SQL/下载 | SUCCESS_LOCAL |
| D07 | Project软删/硬删/同ID重建 | 软删全入口拒绝；硬删清自身业务行、旧UUID404、无新PROTECT；既有#79/#80约束不改 | PG/SQLite/HTTP | SUCCESS_LOCAL |
| D08 | 历史事件T前已消失且从未保存、当前latest出现 | completeness不夸称所有历史；不从当前对象补造时间/理由/执行归属 | 包schema/负例 | SUCCESS_LOCAL |

## E. 脱敏、包schema与完整性（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| E01 | 自由reason/name/model/producer/profile标签/PII/任务原文/答案/媒体/secret混入每字段 | public ZIP/manifest/headers/log无这些值或raw低熵hash，版本仅固定协议白名单 | allowlist扫描/金丝雀 | SUCCESS_LOCAL |
| E02 | reason为空/有内容/不可得 | absent/present_redacted/unavailable严格区分，脱敏不是“没有理由” | 包/两语UI | SUCCESS_LOCAL |
| E03 | 同源同包、同源不同包、事务回滚/重试 | 同包opaque ref一致，跨包不同；T后secret丢弃但重试字节不变 | HMAC/worker | SUCCESS_LOCAL |
| E04 | manifest/public records/private bundle/source graph任一改bytes | 对应SHA/DB guard/typed binding拒绝，public不泄私有bundle | DB/文件负例 | SUCCESS_LOCAL |
| E05 | 重复/缺失entity、edge dangling、candidate少一项、boundary无限展开 | 固定闭包/唯一键/计数方程拒绝，无静默truncate | parser/图边界 | SUCCESS_LOCAL |
| E06 | JSON重复键/非有限数/弱类型/未知schema/Unicode/超大整数 | strict parser、JCS与原精确snapshot hash分层正确 | Python/Node向量 | SUCCESS_LOCAL |
| E07 | 同T不同token/启动时间、部署改变formatter/Python/依赖/README | 冻结audit-zip-format/v1+compat digest+README字节；匹配环境字节确定，不匹配start/publish blocked_runtime，原兼容环境显式retry才恢复 | worker/部署漂移注入 | SUCCESS_LOCAL |
| E08 | 把脱敏包当原文复本/永久审计/Host版本 | manifest/README/UI明示范围/gap/redaction/观察时点/非发布效力 | schema/文案review | SUCCESS_LOCAL |

## F. 幂等、后台worker和并发（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| F01 | same owner/key/body成功或lost ACK后精确恢复 | 原receipt/input/projection，零live图/producer查询 | SQL/HTTP | SUCCESS_LOCAL |
| F02 | same key改body/root/profile/instance/owner | conflict；viewer salt轮换同owner仍保留原命令 | API/UI | SUCCESS_LOCAL |
| F03 | PG双连接同key同body；不同key同actor、首次binding并发 | 同key唯一package/active actor generation；owner1/project2限额，锁User后binding，整事务一致 | PG barrier | SUCCESS_LOCAL |
| F04 | queue不可用/提交后enqueue未知/重复RQ delivery/retry命令ACK丢失 | dispatch_pending/queued可恢复，retry独立namespace原回执恢复，无重复claim/推进，无credential进job | queue故障注入 | SUCCESS_LOCAL |
| F05 | queued 30m从最新enqueue计时、running31m回收 | 新token fence旧worker；只格式化同T，无重捕获 | 时间/并发 | SUCCESS_LOCAL |
| F06 | worker start/publish前creator撤权/删除、policy/formatter改动、input损坏 | 无READY/字节，blocked或finalerror；tombstone永久拒绝，不从数字ID恢复creator；无自动重新授权 | PG/worker | SUCCESS_LOCAL |
| F07 | winner提交ACK丢失、stale worker清理、artifact已发布 | 不删除潜在READY；scratch只属旧token，无覆盖 | I/O/DB fault | SUCCESS_LOCAL |
| F08 | capture/Project/单Task/批Task/cascade/User删除、同ID重建真实竞争 | public删除Collector前Project→User→binding；DB原子tombstone；before/after或整事务rollback，无半包/代际复活 | PG双连接+SQLite | SUCCESS_LOCAL |

## G. 文件下载与所有旁路（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| G01 | READY ZIP/manifest合法GET/HEAD/singleRange | 当前双authority、完整inventory/同FD/hash通过才首字节 | HTTP/文件 | SUCCESS_LOCAL |
| G02 | revoke后旧ETag/If-Range/HEAD/OPTIONS/304/416/Range | 先授权，无size/metadata/部分byte泄漏 | HTTP负例 | SUCCESS_LOCAL |
| G03 | path traversal/叶子symlink/祖先symlink或reparse/root替换 | containment+实际descriptor失败封闭，无外部文件 | nativeLinux/Windows | SUCCESS_LOCAL |
| G04 | 首字节前同inode篡改/截断/替换、私有root不安全；stream后OS授权写入 | 前者可检测篡改拒绝、不安全root不支持启用；后者属不可变发布前提破坏，不保证无错误字节/撤回；同FD不被当成inode快照 | 双进程I/O/部署前提审查 | SUCCESS_LOCAL |
| G05 | artifact跨package/attempt、漏/多文件、size/hash/ptr漂移 | quarantine，不做旧key重建，不给部分内容 | 文件/DB | SUCCESS_LOCAL |
| G06 | LocalFiles/存储配置/直接blob/导入scan/静态/Nginx/signedURL | audit subtree关闭旁路，开关关闭也保留reservation | 全surface负例 | SUCCESS_LOCAL |
| G07 | invalid/multiple Range、FD异常/用户取消/流结束 | 规范416或拒绝，FD关闭，server不buffer全文件 | HTTP/资源 | SUCCESS_LOCAL |
| G08 | 允许access event写失败/拒绝audit写失败/敏感日志 | allow失败无字节，deny尽力记录不反向允许，日志无正文凭据 | fault/privacy | SUCCESS_LOCAL |

## H. 输入、计数和字节硬边界（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| H01 | raw body16KiB/重复query/short-read/chunked/falseID/extra keys | 精确界值/超界，preparser gate，无隐藏参数扩权 | parser/HTTP | SUCCESS_LOCAL |
| H02 | 全部可用根/lineage静态证明、缺类型proof、当前XML变化 | 每根exactproof；不能用当前XML替代历史，无证明则整包拒绝 | API/旧源 | SUCCESS_LOCAL |
| H03 | root1000/1001、lineage2000/2001、每assignment20/21 | 计数/字节/SQL门禁交集内支持，超任一界整包拒绝，无自动拆小掩盖完整性 | DB/图 | SUCCESS_LOCAL |
| H04 | graph5000/5001、edges10000/10001、cohort2000/2001 | 预检拒绝，不能先无界物化或递归 | DB计数/内存 | SUCCESS_LOCAL |
| H05 | snapshot256KiB/union512KiB/storedrow2MiB、plan4/16MiB | 各原门禁及共享累计同时成立，异常不fallback | SQL CASE/decoder | SUCCESS_LOCAL |
| H06 | 两pass总canonical8MiB/stored32MiB，含reason/scalars/converters | 实际SQL bytes预算与变化后的hydration gate都有效，非按hash去重免费 | PG/SQLite故障注入 | SUCCESS_LOCAL |
| H07 | frozen/public各8MiB，ZIP16MiB，scratch32MiB，4文件 | 界值/超界都验证，超限不READY，不截断record | 文件/worker | SUCCESS_LOCAL |
| H08 | response/page/count/cursor/active/worker限额、DB坏类型、source SQL128/256上下界 | 每wrapper实际字节计入；chunk≤250，超SQL预算下一查询前拒绝，无N+1/全Project展开；共享authority基线另列 | API/SQL计数 | SUCCESS_LOCAL |

## I. 页面、两语和原生时序（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| I01 | 批次详情→说明→preview→确认→history→下载 | 一条连贯既有流程，完整/gap/redacted/T/非发布标签明确 | en-US/zh-CN E2E | SUCCESS_LOCAL |
| I02 | 切project/batch/owner、迟到preview/下载响应 | generation与owner清理缓存，旧结果不显示/不保存 | component/E2E | SUCCESS_LOCAL |
| I03 | create lost ACK、read失败、显式retry/refresh | UNKNOWN只原body/key恢复；读失败不写、不重复创建 | UI/API | SUCCESS_LOCAL |
| I04 | Tab/Enter/Space、禁用/卸载、返回、用户主动改焦点 | 原生激活正确，disable前迁移焦点，异步不抢焦点 | 原生浏览器两语 | SUCCESS_LOCAL |
| I05 | count/gap状态、sourceUnavailable、quarantine/403 | 无成功假象，无自动转approved/raw导出，失权清链接 | UI/负例 | SUCCESS_LOCAL |
| I06 | 下载filename/header/servererror/取消/无效DTO | 固定安全文件名、错误无敏感信息、无未知body保存 | HTTP/浏览器 | SUCCESS_LOCAL |
| I07 | 现有审核/返工/三类handoff/二审/concern/#79/#80 | 原行为/hash/权限/快捷键/返回焦点不变 | 既有完整回归 | SUCCESS_LOCAL |
| I08 | 新覆盖预算与CI closed inventory | 原测试守恒、明确新call/shard预算，原300s/15s/max3/required不放宽 | CI合同/未来远端 | SUCCESS_LOCAL |

## J. DB guards、恢复和阶段边界（8）

| ID | 场景 | 必须观测 | 未来验证 | 状态 |
| --- | --- | --- | --- | --- |
| J01 | ORM/bulk/directSQL改InputBundle/projection/identity/hash/JSON类型/generation/tombstone | PG/SQLite拒绝；source便利FK仅null；actor binding仅User删除原子tombstone，不可复活/改generation/删墓碑，无GET隐式建binding | 双DB guards | SUCCESS_LOCAL |
| J02 | 直接READY insert、不全READY、改sealedfile/manifest/token | 拒绝；合法READY只正确token原子publish，quarantine不可回READY | DB/并发 | SUCCESS_LOCAL |
| J03 | parent仍在直接删冻结行，Project硬删/cascade，含永久binding的SQLite全库flush | 直接删拒绝；Project业务行按DB-owned cleanup、保留binding/墓碑；现存audit namespace的SQLite全库DELETE flush预期拒绝，不关guard/FK；测试用rollback或新专用DB；不解除旧#79/#80PROTECT | 双DB/migration | SUCCESS_LOCAL |
| J04 | forward/back/replay、已有READY时降级、restore/clone重用namespace | guard和schema稳定；危险降级/异主namespace拒绝，未启动保留环境 | 隔离migration | SUCCESS_LOCAL |
| J05 | helper提取与已批准export/snapshot领域兼容 | 原fork_approved/current-source/creator/download权限未放宽；auditmode独立 | #79/#80回归 | SUCCESS_LOCAL |
| J06 | 锁顺序/共享Project授权上下文/provider兼容 | public删除路径清单逐个验证，源/authority一致，scope未知默认deny | PG/provider | SUCCESS_LOCAL |
| J07 | 原包内容校验与导出后外部校验 | manifest/payload/ZIP哈希可验证；不宣称Host接收、长期保留或历史真实性 | 独立离线验证 | SUCCESS_LOCAL |
| J08 | 本地验收/独立review/push/PR/实际merge/main/部署分阶段 | 无权阶段保持NOT_RUN；不改#81/#84、Docker76、主仓/其他worktree | 证据与边界审计 | SUCCESS_LOCAL |

## 设计保留及实施澄清

2026-11-04实施澄清：J03原“与flush”措辞和J01/设计第6节永久墓碑不变量冲突，已按该不变量明确全库DELETE拒绝。
原设计hash/独立审查与完整原文副本保留在设计阶段报告；此处不放宽产品删除权限，不把PG特权TRUNCATE当正常清理或DELETE guard验证。

设计阶段只做了80个ID唯一/计数、引用路径、source-map锚点、权限语义及独立设计审查；当时全部功能NOT_RUN的原文仍保留。
本轮已获实施授权，执行PG/SQLite、原生Linux/Windows文件负例、两语Electron及独立复审。
各计数和字节限制同时成立，最大count不承诺任意最大payload同时容纳。H03的2000总修订上限有低配实库等界/超界与共享预算证明，实际1000根及20/21 lineage另有直接全图证据。
H04的2000/2001 cohort用真实SQL中typed历史ledger逐个通过既有validator，验证完整边界union；不把合成历史receipt视为历史真实性认证。
F08/J06中Project/Task/User/Org是既有公开物理删除根；Assignment API DELETE为cancel，Submission无DELETE入口。内部ORM Assignment/Submission删除验收证明源行锁与完整冻结的before/after，不新增公开硬删权限。
本地浏览器显式synthetic queue，真实Redis传输未运行；旧25项browser调用清单守恒，全量旧后端/前端回归已运行，不冒称旧25个原生调用全部重跑。远端warm/cold/partial和部署仍NOT_RUN。
