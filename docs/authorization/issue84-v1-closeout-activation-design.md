# #84 受限 V1 收口与受控启用验收设计

2026-11-04。**本轮仅设计，不开发、不执行隔离验收、不实际启用。**
基线 `main@d814ab63f205fb2ef4ecff007525c836e928bb82`，完整 tree
`c4af2313a80360487522afa6b634c1bf692c4f07`。Fork-only；#81/#84 OPEN；
Docker/#76、主仓及原有 worktree 不动。

配套：[当前收口索引](issue84-v1-closeout-current-state.md)、
[验收矩阵](issue84-v1-closeout-activation-acceptance.md)、
[机器可读矩阵](issue84-v1-closeout-activation-matrix.json)。

## 1. 范围、效力与阶段

仅已有 inline 单 Text + static Choices 的静态文本分类；二审和 concern 跟进效力固定
`supervisory_only`；审计只用 `review-audit-redacted/v1`、`audit-zip-format/v1`。
复用 ReviewBatch、不可变 Submission、原初审/返工比较、分类冻结 cohort 交接、现有导出/快照和私有文件 helper。
不新增产品 API、schema、权限、设置项、UI、队列服务或调度器，不放宽 #79/#80 的领域 guard。

| 阶段 | 可获得的结论 | 当前状态 / 另需的权限 |
|---|---|---|
| D：设计与独立 review | 规范及可追溯矩阵充分；可指出执行前置缺口 | 本轮授权；本轮静态结果与所有运行结果分开 |
| E：隔离合成验收 | 选定代码与配置在自有合成环境的协议、真实队列传输、权限、冻结/恢复行为；69项E案例 | NOT_RUN；须另授权具体环境/资源和运行。启动配置桥接/fixture/harness 若需新代码，先另获实现及审查授权 |
| P：真实 provider/producer 准备 | L03/L04两个关口：选定组件的非测试权限映射；真实低置信度业务额外需要producer版本/校准 | UNKNOWN/NOT_RUN；不拿测试 adapter 或合成分数代替；缺真实producer只阻断真实低置信度使用，不自动阻断另行获准的独立二审/audit组件 |
| O：真实启用/部署与业务验收 | 被明确批准实例/项目中的运行和业务结果 | NOT_RUN；与 E、代码 merge 分别授权。本轮不接触 Host/Core 实例 |

E 通过最多称“受限 Fork V1 隔离合成验收通过”。真实 producer 不在 E 的合成分数中完成质量验收。
全量 #84、#81、更广模态、Host 业务链和 Issue 关闭均不由 D/E 自动完成。
矩阵用 `acceptance_phase` / `blocking_for` 明确69 E、2 P、1 O。L06是O授权关口；
E汇总只看获准E范围，不把P/O待满足关口算E失败或E已通过。任一真实组件启用仍需其适用P前置与单独O授权；
仅局部组件准备成立时必须按该组件报告，不能称完整业务V1已经就绪。

## 2. 已有架构约束与准入结论

以下按现行源码，而非历史设计标题推导：

1. `access_control/project_operations.py` 唯一非测试 Legacy provider 不授予
   `PROVIDE_REVIEW_CONFIDENCE`、`ORGANIZE_SECOND_REVIEW`、`SECOND_REVIEW`、
   `RECORD_SECOND_REVIEW_FOLLOWUP`、`EXPORT_REVIEW_AUDIT`。当前新能力的其他授予实例是 synthetic seeder。
   真实启用的权威所有者必须交付受信 provider、版本与项目 allowlist 并接受审查；此依赖没有满足前不得实际启用。
   `ProjectOperationPolicy` 是 project 级 operation 集，不能伪装成已具备逐人 IAM 配置器。
2. `policy.state='paused'` 本身不会禁止操作：`allows()` 只检查 `allowed_operations`。
   停用须实际撤销所需 op、关闭相应 gate，并证明进程配置收敛；角色、账户、组织和成员仍由原服务端逻辑检查。
3. 完整审计浏览在 `review_audit.collection_supported()` 只允许 **exact LocalIdentityProvider**。
   custom identity 即使有 `resolve_background` 也不建立 SQL-visible 全 creator 集合。E 完整流使用内置 local identity；
   custom/Host 只做受限否定契约，不声称联通。生产背景解析和集合适配缺口单独设计。
4. `seed_review_audit_e2e.py` mock `queue_available` / `api_queue` 并在本进程调用 `generate`。
   旧 CI 证明冻结/格式化/HTTP 及 synthetic 场景，不证明 web→Redis→独立 RQ worker 传输。
   E 必须有不 mock 的独立启动配置桥接与传输证据；现有 seeder 不能直接当 G01 的通过脚本。
5. 审计复用 `approved_exports` queue alias、`APPROVED_EXPORT_REDIS_URL` 和 `APPROVED_EXPORT_ROOT`。
   本设计不发明 audit-only queue/root 开关；E 用全新独占 Redis 实例与私有 root，#79/#80 合成共存也只在此 namespace 内。
   暂停共享 queue/worker 不是线上 audit-only 停用措施。
6. 配置在 Python 模块导入时读取，没有分布式原子开关。HTTP 下载在最终 recheck 后交出已校验 FD，
   现有 Range/File response 不逐 chunk 重新鉴权。停用控制必须区分新请求、已提交命令、已打开的流和已交付字节。
7. 现行 audit preview/viewer 使用 Django `SECRET_KEY` 的 salted HMAC；history cursor 也用 Django signing。
   **没有独立 audit signing-key 设置项**。历史审计设计中“独立 preview signing key”的措辞不能作为现行部署能力。
   本轮不读、生成或轮换任何 secret。记录运维 secret 引用/版本，不把值写入文档、命令、截图或公开日志。
8. 审计 policy 的 instance/version/profile/limits 与包内策略精确匹配；减限或换 version 也可能拒绝旧包访问。
   formatter 指纹还绑定 Python patch、stdlib zipfile、formatter、RFC8785/jsonschema、README/ZIP设置。
   配置回退必须恢复完整已批准配置和兼容运行环境，不改冻结包，不改 UUID，不把改 key 当 namespace 重置。
9. 没有 audit cancel/reopen API。READY、quarantined、failed_final 不能 retry；二审 cancel 占永久 subject 槽。
   二审 flag/mode/instance 关闭后 SECURITY_CANCEL 也无法越过 gate，不能安排为“关开关之后再取消”的固定步骤。
10. 初审表不是永久冻结事件账本。审计包只冻结 T 时可观察/可证明的有界图；缺失与未记录归属明确 gap。
    Project 删除后的私有孤儿文件无公开访问路径，但不是物理擦除承诺。长期保留/擦除属于另行政策设计。

这些是必须如实验证的架构边界。需要新产品控制的要求留在后续单独设计；不以文档暗含当前已有 kill-switch、
通用 Host adapter、持久返工调度或永久审计平台。

## 3. 依赖与配置清单

所有项先在将来的隔离环境证明资源所有权，再由其操作人设置；本轮不提供可直接执行的启用脚本。
配置清单只存 public 标识、digest、非敏感路径和 secret reference，不存 URL 凭据、会话或密钥。

| 依赖 | 现行入口 / 约束 | E 的准入要求 |
|---|---|---|
| 精确代码与 DB schema | 本基线 tree；现有 requirements/lockfiles 和 migrations | 记录 source/tree、迁移计划/结果；禁止对保留 DB migrate 或 downgrade |
| Python/库/formatter | 原 pyproject/poetry.lock；audit `formatter()` | 锁定并记录 web/worker 实际指纹；恢复原 formatter 才能重试 blocked_runtime |
| 基础抽审/任务包 | `ANNOTATION_REVIEW_BATCHES_ENABLED`、`ANNOTATION_REVIEW_BATCH_MODES`；任务包场景另需 `ANNOTATION_FIXED_PACKAGES_ENABLED` | 仅 allowlisted org:project 的 fork_review；缺失/未知/Core-only 拒绝新 batch 路径 |
| 分类冲突/仲裁场景 | 原 classification disagreement/batch/arbitration/handoff 的各 enabled/modes 设置与 op | 只启用本场景必需项；原 pending、独立 cohort、exact input guards 保持 |
| 低置信度 | `ANNOTATION_REVIEW_CONFIDENCE_ENABLED`、`...INSTANCE_ID`、`...PROFILES`、`...GRANTS` | operator 可信 namespace/actor/profile digest；模型工件/evaluator/calibration 版本为断言来源，合成事实不称质量证明 |
| 二审 | `ANNOTATION_SECOND_REVIEW_ENABLED`、`...INSTANCE_ID`、`...MODES` | 稳定 canonical UUID；独立 Reviewer；current READ_WORK 和专用 organize/decide op |
| concern 跟进 | `ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED` + 二审 gate | 独立 Manager 和 record op；一次 no_change 或合法 revision_linked；不自动仲裁 |
| 审计 | `ANNOTATION_REVIEW_AUDIT_ENABLED`、`ANNOTATION_REVIEW_AUDIT_POLICY`、可收紧的 `...LIMITS` | policy.instance_id、projects[org:project]={mode,version}；固定 profile/format；不扩大源码硬上限 |
| 身份与操作映射 | `ANNOTATION_IDENTITY_PROVIDER`、`ANNOTATION_PROJECT_OPERATION_PROVIDER` | E 完整路径用 exact local identity；provider 有明确 synthetic scope 或经审查的非测试交付；两者状态分别报告 |
| Redis/RQ | `APPROVED_EXPORT_REDIS_URL`，alias `approved_exports`；`approved.api_queue` | 全新独占 Redis 实例，真实 ping/enqueue/dequeue；独立 worker；不得指向任何保留 Redis/Docker 服务 |
| 文件 | `BASE_DATA_DIR`、`APPROVED_EXPORT_ROOT`、独立 static/temp/report 目录 | owner manifest、canonical path、无 protected 路径重叠和 symlink/junction 越界；保留 private-root reservation |
| 配置传播 | Django import-time settings；web/worker 均独立进程 | 每个 PID/启动代次、source、非敏感配置 digest、secret-reference version、DB/Redis/root 指纹匹配后才进入验证 |

基础 batch flag 不是所有新功能的总开关：必须逐组件验证实际 gate；不能假设关闭 batch 会关闭独立 audit，
也不能把打开 followup 当成自动具备 second-review mode/identity。关闭/恢复按整套显式清单执行。

## 4. 权限映射与独立性

角色、成员、principal→actor 和身份source从已认证服务端取得，不能采用客户端伪造的actor/reviewer/effect/time/mode。
新严格接口按其schema拒绝额外字段；旧普通初审serializer可忽略未知字段，但只能使用验证后的decision/reason与服务端actor。
验收不要求为旧兼容接口新增严格parser或其他业务代码。
org admin、superuser、拥有 READ_WORK 或旧 release 权限，都不自动等于 scoped Manager 或获得新 op。

| 行为 | 角色/范围与必要条件 | 必验负例 |
|---|---|---|
| 标注/返工 | 原 active assignment 的 Annotator；沿用 EDIT/SUBMIT 等既有规则 | 改派/取消不复活；不改旧 Submission/意见；不新增任务授权 |
| 批次组织 | 当前 scoped Manager + READ_WORK + ORGANIZE_REVIEW；显式 feature/mode | 假 Manager、其他项目/组织、移除成员、缺 op、换 request actor |
| 初审 | 原 Reviewer + READ_WORK/REVIEW，pending exact revision 与非自审 | selected 不授予角色；普通审核入口也不能旁路原权限/发布 |
| 可信置信度输入 | current scoped Manager + READ_WORK + PROVIDE_REVIEW_CONFIDENCE，可信 grant/namespace/profile；非提交人 | Prediction/导入分数不能代替；retired grant 不重写历史；不能伪造 model/actor |
| 二审组织 | Manager + READ_WORK + ORGANIZE_SECOND_REVIEW | 新鲜 dispatch 需合法 latest 初审来源，完整排除集；组织权不是决定权 |
| 二审意见 | 被分派的独立 Reviewer + READ_WORK + SECOND_REVIEW | Manager 加 op 仍不成为该 Reviewer；自审、初审人/cohort、重分派/第二意见拒绝 |
| 二审安全取消 | 当前 scoped Manager，SECURITY_CANCEL 的既有特殊路径 | 不依赖 READ_WORK/op 授权答案；仍需 feature/mode/instance/身份/项目，取消后不重开 |
| concern metadata list/detail | 当前 scoped Manager + READ_WORK及原 feature/mode/instance gate；无需 record op或原handler身份 | 排除身份仍可合法读其scope内metadata；不含source/target snapshot，不将metadata读取升级为处置权 |
| concern source context / 新record | Manager + READ_WORK + RECORD_SECOND_REVIEW_FOLLOWUP；完整独立性排除集 | 提交人/owner/初审人/二审人/冻结cohort/target初审人排除；此处才在source-bearing输出/新写前核对 |
| concern 原键replay | current Manager + READ_WORK + record op；原handler、原body/key与冻结receipt | 不重读源、不重新要求完整当前排除集，返回source_integrity=not_checked；其他Manager可读metadata仍不能冒充原handler重放 |
| 审计预览/创建/浏览/下载 | scoped Manager + READ_WORK + EXPORT_REVIEW_AUDIT，当前 requester 和原 creator 的完整 gate | creator generation/tombstone、旧 URL、HEAD/Range/conditional、跨 scope、模式关闭；审计自身操作不授予自审 |
| 审计 retry / 原命令恢复 | 只有原 owner generation/principal/source，仍有当前权限门；区分capture与retry命令namespace | 同一UNKNOWN命令保持原body/key；旧retry key只回原receipt。已确认结果后新的显式retry用新retry-command key、同package/T/input；不以新capture key重建T来冒充恢复 |
| 分类仲裁 | 原 Manager/仲裁者与 ORGANIZE_ARBITRATION/ARBITRATE、冻结 pending cohort | approved/rejected concern 不能直接套用旧 handoff；不改原提交/初审 |
| 发布/交付 | 原 Fork release/#79/#80 与相应 Host 策略的独立授权 | confirmed/concern/followup/batch 完成/审计 ZIP 均不产生批准或交付资格 |

E 的 actor roster 至少区分提交人 A/B、初审 R1、二审 R2、组织 Manager、跟进 Manager、auditor、
producer、另一 scope 用户。可合并合法的 Manager 职责仅在完整排除集证明成立时；跨 org/project 另有否定用户。
所有人都是合成身份。provider 的 op 集以项目为边界，角色/分派/独立性在对应服务重复检查，不能只在 UI 隐藏按钮。

## 5. 隔离环境所有权与执行准入

当前未选择或连接任何执行环境，资源可用性 UNKNOWN。优先由用户另行指定新建、独占的原生测试环境；
Windows/原生 Linux/已批准 CI 可分别承担明确的平台 case，不因路径含 test、loopback、数据库名字或
TEST_ENVIRONMENT=1 就视为获准。没有 Linux 资源时保留 POSIX 分支 NOT_RUN，不调用 Docker/#76 或保留 WSL。

E0（任何 Django import/migration/seed/worker 启动之前）须记录并匹配：

- 受控 source/tree、环境/资源 owner、允许操作、有效时间、授权人与独立验收人。
- PG 实际 server/system_identifier、data_directory、port、database/user 与空 catalog；
  其控制目录、进程和目标 database 均属于本 run。SQLite 只作补充，不能证明 PG 竞争/约束。
- 新 Redis 实例实际 run_id、地址/port/DB、私有启动目录及空 keyspace、受控 PID/credential reference；
  仅新 DB number/唯一 queue 名不足以证明隔离，禁止对任何共享实例 FLUSH/清理。
- canonical 文件根、private exports/static/temp/log/report 的 allowlist 与 protected path 排除；
  不导入可能自动 mkdir/生成 secret 的 settings 来“探测”未知环境。
- 运行前与结束后的 protected worktrees/refs/证据 hash；metadata 证据不打印密码、URL credential、cookie 或 secret 值。

任一项不成立就停在 E0，不执行 migration、seed、访问现有数据或自动修复环境。拒绝记录本身不是完整 E 验收通过。

E1 之后，另行授权的 harness 才能建立新 schema/合成 actor/source。沿用现有 seeder 的拒绝复用/ownership
思想，但不得把它的 process-local override/mocks 当独立 worker 配置。所有 web/RQ 进程使用同一已审查配置桥接；
真实 RQ 测试禁 patch queue_available/api_queue/enqueue/generate，禁在 HTTP 进程内同步 generate 替代 dequeue。
该启动桥接/组合 fixture/harness 尚非本轮可执行交付；若现有工具不足，后续先取得仅验收支撑的实现授权及 review。

## 6. 未来执行顺序与证据

1. E0 只读所有权检查、配置/权限/formatter 可满足性；任一缺口停止。真实业务启用另需非测试 provider 准备证明。
2. E1 自有空资源建 schema/合成数据，先测默认关闭/未知模式/Core-only/无 op 等拒绝，形成原有数据和 release 基线。
3. E2 全量/比例/人员/任务包、分类冲突、可信合成 confidence 三种合法来源分别重放；一 batch 保持原 discriminated strategy。
4. E3 初审拒绝→原Annotator新revision→单独再审核；二审分别记录confirmed/concern，只有concern进入独立跟进；两种disposition分开。
5. E4 分类 pending/cohort 的原仲裁交接及其不可变来源；concern 不新增仲裁来源。
6. E5 **浏览器/API 中新建** audit capture，经真实 Redis 和独立 RQ worker 到 READY，再真实 HTTP 下载与离线 ZIP 校验。
   记录 HTTP intent/key/提交阶段、package/attempt token、RQ job ID、独立 worker PID、DB 转换、文件/manifest digest；
   四类关联一致才称传输通过。原 synthetic ready 包不能充当此链。
7. E6 撤权/删除/ID复用、UNKNOWN、重复投递/旧 token、并发额度、文件故障和现有 release 回归；
   每个 destructive case 只作用于本 run 的合成源，不操作保留实例或全库。
8. E7 按下一节做停用与恢复演练，完成 private 证据校验和角色/策略效果验收；最后报告每一 case 的执行状态与缺口。

两语 en-US/zh-CN、真实 Space/Enter 激活及异步返回焦点沿用现有 journey；不以脚本 click 冒充原生键盘验收。
真实 producer 准备可以独立受阻：合成 assertion 只能证明协议，必须明确保留 model/校准质量和真实低置信度业务可用为未验收。
Host/Core 本轮仅测试 Fork 的否定/兼容边界，不连 Host，不为 Host 发请求或代签业务批准。

运行证据 private raw / public redacted summary 分开。公开包/summary 不含原文、答案、自由理由、人名、secret。
原合成 browser 页面可含合成答案，其截图/原始日志只留私有报告；最终给出 source/context/profile、对象和证据摘要。
历史 PASS、当前 NOT_RUN、vendor SKIPPED、已证前置 BLOCKED、未经建立 UNKNOWN 保持不同。

## 7. 停用、排空和配置回退

停用不是撤销已提交审核/意见/跟进，也不是撤回已交出的字节。当前没有分布式 kill-switch、audit cancel/reopen、
字节远程召回或自动清理保证。演练只在另获授权的隔离实例/PID/连接上进行。

### 7.1 正常暂停

1. 先记录停用意图、进程和在途请求/作业清单，控制本run测试驱动不再提交新业务意图。
   这只是准备阶段，不称已建立服务端屏障，也不把仅关闭UI按钮当作撤权。
2. **在HTTP入口、gate及Manager权限仍有效时**，对明确需要终止的 pending 二审走既有安全取消；逐一批准并记录。
   取消永久占subject槽。不需取消的pending保留，关闭后等待同配置恢复；不得先封闭全部HTTP入口再假设还能调用取消。
3. 然后建立获准自有服务的新请求入口/进程屏障；无现成流量控制时使用自有服务停接收/优雅退出措施。
   在原合法配置下让已提交audit作业正常drain到已知状态，让已打开的下载流排空；
   DB/dispatch/publish 的 UNKNOWN 留原 key/token/文件/lease，不猜测回滚，不把“无响应”当“未提交”。
4. 对每个组件关闭相应 enabled/mode/operation，按其真实依赖处理：followup 依赖 second；audit 等独立 gate 也须单独关闭。
   op provider 必须撤实际 allowed_operations；`state=paused` 不是撤权。不得改变legacy release领域政策/数据或其他scope。
   获准独占实例的进程停机可暂时影响该实例的legacy请求可用性，须在操作授权和结果中明列；不能将此方案描述为线上共享进程的audit-only无影响停用。
5. 全部自有 web/worker 进程以停用配置重启/停机并逐个证明版本/配置代次收敛，才定义 **D_closed 屏障**。
   从每个存活入口发新collection/detail/command/旧URL/HEAD/Range/conditional请求，按当前gate拒绝，
   无新source/target答案、审计文件内容字节或业务写入。原有拒绝安全审计/会话维护可发生，不能携带原文/secret。
   拒绝的具体码以现有401/403/404/typed协议为准，不强迫不同资源伪装同一404。
6. 分开记录屏障前已授权/已提交的结果及屏障后新请求。屏障前已交出的 FD 流可能继续；若必须中止，
   另获授权后终止本隔离服务/连接，并记录已发送字节。已经下载的副本无法撤回。

### 7.2 紧急停用与作业状态

可先关闭 gate/撤 ops 并收敛进程，但其结果不能统称“所有作业 cancelled”：

| 观察状态 | 合法的停用/恢复解释 |
|---|---|
| dispatch_pending | enqueue 的 creator guard 可能直接拒绝并保留 dispatch_pending；不保证创建 blocked 记录 |
| queued / running | worker 入场和 publish 前重验；403类可能 blocked_auth，formatter不兼容可能 blocked_runtime；硬终止/DB不确定可留下原 lease/state |
| ready | 字节和 manifest 保持；停用后新访问拒绝；恢复完整原身份/配置且无 tombstone 才能再读；不重新生成 |
| blocked_auth / blocked_runtime / failed_retryable | 先证明依赖和完整配置恢复，再由原 owner 显式 source-free retry；进入 ACTIVE 仍受 owner1/project2 准入 |
| quarantined / failed_final | 终态不可 retry；不重开、不手改状态、不把健康包错误UUID/末端拒权当隔离依据 |
| 二审 cancelled / concern followup recorded / creator tombstone / Project 硬删 | 配置恢复不能撤销终态、复活账号代际或重建历史对象 |

queued lease 为30分钟、running为31分钟的恢复资格是源码边界，不能靠改不可变时间/状态绕过。
真实重复投递和旧 token 可在短时链路演练；时间边界用已标明的隔离时钟合同测试或另行授权长时演练。
前者不称已经等待真实31分钟；后者不塞入300秒 browser call，不扩大原 UI/CI 限额。

不得清空 Redis、广泛 cancel queue、删除 READY/UNKNOWN 文件或 lease、停用非本 run worker、修改共享 #79/#80 环境。
本设计没有审计专用保留/擦除任务。源码自身对已确定未发布 scratch 的合法清理保持原合同。

### 7.3 恢复与回退

配置回退恢复完整获准版本：source/兼容 formatter、DB schema、stable instance UUID、policy/version/profile/limits、
身份/creator generation、相同 secret-reference version、op mapping 和 private root；逐进程校验后再开放本 scope。
减限、policy.version 变化、formatter 漂移均可能阻断旧包；不能只恢复一个 enabled flag。
不通过schema downgrade、删除业务行、换UUID、重签/重建包来恢复。同一UNKNOWN capture/retry命令只以原body/key恢复；
capture key与每次retry-command key是不同命名空间。旧retry key永远只返回该次冻结receipt，不再调度下一attempt。
在旧命令结果已经确认、包又处于合法可重试状态时，用户发起一次新的显式retry应使用**新的retry-command UUID**，
保持同package/T/frozen input，由服务端生成新的attempt token。它不是新capture，也不允许新capture key重建T冒充恢复。
已知提交前拒绝且没有receipt的原retry命令可按原规则在条件恢复后保留同key再请求；
这不同于“已有receipt的旧retry key再次调度”。原键回执恢复与新的显式retry都必须覆盖。

本文/矩阵的“无字节”指不泄露source答案或审计文件内容；通用拒绝JSON/HTTP headers不据此被要求为空。

SECRET_KEY 轮换可能使 preview/viewer/cursor 失效；旧 owner generation/principal 等仍决定原命令恢复，
不能声称轮换必然毁掉全部原 key，也不能把轮换当业务 namespace 重置。本轮不演练真实 secret 轮换。
需要轮换的部署须另有密钥生命周期设计和授权，并保留 UNKNOWN 原命令。

## 8. 硬预算与关闭口径

复用现行 limits，不抬限、不截断来源：audit owner1/project2 ACTIVE；roots/assignments1000、revisions2000、
per_assignment20、entities5000、edges10000、cohort2000；canonical/input/projection8MiB、stored32MiB、
artifact16MiB、scratch32MiB、每pass SQL128、chunk250、page50、worker1800秒。
全部同时适用，不能把各最大值误解成可同时装满的承诺。callback/恢复、拒权、重复读也按原预算累计。

既有 UI/CI 清单仍为11 shards、26 browser calls、12 evidence producers（含prepare）、13实际jobs、14 artifacts；
browser max-parallel3，prepare25分钟、browser硬40分钟/观测软30、aggregate5分钟；
二审和审计各300秒call+15秒kill grace。授权gate三producer各25分钟+gate5分钟、理论80 runner分钟。
本轮不改这些文件或跑CI；若未来新增真实RQ验收call/支撑代码，须另行设计独立预算和清单并另授权，不能挤入原call或删除旧场景。

受限Fork V1报告须分别列72项的阶段归属与来源：E的69项形成获准隔离scope的逐条执行/NA/平台状态和停用证明；
P的L03/L04与O的L06形成各自独立准备/授权结果，不混入E通过数。E必需项未满足则partial/no-go；
E通过但P/O未满足只给隔离合成通过，不给真实业务启用结论。前置不具备保持NOT_RUN/已证BLOCKED，未选环境为UNKNOWN；
不得把 D 的静态审查、旧CI、合成producer分数、测试provider当 E/P/O 的通过。
原 #84 与 #81 保持 OPEN；任何实际启用、Host操作、推送/PR/合并、部署与 Issue 状态变化都另行确认。
