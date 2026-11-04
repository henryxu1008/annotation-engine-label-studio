# 审核审计下载 V1：本地执行记录

本轮仅本地开发、合成验收与独立审查；原设计、所有中间失败和平台限制记录均保留。完整逐test/JUnit映射在本轮报告ACCEPTANCE.json。

| 验收 | 结果 |
| --- | --- |
| backend-pg-closure-r3.xml | 129 PASS / 10 SKIP / 0 FAIL |
| backend-sqlite-closure-r3.xml | 115 PASS / 24 SKIP / 0 FAIL |
| ci-contracts-closure.xml | 93 PASS / 0 SKIP / 0 FAIL |
| linux-domain-tests-3.xml | 23 PASS / 0 SKIP / 0 FAIL |
| regression-pg-r2.xml | 1316 PASS / 7 SKIP / 2 FAIL |
| 前端完整prepare | 4948 PASS / 5既有SKIP；labelstudio最终42 suites/732 PASS |
| 生产构建 | SUCCESS，66.17秒；保留既有构建警告 |
| 最终原生Electron | en-US/zh-CN 2/2，36.015秒，未超300秒调用上限，服务已停 |
| 独立离线下载校验 | 实际两份ZIP的四entry、哈希、JCS、计数/gaps/edges与脱敏金丝雀通过 |
| 隔离迁移 | PostgreSQL forward/back/replay/retained拒绝5项；SQLite含namespace flush拒绝6项 |

历史regression的两个FAIL仅为Windows WinError1314符号链接权限；相同两测试已在临时原生Linux/Python3.11/PostgreSQL16补跑通过，另含21文件安全测试。原失败仍为FAIL，不改写原XML。10个POSIX分支Windows skip与SQLite的PG-only skip分别列出。

计数、字节和SQL门禁取交集。真实1000根、20/21修订及2000/2001 typed历史cohort有直接证据；不同数量的最大payload不保证同时可容纳。真实Redis传输、远端warm/cold/partial、push/PR/merge/main、部署和Host业务验收均NOT_RUN。旧25项原生调用未在本轮全部重跑，其closed inventory语义守恒，旧领域和前端回归已运行。

临时Linux发行版按精确名称/registry BasePath退役，未进入或停止Docker。辅助static_build清理曾被自动执行策略拒绝，未重试或换工具删除；生成目录保留。

| ID | 本地结论 | 实际观测及边界 |
| --- | --- | --- |
| A01 | SUCCESS_LOCAL | ordinary、分类优先及confidence冻结原candidate/selection；实际1000根完整捕获，confidence保留原协议枚举。 |
| A02 | SUCCESS_LOCAL | selected/unselected与pending、approved、rejected、superseded分列；READY不赋审核效力。 |
| A03 | SUCCESS_LOCAL | 全部可观察根assignment修订、初审及相邻差异；1/true/1.0和输入变化不误判相同。 |
| A04 | SUCCESS_LOCAL | 初审关联明确subject_observed；batch_execution_provenance和历史actor incarnation不伪造。 |
| A05 | SUCCESS_LOCAL | 真实pending/confirmed/concern/cancelled快照及no_change/revision_linked，保持supervisory_only与业务源不变。 |
| A06 | SUCCESS_LOCAL | 真实分类handoff、pending/decision/cancellation和boundary refs；不扩其他batch历史。 |
| A07 | SUCCESS_LOCAL | 真实NER、TextArea字段、bbox、动态Choices、multiText配置拒绝；没有通用export回退。 |
| A08 | SUCCESS_LOCAL | 实际#79 READY/#80 snapshot前后字节及原PROTECT不变，另有完整历史领域回归。 |
| B01 | SUCCESS_LOCAL | active scoped Manager、READ_WORK与独立audit operation同时成立，缺一拒绝。 |
| B02 | SUCCESS_LOCAL | 其他角色和legacy op直接调用collection/object/API均拒绝。 |
| B03 | SUCCESS_LOCAL | 跨Project/package与不可见creator分页过滤，最终检查包括lookahead，游标不携隐藏行。 |
| B04 | SUCCESS_LOCAL | 伪造actor/effect/time/mode及未知字段严格拒绝，身份来自认证上下文。 |
| B05 | SUCCESS_LOCAL | requester撤权后GET/HEAD/OPTIONS、条件头及Range均先授权，原生UI清链接。 |
| B06 | SUCCESS_LOCAL | creator撤权、删除、同ID账号重建及policy/instance变化不接管旧generation，墓碑不可恢复。 |
| B07 | SUCCESS_LOCAL | 冻结包在source feature/profile退休后无live图读取，独立audit开关仍拒绝。 |
| B08 | SUCCESS_LOCAL | Core/未知mode及缺provider能力失败封闭；custom collection显式unsupported，UI关闭。 |
| C01 | SUCCESS_LOCAL | preview后修订变化使捕获冲突并回滚package/binding；新preview才可新捕获。 |
| C02 | SUCCESS_LOCAL | root actor/principal/created_at/request/plan逐字段DB guard拒绝，同PK重建旧binding拒绝。 |
| C03 | SUCCESS_LOCAL | confidence namespace/profile/version等来自冻结plan，不读取当前registry或评分。 |
| C04 | SUCCESS_LOCAL | 初审proof冲突及活followup target任一九pins不一致拒绝，不补当前理由。 |
| C05 | SUCCESS_LOCAL | 两pass全私有向量比较和共享SQL/字节预算，变化拒绝并回滚，无半包。 |
| C06 | SUCCESS_LOCAL | T后新修订/删除不改变input/projection，worker/retry不读取源；preflight I/O不在写事务。 |
| C07 | SUCCESS_LOCAL | 原Python精确hash与JCS分层，大整数不由浏览器/JCS替换，公开仅opaque commitment。 |
| C08 | SUCCESS_LOCAL | Manager可脱敏审计本人行为，未赋予自审、二审cohort或发布权限。 |
| D01 | SUCCESS_LOCAL | 缺root/坏plan/同PK复活已断batch关系拒绝，不缩分母或猜来源。 |
| D02 | SUCCESS_LOCAL | exact typed receipt加独立同config哈希arity witness保留历史源与live-origin gap。 |
| D03 | SUCCESS_LOCAL | 缺type proof拒绝；同PK同payload也不能重水合已断subject，命名空间独立。 |
| D04 | SUCCESS_LOCAL | approved/rejected缺初审明确gap；矛盾proof拒绝，不说成零意见。 |
| D05 | SUCCESS_LOCAL | SET_NULL二审/初审/followup/仲裁保留验证历史，不重绑新对象；PG原terminal guard仍生效。 |
| D06 | SUCCESS_LOCAL | 冻结后删除Task/batch/review仍source-free读包；creator删除永久拒绝，历史actor仅occurrence。 |
| D07 | SUCCESS_LOCAL | Project软删全API拒绝，硬删清自身业务行并保留binding，原PK新Project旧UUID404。 |
| D08 | SUCCESS_LOCAL | 从未保存的历史不补造，known修订序号缺口/差异输入不可得显式报告。 |
| E01 | SUCCESS_LOCAL | 固定字段/枚举白名单，原文、答案、自由reason/name/model label/PII与凭据金丝雀不出公开包。 |
| E02 | SUCCESS_LOCAL | absent、present_redacted、unavailable区分，非把脱敏当无理由。 |
| E03 | SUCCESS_LOCAL | 同包ref稳定，跨包使用新secret后不关联；secret不进入冻结bundle，retry复用冻结字节。 |
| E04 | SUCCESS_LOCAL | InputBundle/projection/identity/hash/manifest受DB与reader完整性门禁，损坏不交付。 |
| E05 | SUCCESS_LOCAL | 唯一entity/edge、无dangling、candidate-source/root方程及counts/gaps严格校验。 |
| E06 | SUCCESS_LOCAL | 重复键、非有限数、weak IDs、未知schema、Unicode、大整数分层解析验证。 |
| E07 | SUCCESS_LOCAL | worker start及publish两个阶段formatter漂移均阻断；恢复原runtime后不同token产生完全相同ZIP。 |
| E08 | SUCCESS_LOCAL | 固定双语README、manifest/UI声明观察时点、gap/脱敏及非发布、非永久历史/真实性认证。 |
| F01 | SUCCESS_LOCAL | 成功/lost ACK后原key/body只恢复原receipt/input/projection，源已删除仍不重算。 |
| F02 | SUCCESS_LOCAL | project+key被原owner/instance/body保留；跨owner/instance只generic409，同owner盐轮换保留UNKNOWN原body/key。 |
| F03 | SUCCESS_LOCAL | 真实PG独立连接同key只一包/一generation；owner1/project2由相同fence内active计数限制。 |
| F04 | SUCCESS_LOCAL | queue故障/未知ACK/重复claim/retry原回执测试；真实postcommit异常不发rejected并可原key恢复。 |
| F05 | SUCCESS_LOCAL | queued30m和running31m独立时钟；新token fence旧worker，显式retry不重捕获。 |
| F06 | SUCCESS_LOCAL | start/publish双creator/policy/runtime/input门禁，撤权/墓碑不READY，不自动重新授权。 |
| F07 | SUCCESS_LOCAL | 真实PG三attempt竞争、READY保留scratch lease，未知publish不删潜在成品，旧writer只清自身。 |
| F08 | SUCCESS_LOCAL | PG Project实例/QuerySet、Task实例/QuerySet/unlink、User实例/QuerySet与capture真实blocking证据；内部Assignment/Submission源行锁另验。 |
| G01 | SUCCESS_LOCAL | READY GET/HEAD/singleRange先完整inventory/hash/同FD与双authority，实际浏览器ZIP下载通过。 |
| G02 | SUCCESS_LOCAL | 旧ETag/Range/HEAD/OPTIONS等均先当前权限，无size/部分内容泄漏。 |
| G03 | SUCCESS_LOCAL | 原生Linux symlink/祖先/containment及Windows junction/DACL负例，两平台证据分别保留。 |
| G04 | SUCCESS_LOCAL | 真实独立writer在hash后首字节前改同inode拒绝；stream后授权OS写入明确属于不可变发布前提之外。 |
| G05 | SUCCESS_LOCAL | 错误bytes/hash/attempt/storage pointer与ZIP inventory不交付并quarantine，原key不重建。 |
| G06 | SUCCESS_LOCAL | 复用保留的private-root reservation，LocalFiles/import/storage等旁路受原负例覆盖，audit关闭不撤reservation。 |
| G07 | SUCCESS_LOCAL | invalid/multiple/out-of-bounds Range拒绝；同FD读、退出及异常关闭，不全文件buffer。 |
| G08 | SUCCESS_LOCAL | allow event写失败不出byte，deny尽力记录；worker/provider原始异常转固定码，PII不进公共错误。 |
| H01 | SUCCESS_LOCAL | 真实短读流与BytesIO均支持16KiB等界、超1byte拒绝，重复query/weak IDs/额外字段拒绝。 |
| H02 | SUCCESS_LOCAL | 每个纳入revision静态单Text证明，旧receipt缺arity不能凭当前XML补证。 |
| H03 | SUCCESS_LOCAL | 真实1000根完整捕获/1001 producer拒绝、实际20修订完整/21拒绝；总修订门禁与字节门禁取交集。 |
| H04 | SUCCESS_LOCAL | 实际5000/5001 entity、10000/10001 edge门禁，SQL typed历史ledger的2000 boundary正例/2001拒绝，无validator mocks。 |
| H05 | SUCCESS_LOCAL | SQL stored CASE在decoder前，单snapshot/整个pass union及plan限制；chunk1/250均不能重置union预算。 |
| H06 | SUCCESS_LOCAL | 两pass含元数据/COUNT/ID/scalar/锁查询的实际shared Budget，实测等界与少1byte拒绝，不按hash免费。 |
| H07 | SUCCESS_LOCAL | 输入/公开/ZIP/scratch限制同时成立；CappedWriter等界/超界及footer、真实超限不READY，三writer不超两slot。 |
| H08 | SUCCESS_LOCAL | page/cursor/response/active/SQL/chunk均有限；实际1000根无N+1；共享authority固定基线单列。 |
| I01 | SUCCESS_LOCAL | 既有batch详情内一条preview→明确确认→history→download流程，两语原生验收。 |
| I02 | SUCCESS_LOCAL | context/owner/generation隔离，迟到capability/JSON/blob不显示或保存旧结果。 |
| I03 | SUCCESS_LOCAL | lost ACK只原body/key恢复，读失败不写；服务端确定precommit拒绝与真实postcommit UNKNOWN分别覆盖。 |
| I04 | SUCCESS_LOCAL | 两语真实trusted native Enter/Space/Tab、disable前焦点转移和403卸载外部标题，异步不抢用户焦点。 |
| I05 | SUCCESS_LOCAL | count/gap/sourceUnavailable/quarantine/403有界DTO与状态明确，失权无链接或raw fallback。 |
| I06 | SUCCESS_LOCAL | 实际下载固定UUID文件名、MIME/大小/SHA检查；异常/迟到/无效blob不保存。 |
| I07 | SUCCESS_LOCAL | 全量历史领域回归及全量前端prepare，新功能未修改旧25 browser calls；本轮不冒充旧25项原生重跑。 |
| I08 | SUCCESS_LOCAL | CI合同原25calls摘要完全守恒，独立review-audit call300s/15s，max3/原required不变；远端另行授权。 |
| J01 | SUCCESS_LOCAL | 双DB ORM/QuerySet/directSQL冻结字段、JSON弱类型、generation/tombstone及固定error code guard。 |
| J02 | SUCCESS_LOCAL | 直接READY/不完整READY/改token拒绝；合法READY只当前token原子publish。 |
| J03 | SUCCESS_LOCAL | 双DB直接删冻结行拒绝，Project清业务行保留registry；SQLite整namespace DELETE flush拒绝，不关guard/FK。 |
| J04 | SUCCESS_LOCAL | 双DB隔离forward/back/replay、已有READY和registry危险rollback拒绝，原namespace保留。 |
| J05 | SUCCESS_LOCAL | 共享checked_file提取兼容，真实#79/#80及旧权限/当前源/发布回归，两个Windows symlink限制用原生Linux补跑。 |
| J06 | SUCCESS_LOCAL | public Project/Task/User/Org root coordinator与锁序；Assignment DELETE是cancel、Submission无DELETE；unknown provider fail-closed。 |
| J07 | SUCCESS_LOCAL | 实际浏览器两ZIP用独立stdlib/JCS校验四entry、hash/count/gap/edge/canary，不声称Host接收或历史真实性。 |
| J08 | SUCCESS_LOCAL | 最终本地commit/tree、独立review、工作区/refs/旧证据保留核验；未授权远端/部署保持NOT_RUN。 |

此处记录本地合同与测试观察。最终独立审查结论和准确commit/tree以本轮封存报告为准；授权边界不因本地通过而推进。
