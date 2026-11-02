# #84 二次复核 V1 验收矩阵（设计阶段）

对应[设计](second-review-v1-design.md)，基线 `15ca19c5abf48505042ee6b54bbb7194c5bf8d1e`。
**以下56项均为未来要求，当前统一NOT_RUN。** 静态review通过不代表功能通过；
supervisory_only仍是待用户确认的效力提案。
PG=新原生隔离PostgreSQL；SQ=新SQLite；双语=en-US/zh-CN完整浏览器。
每项执行时引用实际nodeid/步骤、checkout SHA、环境、结果，不以测试定义代替执行。

| ID | 场景/攻击或竞争 | 必须观测的结果 | 未来验证 |
| --- | --- | --- | --- |
| A01 | selected普通批次latest approved | context→明确选人→派发→assigned queue→confirmed；原初审/快照不变 | PG/SQ/双语 |
| A02 | 分类冲突与confidence来源 | 完整plan元数据/target proof及原策略gate验证；target_source_verified、metric_integrity=not_checked，0scorer/其他cohort答案载入 | PG/SQ |
| A03 | latest rejected→concern，中英理由 | 原rejected不变，意见单独显示，无自动返工/批准 | PG/双语 |
| A04 | pending/无初审/superseded/unselected/非latest旧approved | fresh dispatch拒绝，无残留task/receipt | PG/SQ |
| A05 | URL/动态Choices/HTML/媒体/NER/bbox/字段/伪静态 | unsupported，无降级或外部模型调用 | PG/SQ |
| A06 | 同task不同assignment/submission/revision/作者 | 不按task ID近似关联 | PG/SQ |
| A07 | dispatch后新revision | 原任务只审原revision并标历史，新revision不继承意见 | PG/双语 |
| A08 | metadata list/detail与source GET | 元数据零答案载入且not_checked；源验证成功才verified | instrumentation |
| A09 | 原batch/初审/source删除 | 元数据保留冻结IDs；source/decision unavailable，无替代来源 | PG/SQ |
| A10 | 切project/batch/source及返回filter/focus | fresh授权；晚响应不能污染页面/缓存 | component/双语 |
| B01 | Annotator/其他Reviewer/org admin/unscoped superuser | 所有新路由、known-ID、计数拒绝，不靠UI | PG/SQ |
| B02 | Manager仅有REVIEW/ORGANIZE_REVIEW/仲裁operation | 无新operation不能派发/代二审 | PG |
| B03 | 受派人为原assignee/submitter/初审人/冲突cohort本人，大项目候选翻页 | 过滤先于计数/分页；50/100有界、仅标量，context无整roster，直接POST单ID再校验 | PG/SQ |
| B04 | 跨org/project，交换batch/source/task/decision ID | default deny，计数/游标不泄露外域 | PG/SQ |
| B05 | 禁账号、删org/project成员、改角色及旧页面 | 读/写/恢复依当前scope拒绝，无旧状态授权 | PG/双语 |
| B06 | 撤SECOND_REVIEW保留READ_WORK；撤组织operation后取消 | 仅scoped只读，写拒绝；当前Manager可安全取消而不读答案 | PG/component |
| B07 | 新flag或原来源策略flag关/unknown/Core-only/missing map/其他DB | 新路由/源读取按各自gate拒绝；恢复不读live源但保留新feature/scope；旧Core内部读不变 | PG/SQ/vendor stub |
| B08 | 伪造actor/reviewer/status/effect/result/Host字段 | 严格拒绝；身份仅server session/token映射 | PG/SQ |
| B09 | 初审人后来失去Reviewer角色 | 不重写合法历史初审，不授予其二审权 | PG |
| B10 | 无独立人选/受派FK清空/复用数字ID | 无自动候补/重绑，明确不可派或操作 | PG/SQ |
| C01 | 九pins逐项改写及重算snapshot hash | 任一不符拒绝，无局部匹配绕过 | PG/SQ |
| C02 | 初审ID/结果/reason/time/reviewer、proof/hash损坏 | 派发/详情/写末尾拒绝，无现status修复 | PG/SQ |
| C03 | 派发前首审人/submitter/assignee为NULL | unavailable，不合成历史principal或推定独立 | PG/SQ |
| C04 | 冻结后submitter/初审人SET_NULL及非空改人 | NULL使source/decision unavailable但保留历史/原键恢复和冻结自审排除；非空改人corruption | PG/SQ |
| C05 | assignment改派/关联漂移、换batch | source/decision fail-closed，元数据not_checked | PG/SQ |
| C06 | update/bulk/native SQL改冻结列/PK或JSON 1→1.0、无receipt翻cancel、decision/cancel并存、遗留pending+decision | PG/SQ冻结/状态校验拒绝，SQLite不伪称deferred保证；SET_NULL不改冻结标量 | PG/SQ |
| C07 | 跨batch/key/assignee对同revision派发 | 全状态仅一task，不可选择性重审 | PG/SQ |
| C08 | 终态改结论/重开/改派/第二decision | 原意见不变，替代task也拒绝 | PG/SQ |
| C09 | pending取消、丢响应恢复、取消后新key再派 | 独立取消receipt准确且不覆盖派发receipt；当前取消scope恢复，V1不回收subject名额 | PG/SQ |
| C10 | 未知rule/schema/algorithm/effect/部分marker/伪类型 | 明确不支持/损坏，不进入普通batch fallback | PG/SQ |
| D01 | 同key/body/owner，丢响应后explicit recovery | 原UUID/时间/结果，零重写，UNKNOWN不自动换key | PG/双语 |
| D02 | 同key改body/principal/provider/actor，跨命令key | 按命令namespace验owner；冲突不借回执 | PG/SQ |
| D03 | live source/batch缺失或超限后的原键恢复 | 当前权威有效才恢复，0live-source/首审reason读取，not_checked | PG/instrumentation |
| D04 | 两连接同subject不同key或同key同请求 | subject一条；同请求恢复，其他冲突，无孤儿 | 独立PG连接 |
| D05 | 两decision及decision/cancel竞争version1 | 仅一终态/决议，失败无部分写，回执准确 | 独立PG连接 |
| D06 | 新revision先提交/派发先提交两种顺序 | 前者旧preview拒绝；后者保留历史复核，新revision无继承 | 独立PG连接 |
| D07 | revocation先提交/command先提交 | 拒绝或合法先提交，Project fence持续到render | 独立PG/NOWAIT |
| D08 | render期间换actor/mode/operation/源/初审/表示 | 末尾当前检查拒绝，全回滚，key可合法重试 | PG/injection |
| D09 | salt-only viewer变化与真实owner变化 | 同owner保留UNKNOWN body/key，换人清空，指纹非凭证 | component/PG |
| D10 | 成功后progress失败、错误ACK、迟到响应 | 已保存/读失败与UNKNOWN分离，只读retry不POST | component/双语 |
| E01 | snapshot SQL/canonical界值及多字节超界 | 准确bytes，超限decoder前拒绝、不截断 | PG/SQ/decoder |
| E02 | proof/receipt/plan多列和分页总量界值 | 计全部物理读取，不仅最大单列/最终DTO | PG/SQ |
| E03 | 初审超长reason、escape、Unicode二审reason | 首审完整有界、不截断；concern空白拒绝 | PG/SQ |
| E04 | preflight后JSON/TextField增长 | hydration SELECT内gate挡住，坏值不到decoder | PG/instrumentation |
| E05 | 实际wrapper、严格JSON类型、未知/重复keys | 16KiB body/4MiB响应界值明确，bool/float不冒充ID | PG/SQ |
| E06 | chunked/短读/空流/伪Content-Length | parser前按实际累计bytes限额 | protocol |
| E07 | task与assignee 100/101页长、scope/source/viewer换游标/过期/损坏、候选超长显示字段 | 分别按4MiB/256KiB有界，context128KiB且无roster；仅标量、metadata无答案 | PG/SQ |
| E08 | 成功source/写及恢复计数 | 恰好两遍有界源校验、0scorer；恢复0source，失败报实际工作 | instrumentation |
| F01 | approved+concern、rejected+confirmed | 旧release/#79按初审准入/拒绝，不假称二审门禁 | PG/双语 |
| F02 | unchecked/skipped/unselected/pending来源发布 | 不因task/意见/队列完成变approved，旧下载负例保持 | PG/SQ |
| F03 | #79 preview/freeze/worker READY/content/download/retry | supervisory意见不改变合同；权限/完整性负例保持 | PG |
| F04 | #80 capture/worker/manifest/assets/diff/已有READY | 旧schema/bytes不变，无二审放行/撤回，旧gate保持 | PG |
| F05 | annotations/files/media/export/bulk/Core-only | 无新增答案访问/发布权或Host/Core批准 | security回归 |
| F06 | 已初审源调用旧review/仲裁handoff | 原pending/selected-conflict约束不变，无自动交接/采纳/返工 | PG/SQ |
| F07 | 两语Manager→独立Reviewer→意见→返回、键盘/窄屏 | 监督效力/历史修订清楚；初审与意见并列，无“双审可发布” | 双语真实浏览器 |
| F08 | 部署/真实用户/Host/Issue关闭、CI定义vs实际运行 | 另授权记录，本轮NOT_RUN，#81/#84OPEN，保留一致性UNKNOWN | 边界审计 |

设计验收单独检查效力提案醒目、来源/状态/锁序/预算一致、原合同与拟议API分明、
56个ID完整唯一、独立review finding关闭及最终文件hash。当前不执行PG/SQ/浏览器/远端CI。
