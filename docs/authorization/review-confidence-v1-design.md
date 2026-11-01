# 低置信度抽审 V1：可信修订事实与 ReviewBatch 复用设计（#84）

状态：**DESIGN_ONLY / IMPLEMENTATION_NOT_STARTED**。基线为已合并且完成 main/push CI 的
`1d4092c87d4e020dbd4f92aa63428679b44590e3`，tree
`083cc1ad04d73161f14d0f527f6479a6b2facf89`。本文提出未来契约，不代表路由、字段、迁移、
producer 或开关已经存在。功能、PG/SQLite、浏览器、远端 CI、部署均 **NOT_RUN**；
保留运行态/数据库一致性 **UNKNOWN**。执行证据另置报告，不回写历史设计快照。

验收要求见 [`review-confidence-v1-acceptance.md`](review-confidence-v1-acceptance.md)。
仅 Fork；#81/#84 保持 OPEN。二次复核、Host/Core 集成、发布策略、部署、Docker/#76
以及其他 worktree/保留数据均不在本次操作范围。

## 1. 已核对的实现与根本约束

| 基线文件 | 可复用能力 / 必须保留的约束 |
| --- | --- |
| `label_studio/review_batches/service.py` | latest-pending/assignment 候选、package/submitter 交集、1000 上限、global/per-person ceil 配额、seed、完整冻结分母；当前 priority 只接受三类 conflict |
| `label_studio/review_batches/api.py` | 同一批次预览/创建/队列/返工入口；原键/完整请求 hash、principal/actor 恢复；新策略须单独走受控适配 |
| `label_studio/review_batches/guarded.py`、`ner_priority.py`、`field_priority.py` | SQL 字节门禁、受控 plan 识别、当前 viewer/权限复验；marker 部分命中不能降级 legacy；保留 #128 SQL NULL 负例 |
| `label_studio/tasks/models.py`、`tasks/serializers.py`、`tasks/api.py`、`ml/models.py` | Prediction 可由普通导入/API/ML 路径写入；nullable score、自由 model_version、task/project/model_run 均不是 exact Submission 输入证明；Prediction save/delete 还会修改 task 更新时间 |
| `label_studio/tasks/submissions.py` | result_snapshot 冻结 annotation/task.data/task.meta/project.label_config；result_hash、每份修订一次 ReviewDecision、原审核/提交权威保持不变 |
| `label_studio/access_control/identity.py`、`project_operations.py` | trusted Principal(source/principal_id) 必须映射 active 本地 User；操作授权和 Project-first fence；尚无未映射机器身份写入通道 |
| `web/apps/labelstudio/src/pages/DataManager/ReviewBatches.jsx`、`ReviewPriority.jsx`、`reviewBatchResponses.js` | 整条列表→预览→创建→审核→返工流程复用；当前 algorithm/capability/bounded-viewer 分支只认识旧策略，不能只加一个 selector |

`Prediction.score` 不是人工 Submission 正确率。对旧 Prediction 在预览时补拍“当前 task/config hash”，
不能证明原推理看到了那些输入；最新 Prediction、task.precomputed_agreement、冲突率及客户端提交的
model_version 都不能升级为可信置信事实。本文不修改通用 Prediction/导入/ML 摄取链路。

## 2. V1 产品边界与复用

1. 可信 producer **预先**针对 exact immutable Submission 产出一个标量置信声明；Fork 校验并存储
   不可变的最小来源事实。人类 Manager 明确选择一种 source profile/version，再预览和创建现有 ReviewBatch。
2. 一次批次只使用同一 profile digest，即同一 evaluator/model artifact/分数定义/校准/输入合同。
   不跨模型排序，不混合 confidence 与 conflict，不自动切换“最新”来源。
3. 首个输入合同 `static-text-choices-submission/v1` 仅支持已有分类读取器认可的 inline Text + static Choices
   子集、完整单份 Submission。复用其纯支持范围检查与当前快照验证，不运行一致率指标。
   URL/媒体/CSV resolver/动态 Choices/混合或不支持控制明确 unsupported；NER/字段置信输入合同另评。
   **三种既有 conflict 策略与其功能/开关完全保留**。
4. 复用 ReviewBatch 的人/任务包过滤、分母、配额、种子、队列、审核、返工比较及进度恢复；只适配新 priority
   的输入、冻结证据、排序及受控读写。无第二个抽样批次、调度器、审核台账或模型计算框架。
5. 不新增手工评分上传页面；不自动发起推理/训练/采集、不回写 Prediction/Task/Annotation/Submission，
   不产生仲裁交接，不改变 approve/reject/release/#79/#80。抽样理由、置信声明、仲裁 resolved 都不是批准。

新增 `ReviewConfidenceObservation` 是**来源完整性所需的最小事实存储**，不是 review decision。
将其塞进已冻结 Submission 或可变 PredictionMeta 会破坏既有合同，因此不采用。

## 3. 信任锚、版本与启用

拟新增默认 false 的 `ANNOTATION_REVIEW_CONFIDENCE_ENABLED`，并同时要求现有 review-batch flag
和 exact organization:project=`fork_review` 模式。Core-only、缺失/未知模式、非 PG/SQLite 默认拒绝。
不借用 export 开关、凭据或发布 policy；开启 feature 不自动授权 producer。

运维侧 registry 分成两部分，均无浏览器/API 修改入口：

| 配置 | 内容与语义 |
| --- | --- |
| Instance identity | 独立稳定 UUID，纳入所有 input/fact hash；不从 hostname/凭据/项目 ID 推导、不在启动时随机生成；克隆/恢复前明确实例归属 |
| Source grants | instance/org/project 下，trusted Principal.source/principal_id/mapped actor → 唯一稳定 producer namespace +允许的 profile IDs；namespace 不随 descriptor 版本改变，配置歧义 deny。允许摄取的当前授权撤销立即生效；凭据不写入 descriptor/hash/响应/日志 |
| Versioned descriptors | 不可变 profile_id + version + digest；evaluator ID/version、model ID +不可变 artifact SHA-256、score definition、calibration version、整数规范版本、input contract version、producer namespace |

descriptor 的权威版本标识是 **profile_digest（内容寻址）**，完整 key 为 profile_id + digest；人类可读的
version 标签不能代替它。同 key 内容不同、自报 digest 不符或重复冲突的 registry entry 校验失败并 deny。
任何内容变化必须产生新的 digest，并作为新的明确版本选择；相同显示标签也不得自动替换旧 digest。
不靠进程记忆或隐含注册表历史证明标签不可变。请求显式指定 digest，服务端不补最新值。credential rotation 在同一 trusted
principal 内不改变 descriptor；principal/source/actor 变更仍是不同 receipt owner。

hash 使用既有 strict canonical JSON + SHA-256（拒绝 NaN/Inf/非法 Unicode）；它证明内容绑定，不是签名、
bearer token、模型执行证明或准确率认证。artifact digest/分数含义是运维准入与可信 producer 的明确声明；
Fork 不下载权重或验证实际推理。没有合格 producer 时只能保持未启用/unknown，不伪造可用性。

V1 producer 采用现有认证与 `resolve_actor`：专用 active 本地审计账号、当前组织成员及 Manager 项目成员，
由 registry 匹配其 trusted principal/source/actor。另需 `READ_WORK` 和独立的拟议操作
`PROVIDE_REVIEW_CONFIDENCE`。普通 Manager 的 `ORGANIZE_REVIEW`、管理员身份或普通 Prediction 写权限
都不能发布置信事实。该新操作不加入默认 legacy operations，也不随开关自动授予；运维显式配置的现有
ProjectOperationProvider 必须允许它。其账号不得是被评分 Submission 的 assignee 或 submitter。

## 4. 输入与 exact revision 的九项绑定

服务端从已授权、当前资源图和不可变 snapshot 推导，不接受客户端认领身份：

| Pins（全部必需） | 推导与校验 |
| --- | --- |
| submission_id / assignment_id / task_id / revision | 当前同项目完整 FK 图；正 safe integer，bool/float/string 不等于 ID |
| assignee_id / submitter_id | 读取当前 assignment 与 Submission；新事实要求两者相同且非空，不凭请求指定 |
| result_hash | 验证实际 result_snapshot 的既有 hash 后取得；不以“字段非空”代替完整性检查 |
| task_data_hash | strict canonical snapshot.task.data 的 SHA-256；不读取当前 Task.data 冒充历史 |
| label_config_sha256 | snapshot.project.label_config 的确切 UTF-8 SHA-256；不使用进程相关 label_config_hash |

外围还绑定 instance/org/project IDs、snapshot contract/schema、task.meta 与完整 snapshot 字节。
`confidence_input_hash = digest(schema, instance, scope, 九项 pins, 完整 canonical result_snapshot)`。
producer 接收服务端生成的 input_hash，处理该确切内容后回传；服务端再计算并比较，而非相信请求中的 hash。
完整快照不存入 observation。遗失/重建/NULL FK 不能按冻结数字 ID 重绑。
输入 envelope schema=`review-confidence-input/v1`；fact schema=`review-confidence-observation/v1`，
独立 receipt schema=`review-confidence-observation-receipt/v1`，均纳入对应 canonical hash，拒绝未知/部分版本包。

fresh input/摄取要求是 latest pending、无 ReviewDecision，且当前 assignee=submitter；仅影响事实摄取，
不缩小 ReviewBatch 的原候选分母。旧修订事实不能跟随 assignment/task/最新修订迁移。
合法重派造成 pins 不再相等时，新预览将该候选标记 `unknown:revision_binding_changed`；现有计划仍为历史事实。
存储事实自身 hash/内部绑定损坏则整请求 409，不能用 unknown 掩盖篡改。

## 5. 最小 producer 接口与不可变事实

拟议路径；当前均不存在，也不在本次实现：

| 路径 | 用途 / 权限 |
| --- | --- |
| GET `/api/projects/{P}/review-confidence-inputs/{S}/?profile_id=…&profile_digest=…` | 专用 producer 取得已界定大小的完整冻结输入、九项 pins、input_hash、descriptor digest；上述 source grant + READ_WORK + PROVIDE 权限全套检查 |
| POST `/api/projects/{P}/review-confidence-observations/` | 同一 source grant/操作；提交事实 metadata，`Idempotency-Key` 为 producer 原始事件 UUID；没有人类批量上传或公开事实列表 |

人类 profile 发现复用 GET `/api/projects/{P}/review-batches/capability/`：仅当前 Manager + READ_WORK +
ORGANIZE_REVIEW 且新模式/开关允许时，返回 `confidence_profiles`（最多20个当前scope可用描述）、
`can_prioritize_confidence`、`can_read_confidence_priority`、`can_recover_confidence`、当前 viewer 与当前 Manager 的 `confidence_owner_hash`。
每个条目仅含 profile UUID/digest、展示标签、evaluator/model/artifact/分数语义/校准/整数/输入合同版本，
不含 source grants、publisher 身份、凭据或答案。单描述 canonical <=4KiB，整个 capability <=128KiB，
超过20条不截断，仅将新策略的 **fresh 发现/input/ingest/preview/create** 标 unavailable；旧批次能力保持原合同。
历史读能力、合法人类owner的 `can_recover_confidence` / owner hash 独立于 fresh descriptor 列表，不能因列表为空、超限或
descriptor退役而撤销。can_recover只取决于当前Manager/READ_WORK/ORGANIZE_REVIEW/新旧scope gates，后端仍逐项校验receipt。
无规划/恢复权的 Manager 和 Reviewer 不返回 profiles/owner；Reviewer不获得can_recover。
Reviewer 只有新策略 read capability/当前 viewer 等最小投影；关闭新开关时不扩旧 wire。
profiles 与权限在响应末尾复验，UI 不读私有 registry、不猜默认最新版本；该读接口不产生事实或推理。

严格 POST 仅接受 `submission_id, profile_id, profile_digest, evaluated_input_hash, score_units,
evaluated_at`；event UUID 来自 header。actor/provider/producer/model/status/答案/subject pins/hash 等额外字段拒绝。
descriptor 与身份由服务端补齐。`evaluated_at` 是 producer 声明的 UTC 时间，不授予权限、不决定先后/择优；
另存 server accepted_at，不相信它证明推理时间。日期必须有效且无歧义；不得以时间戳判断是否覆盖旧事实。

score 是严格整数 `0..1_000_000`，单位为百万分之一；越低表示该 descriptor 定义下越不确定，
不宣称是真实正确概率。V1 wire 不接收 float/字符串/百分号；bool、NaN、Inf、负数、越界拒绝，
不 clamp/转零。producer 适配器须在其版本化整数规范中定义转换、舍入和校准；Fork 不二次归一化。

`ReviewConfidenceObservation` 冻结：UUID、项目/实例/组织、全部 pins/input_hash、完整 descriptor 的最小版本包与
digest、score_units、producer namespace、event UUID、evaluated_at/server accepted_at、fact_hash，
以及独立的 principal/source/actor/request_hash/receipt_hash。无答案、无预测 result、无凭据。
可空便利 Submission/User FK 与 frozen IDs 分开；清空 FK 不得恢复。项目 cascade 沿用当前本地工作记录语义，
不宣称永久平台审计。无独立编辑/删除 API；PG/SQLite guards 保护 PK、scope、proof、分数、时间和 receipt，
只允许预定 convenience FK→NULL。

两个唯一约束：

- `(project, instance, producer_namespace, event_uuid)`：事件命名空间由当前 trusted identity 的唯一 source grant
  导出，独立于 profile/digest；同事件同完整 body/owner 原键恢复，改 profile/digest/score/body/owner 409。
  同 namespace 的 principal 轮换不取得旧 owner receipt；不同 producer namespace 的 UUID 可独立使用。
- `(project, instance, profile_id, profile_digest, submission_id_frozen)`：一个 profile/version 对一修订仅一份事实；
  不同事件竞争/更正同修订 409，不挑最新、最小分或先查询到的任意行。更正必须是明确新版本与新事件，旧计划不变。

POST fresh 必须重新校验 input_hash、完整 source、当前 producer grant/角色/operation/descriptor。
回复之前再检验实际序列化数据与当前身份；失败整事务 rollback。既有事件恢复先查当前 source grant/操作与
owner，再只校验本地完整 receipt；不要求 live Submission、fresh pending、当前 descriptor 可供新摄取，
不再读取 snapshot/运行模型。grant/账号/项目权限/feature/mode 被撤销仍拒绝；不同身份不能借 event 恢复。
父项目消失或本地 receipt 不完整不能恢复。此合同不改变任何旧 ReviewBatch key/hash。

## 6. ReviewBatch 新 priority 与冻结计划

在原 `PlanInput` 内增加严格判别 union；三个旧 conflict 对象仍原形、原验证、原 request hash：

```json
{
  "priority": {
    "kind": "revision_confidence",
    "profile_id": "11111111-1111-4111-8111-111111111111",
    "profile_digest": "<64 lowercase hex>",
    "threshold_units": 400000
  }
}
```

name/rate/per_submitter/seed/package_ids/submitter_ids/expected_plan_hash 继续由原批次合同定义。
不接受 batch_id/manifest_hash/Prediction ID、自报分数、actor 或模型覆盖参数。
新 algorithm=`revision-confidence-ranked/v1`，priority schema=`review-confidence-priority/v1`，kind 同上。
descriptor 必须仍允许 fresh 使用；只加载本项目 exact candidates 的最多一份同 profile/version 事实。

计划冻结整个候选分母、选择 flags、旧六项候选 identity 与附加 pins，profile 最小完整版本包、阈值及整数规范、
排序规则、seed，每候选的 fact UUID/hash/最小完整事实包或明确 unknown 原因。事实包不含 publisher 身份/凭据/答案，
publisher 审计留在 observation；fact hash 不依赖那些私有字段，receipt hash 单独覆盖它们。
完整计划受既有 ReviewBatch plan hash 和 PG/SQLite immutable guard 保护，不重写旧计划或旧 migration。

| 冻结 state | fresh 语义 |
| --- | --- |
| `low_confidence` | 完整同 profile/version 且 exact pins/input 验证成功，score_units < threshold_units |
| `not_low_confidence` | 完整证据且分数 >= 阈值；仅表示未命中该规则，不表示高质量/批准 |
| `unknown` | 无事实、只有 legacy Prediction、仅旧修订事实、合法重派/原 FK 丢失、或输入合同 unsupported；保留明确 reason，不推断低风险 |

profile 版本/实例/hash 不匹配不能猜测替代来源；错误项目/namespace 的 fact 永不加载。
候选本身快照/计划损坏、同唯一键多份有效事实（绕过 DB guard 的合成破坏）及事实内部 hash 损坏均拒绝，
不归类为正常 unknown。来源缺失与数据损坏是不同情况。

## 7. 确定性排序与覆盖率

继续使用原候选池和每组配额 `ceil(N * rate / 100)`，全局或每 submitter 分组；分母包括 unknown，不能按有分数的人缩小。
每候选稳定 hash=`digest([algorithm, project_id, seed, 原候选 identity])`；固定长度 ID 作最终 tie-break。
组内 key：

- low_confidence：`(0, score_units, stable_hash, submission_id)`，低分优先，同分由 seed 决定。
- not_low_confidence/unknown：`(1, 0, stable_hash, submission_id)`，共同作原配额的确定性补足，unknown 不冒充可信或自动排除。

不把冲突率/多个模型分数拼成混合 key。阈值范围含 0 和 1_000_000，比较严格 `<`：0 不命中；满分在最大阈值时仍不命中。
low 数量超 quota 显示未覆盖数量，不扩样；low 不足显示补足数量。0 份合格事实时允许显式预览/创建种子补足，
必须清楚显示“置信覆盖 0、全部 unknown，按种子抽样”，不显示“低置信风险已覆盖”。
UI 确认摘要属于反馈，expected_plan_hash 才是服务端精确计划前置条件。

## 8. 权限、读投影与冻结后的来源状态

Manager fresh 预览/创建需当前 READ_WORK + ORGANIZE_REVIEW、全部新旧 feature/mode gates、该 scope 可用 descriptor。
无需 PROVIDE；反之 PROVIDE 不授予 ORGANIZE_REVIEW 或 REVIEW。Reviewer 只能按现有项目范围读选中队列和批次，
实际审核仍需 REVIEW；Annotator 无批次或 producer 权限，不能通过已知 IDs/旧浏览器/换 namespace 扩权。

V1 producer 输入只含本 exact Submission，不读其他人的答案/cohort。Reviewer 排除本人 submitter/assignee 的候选，
在 count/queue page/live snapshot 或 fact row 加载前执行；列表先用受控 SQL 判断存在可见候选再分页。
为计划 hash 验证而解码已限额的 frozen **metadata** 是允许的，不得借此加载自己的/不可见来源答案或 live fact。
Viewer HMAC 绑定当前 principal/source/actor/project/org/role；同 role 变 actor 也必须清状态。
新策略的 SQL 可见候选谓词必须同时排除 frozen assignee_id 和 submitter_id，不能只复用旧 submitter 条件；
只在 confidence marker 上适配，不扩大或改写旧 NER/field cohort 规则。自身之外没有 peer/cohort 输入。

Manager 投影可显示自己项目的历史数值、profile label/version、fact UUID/hash、unknown reason；不返回 producer 的
principal/source/actor、原始事件 UUID、模型答案或凭据。Reviewer 只得当前可见候选的普通抽样理由与 state，
不扩大为数值/完整来源 proof/其他答案导出；自己的候选不出现在计数/分页/结果中。

冻结后的 selection 永不重排、不缩分母、不用新事实或新修订补洞。历史分数/理由标明 as-of 创建，不当作实时质量。
live source health 与历史 selection 分开：后续 observation/FK/producer 消失可标 unavailable/inactive，但不得抹成 not_low，
也不能重写冻结 state。当前 snapshot/账号/项目权限依旧实时验证；源损坏/缺失屏蔽当前答案/动作。
ordinary review、已审批记录、仲裁、导出/文件/媒体/bulk 权限不由置信状态或批次完成授予。
撤销 producer 的发布 grant 只阻止其新摄取/恢复，不自动使过去已接受的声明失效；fresh 规划是否仍可用旧事实由
当前 descriptor 是否允许 fresh 使用决定。退役 descriptor 阻止新规划，历史 selection 和合法人类原键恢复不重写。
如安全事件要求停止现有批次上下文读/审核，应撤销其当前 feature/mode/人类operation scope，而不是修改旧分数或默认批准。

现有批次原键恢复：当前 Manager + READ_WORK + ORGANIZE_REVIEW + 新旧 scope gates，核对完整 body/hash、
creator_principal、actor 及新策略冻结的 creator_source，再验证本地 bounded plan/receipt；先于任何 live source
查询、fresh descriptor/producer 可用性和计算。source/descriptor 移除不触发重建，当前人类权限撤销仍拒绝。
返回 UUID、原 plan/request hash、当前 viewer 的 typed ACK；不把 viewer 当 bearer/key 或旧 receipt 的恢复凭据。

## 9. 有界加载与实际成本

以下是 V1 拟议上限；后续实现若无法证明须修订设计再审，不能移除预算、截断样本或仅测最终响应。
字节一律 UTF-8；SQL stored bytes 在 JSON 解码前计算，canonical bytes 在有界解码后、保留/哈希前检查。

| 对象 / 每次 materialization | inclusive 上限 |
| --- | --- |
| 候选/事实数 | 原 1000 candidate；每 candidate 最多 1 fact；source union 去重后最多 1000；profile 最多 20 个可选 metadata 描述 |
| 单个 producer 输入 snapshot | SQL stored 2 MiB，canonical 256 KiB；已有静态分类 text/config/control 支持范围和更小上限继续有效 |
| fresh preview/create 的去重 snapshot union | SQL stored 64 MiB、canonical 8 MiB；不因按人/任务包/页数拆分重置预算 |
| fact/descriptor | 单 fact canonical 8 KiB、SQL stored 32 KiB；descriptor canonical 4 KiB；POST body 16 KiB |
| 每遍 metadata 输入 union | 去重后的所有 fact/descriptor/receipt/已存plan JSON 合计 SQL stored 16 MiB、canonical 4 MiB；恢复只读本地proof也受同一界限；每遍新SQL独立计量，不以单行或最终plan预算代替 |
| frozen plan / 响应 | plan SQL stored 16 MiB、canonical 4 MiB；完整响应 UTF-8 4 MiB；包装开销计入 |
| 含 confidence 的列表页 | 受控 metadata union SQL 16 MiB、canonical 4 MiB；旧 NER/field 各自更小预算仍独立生效；若无新 marker，旧路径/额度不变 |

producer POST 使用有界请求解析（最多16KiB+1探测字节）在 JSON decode 前拒绝过大/分块 body，不只相信 Content-Length。
stored bytes 指 SQL 将要返回的 JSON 文本 UTF-8 字节（PG octet_length / SQLite BLOB bytes 的等价度量），
不能用字符数或压缩后的 pg_column_size 替代。
需在 SQL count/size inspection 阶段拒绝 1001、行大小或累计 stored bytes 超限；同一 hydration SELECT 使用 CASE
和累计 window gate 防止 inspection 后增大而进入 decoder。禁止 `SELECT *`/相关 ORM 偷加载 result_snapshot/大 JSON。
plan、fact、descriptor、receipt、本次 source union 均计入实际对应预算；cache 命中不能替代新 SELECT、锁或最终复验。

fresh 成功 preview/create：两次完整 bounded materialization/计划构造比较，实际 JSON 渲染后再做第三次
**完整 bounded source 验证读取**，不排序、不重新选样，也不替换已序列化输出。第三次重新读取候选图、actual
result_snapshot 与 facts/absence/descriptor，重算九项pins/input/fact proof及来源集合摘要，与前两遍一致才返回。
不能只检查 DB 的 result_hash/size/updated_at 列：同大小 JSON 篡改而列未变也必须发现。
最多三遍 snapshot union，即 192 MiB stored /24 MiB canonical；metadata union 三遍合计最多48/12MiB。
最后一次 source 读取也用SQL同SELECT gate，最终增大/变化则失败；最后 current identity/operation/mode检查在其之后。
该最后读取/授权检查是有限的读线性化点，不承诺阻止读请求返回后别人提交变化；写请求仍持有fence到验证完成。
排序仅在前两遍构造 N<=1000 的有限 key 集合，
不做 O(N²) pairwise、归一化相似度或模型计算；所有模型/外部 scorer 调用数为 0。
ineligible/超预算可提前结束并记录实际执行的 pass；不谎称完成两遍或所有失败都零工作。

producer GET/POST fresh 各至多两次单 snapshot materialization（构造/渲染后完整内容验证），snapshot合计4MiB stored /
512KiB canonical、metadata合计32/8MiB；不执行抽样排序。POST metadata receipt 恢复、
batch original-key 恢复与历史 selection 读取不 hydrate confidence snapshot、不执行模型。
普通队列/详情若需显示选中原 Submission，使用共享 bounded snapshot 路径和当前 scope，不额外加载其他来源答案。

## 10. 事务、缺失事实竞态与最终响应

producer fresh 写与 confidence ReviewBatch create 均必须使用同一 Project-first write fence：
Project → 当前组织/用户/项目成员（既有 helper 顺序）→ Task(pk) → Assignment(pk) → Submission(pk) →
Observation(pk/唯一键) → ReviewBatch；多个 ID 同层升序，禁止反向拿锁。原子层保留到实际 JSON 渲染与末尾复验后。
没有公共/bulk/异步旁路可直接写 observation；DB 唯一键/immutable guards 是第二层约束。

两种合法线性化：

- fact 先提交，batch 再取 Project lock：create 看到新 fact，若与预览 unknown 计划不同，409 preview_changed，零新 batch。
- batch 先完成冻结，fact 后提交：batch 保留此前 unknown 选择，后来的事实不重排；显式新预览才使用它。

仅锁已存在 fact 行不能保护 absence。任何以后新增摄取路径若不遵守同一 fence，不得宣称解决该竞态。
preview 为读也做两次计划 materialization + 渲染后第三次完整 source 验证；无写锁的并发变化须匹配失败/丢弃，不证明长期不再改变。
create 必须在锁内重新构造并比较 expected_plan_hash；withdrawal、new revision、source 增大/删除、descriptor
切换、实际响应被改写均在返回前拒绝并 rollback，不提交“回复失败但新 batch 已存在”的确定性失败。
网络 lost ACK 仍是 UNKNOWN：不据超时断言 rollback，不自动换 key。

registry 的版本描述不可原地修改，source grants 是外部实时信任配置。末尾复验它们是一次授权判断，不宣称
数据库锁能阻止运维在该判断之后改配置；强运维变更与请求交错的线性化点是最后一次授权检查。
它不能修改既有 frozen records，也不能把 POST token 内容当配置权威。

## 11. UI 整流程复用与 typed 协议

沿用 ReviewBatches → ReviewPrioritySelector → 原 preview/confirmation → ReviewBatch detail/ReviewerWorkspace →
ReworkComparison → 原 batch/filter/page/focus 返回。只增加 profile/version/整数阈值适配、历史 coverage/reason 展示，
没有第二套批次页/审核工作区/状态控制器，也不退休尚未被替代的旧流程。

preview 显示模型/evaluator/校准版本标签、分数含义、阈值、known/unknown/low/selected/未覆盖 low、requested/actual rate；
改变任一输入、身份、项目或 descriptor 时旧 preview 失效。Reviewer 不显示完整 source proof/数值。
空候选、全部 unknown、未知来源健康、无 REVIEW 权限、空过滤结果、加载失败与已保存后进度读取失败分别显示。

必须同时扩展新 kind/schema/algorithm 的 capability、preview/list/detail/queue/rework/ACK validators，
`priorityViewMatches`、bounded strategy/viewer/generation 分支及权限校验；unknown algorithm/partial protected marker
不得落回 ordinary/classification。新增 capability 名拟为 `can_prioritize_confidence` / `can_read_confidence_priority`，
仅新开关启用时出现；旧 disabled wire/hash 保持原样。
禁用策略 collection 过滤先将正 marker 归一化 non-null SQL bool 再取反，不隐藏缺少新 JSON keys 的旧批次。

UNKNOWN create 保存原完整 body/expected_plan_hash/UUID，显式原键恢复；read refresh 绝不 POST。
ACK 核对 project、batch、schema/kind/algorithm、key、request/plan hash、actor、当前 owner_hash 与 viewer；错/缺字段不确认成功。
`confidence_owner_hash` 复用现有字段交接的稳定owner指纹思路，由当前可信 principal/source/actor/project/org/role生成，
只作同owner比较，不是授权/输入字段/receipt secret。salt-only viewer轮换仍清旧读证据，但同owner的UNKNOWN
body/key可保留供显式恢复；真正owner或权限变更清旧intent。该适配复用当前创建状态控制器，不另建恢复页。
当前人类权限、身份/组织/provider 变化清理旧敏感 evidence/intent，迟到响应不能污染新 scope。
已确认保存后读失败保留已确认事实并提供只读重试；不重新提交来刷新进度。
沿用已有表单/按钮/i18n、键盘顺序、焦点返回与窄屏行为；产品文案不暴露内部 principal/SQL/hash 细节。

## 12. 错误和回滚合同（拟议）

| 状态 | 代表情况 |
| --- | --- |
| 400 | 严格字段/UUID/时间/整数 score/阈值输入不合法；producer unsupported 输入为 REVIEW_CONFIDENCE_INPUT_UNSUPPORTED；空候选继承原批次错误 |
| 403 / scope-safe 404 | 当前 actor/role/grant/operation/flag/mode 不允许；跨项目/组织资源按原 scope 模式拒绝且无存在性泄露 |
| 409 `REVIEW_CONFIDENCE_INPUT_CHANGED` | input/pins/latest pending、source、descriptor 或序列化后事实不一致 |
| 409 `REVIEW_CONFIDENCE_KEY_CONFLICT` / `REVIEW_CONFIDENCE_BINDING_EXISTS` | 原事件异 body/owner，或不同事件争同唯一 subject；不隐式 link/correct |
| 409 `REVIEW_BATCH_PREVIEW_CHANGED` / `REVIEW_BATCH_KEY_CONFLICT` | 复用原批次 stale preview / 原键冲突，零新 batch |
| 409 `REVIEW_CONFIDENCE_PROOF_INVALID` | 事实/plan/receipt 内部损坏，不用 unknown 或 legacy fallback 掩盖 |
| 422 `REVIEW_CONFIDENCE_TOO_LARGE` | 输入/count/stored/canonical/响应/CPU预估超限，整次失败不截断 |

唯一约束只将明确 allowlist 的两个 observation 约束及既有 ReviewBatch project/key 约束，在事务 rollback 后映射为对应
409。不得捕获任意 IntegrityError 装成幂等成功或确定性失败。数据库/连接异常保持原错误或 UNKNOWN；不泄露答案/凭据。

## 13. 未来实现接点与兼容审查

| 接点 | 后续最小工作；本次不实现 |
| --- | --- |
| `review_batches/models.py` + 新 additive migration | 一张 observation 事实表、两个唯一键、PG/SQLite 冻结保护；不改旧 migration/ReviewDecision/Prediction |
| `access_control/project_operations.py`、settings | 显式 PROVIDE 操作、默认关 feature/registry 校验；既有 policy provider seam，默认 legacy 不自动授予 |
| `review_batches` 新 confidence adapter/producer API | 受控 frozen input、metadata append；复用当前 identity/scope/fence/hash/有界加载，不调用模型 |
| `service.py` / `api.py` / `guarded.py` | strict priority union、策略分派、创建/恢复、typed plan guards、collection NULL 安全和全页预算 |
| `ReviewBatches.jsx` / `ReviewPriority.jsx` / validators / i18n | 完整流程适配和明确失败恢复，不只加选择框；保留旧 UI 行为与旧 ACK/hash |
| 已有 Submission/分类只读支持范围/SQL loaders | 小范围纯 helper 复用；不得通过通用 ORM serializer 绕过 predecode 门禁，也不得重新实现 XML/模型评分引擎 |

## 14. 验收、发布与本次交付

矩阵覆盖可信 producer/伪造输入、exact版本绑定、缺失/unknown、配额/重放、SQL predecode/CPU/response 限额、
独立 PG 连接两种提交次序、撤权和源变化、旧开关组合/partial markers/SQL NULL、双语与键盘/窄屏、
files/media/export/bulk/legacy review 负例。SQLite 顺序成功不能代替 PG 并发。

本次仅设计和静态自检、独立文档 reviewer；**不将任何矩阵功能行标 SUCCESS**。
后续先获设计实施授权，再本地实现/验收与独立源审查；push/PR、actual merge/main CI、部署/启用、真实 producer
接入/平台实跑及 Issue 关闭继续分别取证和授权。复用开源 Label Studio/DRF/当前已有 SQL 与 UI 流程，
不引入新依赖或企业商业组件。没有合格真实 producer 的合成测试成功，只证明协议/边界，不能宣称真实模型质量。
