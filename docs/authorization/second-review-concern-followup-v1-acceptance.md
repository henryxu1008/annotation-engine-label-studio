# #84 concern 受控跟进 V1 验收矩阵（设计）

2026-11-03；配套 [设计提案](second-review-concern-followup-v1-design.md)。
基线 `0143fe04e4b70dec5f7c124f85a8fe7abb5001da`。
**56 项全部 NOT_RUN；这是未来开发的验收要求，不是已通过测试。**
本轮仅检查文档、来源引用、范围和隔离，不开发测试实现或运行功能验收。

等级：API=合成 API/服务；DB=PG 和 SQLite guard/直接 SQL；PG=真实 PG 双连接并发；
UI=组件受控异步；Browser=中文/英文真实浏览器原生交互；CI=未来另授权远端。

| ID | 场景 / 所需证据 | 预期 / 禁止的副作用 | 层级 | 状态 |
| --- | --- | --- | --- | --- |
| A01 | 普通 ReviewBatch、静态 Text/Choices、approved + concern、no_change | 唯一追加处置和原因；原初审/二审/plan 全字节不变 | API/DB | NOT_RUN |
| A02 | rejected + concern、no_change | 原 rejected 不变，不增加 Approved 或清除既有返工 | API | NOT_RUN |
| A03 | 合法新修订 pending/approved/rejected 分别 revision_linked | 冻结 exact 目标和记录时状态/初审；不代替目标审核 | API | NOT_RUN |
| A04 | confirmed、pending/cancelled 二审、unselected、unchecked、skipped | 都不能形成可处置 concern；不冒充 Approved | API | NOT_RUN |
| A05 | NER/字段/bbox、动态/URL Choices、非静态 Text、媒体/HTML 装饰 | fail closed，旧二审/抽审合同不受影响 | API | NOT_RUN |
| A06 | 现有分类冲突/可信 confidence 来源，profile retired，producer grant 撤销 | 按历史读取契约验证来源；不索取新评分/producer grant，不降级普通 batch | API | NOT_RUN |
| A07 | 二审或处置完成后尝试旧 revision 再审批 / concern 直调仲裁 | 初审唯一性、pending guard 和原仲裁 cohort 资格不变；无自动任务 | API | NOT_RUN |
| A08 | active 原 owner 经现有 Editor 提交；rejected MyWork 返工链 | 原命令权限有效才产生新 revision；处置接口不编辑/派发/扩展 batch | API/Browser | NOT_RUN |
| B01 | Manager 缺 READ_WORK 或缺新 operation；只有旧 review/second_review/organize 权限 | 新 context/写/恢复分别拒绝，legacy 不自动赋新操作 | API | NOT_RUN |
| B02 | 原 submitter、assignee、初审人、二审人变成 Manager 后直调合法 body | 新处置均拒绝，不能换 principal/source 绕过同一 human ID | API | NOT_RUN |
| B03 | 分类完整 frozen cohort 的任一 participant 直接指定来源 | 全 cohort 排除；count/分页不暴露答案；独立人不足不降级 | API | NOT_RUN |
| B04 | 普通/原二审 Reviewer、Annotator、未分配用户 | 新 collection/detail/context/write 拒绝，旧二审读取边界保持 | API | NOT_RUN |
| B05 | org/project/task/assignment/submission/task UUID/opinion/target/cursor 逐项换 ID | collection 和 object scope 都拒绝；无跨域 metadata/答案泄漏 | API | NOT_RUN |
| B06 | 当前账号禁用/成员移除/Manager 降权/operation revoke，保留旧 token/UI | 新读写与原键恢复按当前权威拒绝，旧答案从 UI 清除 | API/UI | NOT_RUN |
| B07 | materialize 或 render 时撤权、identity mapper 返回新 actor/source | 丢弃答案、原子写回滚；不以初次权限检查放行最终输出 | API/PG | NOT_RUN |
| B08 | feature off、缺稳定 instance、mode core-only/未知、DB vendor 不支持 | 所有新路径包括恢复拒绝；旧功能保持原开关语义 | API | NOT_RUN |
| B09 | trusted mapped actor 与 session user 不同；客户端提交 actor/effect/time | 使用 server actor，额外字段拒绝；不借用 session Manager | API | NOT_RUN |
| B10 | 其他 Manager 读已存 metadata / source 缺失；原初审人后来降权 | metadata 只声称 not_checked，合法历史不抹掉；无隐式 source hydrate | API | NOT_RUN |
| C01 | 原二审 task/opinion/dispatch receipt/outcome/reason/time/actor 任一变形 | hash/typed/source 绑定失败，不仅校验裸 source_hash | API/DB | NOT_RUN |
| C02 | 原 plan/hash/selected/schema/algorithm/priority/version 任一不一致 | CORRUPT 或明确 unavailable；不刷新当前 batch/profile/metric | API | NOT_RUN |
| C03 | 原九 pins 逐项变更，或初审完整 reason/time/reviewer/decision 改变 | 新处置/答案拒绝，不能截断理由后校验 | API | NOT_RUN |
| C04 | 原 approved/rejected 已非 latest，新目标是合法 latest 后继 | 可关联；原来源按 frozen 历史验证，不能调用 fresh dispatch 拒绝旧源 | API | NOT_RUN |
| C05 | 目标同 task 他人 assignment、旧 revision、自己、跨项目或同内容不同链 | 资格拒绝；不能仅按 task ID/答案相同关联 | API | NOT_RUN |
| C06 | 原 assignment 改派、inactive，目标 submitter/assignee 异常 | fresh 关联拒绝；冻结 ID 不赋当前权限 | API | NOT_RUN |
| C07 | 新目标非 latest/superseded；并发出现更高 revision | context/提交复核，过时目标拒绝；不自动替换新目标 | API/PG | NOT_RUN |
| C08 | 目标 task text/config 漂移、snapshot/result hash 损坏、非静态 parser | 同链仍拒绝；不跨 schema 做貌似有效的比较 | API | NOT_RUN |
| C09 | 目标 pending 却有初审、approved/rejected 无/不符初审、自审初审 | fail closed；初审存在时完整 proof 冻结；null 语义严格 | API/DB | NOT_RUN |
| C10 | 原/目标/batch/必要 actor FK 删除再复用相同数字 ID；记录后再审核/再提交 | 新读取不重绑/替代；已存 status_at_record/receipt 不变、历史不谎称当前健康 | API/DB | NOT_RUN |
| D01 | 相同 key/body/owner 在成功和网络 UNKNOWN 后恢复 | 仅一条，恢复原 receipt；明确 replayed/not_checked | API | NOT_RUN |
| D02 | 相同 key 改 body/subject/path、不同 principal/source/actor | KEY_CONFLICT/拒绝，不泄漏其他人的回执 | API | NOT_RUN |
| D03 | 已有处置用新 key/其他 Manager/不同 disposition 再提交 | subject 唯一性拒绝，不重开/覆盖/追加第二条 | API/DB | NOT_RUN |
| D04 | 同 owner viewer 轮换保留旧 body；source/target 已失效 | 原键恢复成功，不读 batch/初审/目标/快照/producer/scorer；主体改变拒绝 | API | NOT_RUN |
| D05 | PG 双连接同 key 同 body 竞争 | 一条记录、相同 receipt，等待后重查；可证实真实独立事务 | PG | NOT_RUN |
| D06 | PG 双连接不同 key/actor/处置竞争同 concern | 一个赢家一个可解释冲突，无500、重复或丢失冻结 | PG | NOT_RUN |
| D07 | 撤权先提交 vs 处置先提交 | 前者等待写拒绝；后者合法冻结，后续读写仍服从当前权限 | PG | NOT_RUN |
| D08 | 新 submit/target review 先提交 vs 处置先提交 | stale context 拒绝或刷新；先记录的 proof 不被后来合法变化改写 | PG | NOT_RUN |
| D09 | render 来源/输出/hash/权限校验失败、事务异常 | 整体回滚且不返回答案，无半条记录、占名额或假成功 | API/DB | NOT_RUN |
| D10 | native/bulk UPDATE、DELETE、SET_NULL、字段类型1→1.0及唯一约束 | 两 DB guards 冻结业务列，允许明确 FK null/project cascade，不释放 subject；损坏拒绝读 | DB | NOT_RUN |
| E01 | 16 KiB 输入边界±1、短读流、错误编码、深层 JSON、duplicate keys/NaN | 解析前有界，结构错误400/超限422，无先 decode 大 body | API | NOT_RUN |
| E02 | reason2000字符/8192 UTF-8字节边界、空白/NUL、组合多字节 | 严格边界，不截断，不允许 no_change 空理由或错误 target-null 组合 | API | NOT_RUN |
| E03 | SQL stored plan/原 metadata/目标 snapshot/SQLite TextField 过大 | hydrate 前门禁；解码 spy/SQL 证明无巨量 payload 或 deferred 后置读 | API/DB | NOT_RUN |
| E04 | 各子项合法但请求 canonical8 MiB/stored32 MiB union 超限 | 整体422；共享 Budget，含复核读取，不能分 helper 重置 | API/DB | NOT_RUN |
| E05 | 每 snapshot256 KiB/union512 KiB、row2 MiB、plan4/16 MiB 边界 | 各门禁及总预算同时成立；超限不忽略来源或跳项 | API/DB | NOT_RUN |
| E06 | metadata union32/128 KiB、scalar超长/弱类型、4 MiB输出边界 | 对所有选中字段前置计量；渲染字节与 typed hash 双重校验 | API/DB | NOT_RUN |
| E07 | 101+ concerns、page_size1/100/101、游标换 actor/project/排序/固定选择条件/过期 | SQL 先 scope/filter/limit，页完整不重叠、计数无答案/全文 proof decode | API | NOT_RUN |
| E08 | 原 batch1000候选/其他 cohort 超大损坏；恢复分支 spy | targeted read 固定查询/验证次数；无整批 hydrate、重评分或 source替代 | API | NOT_RUN |
| F01 | 中文/英文 Manager 入口→concern→no_change→回执→历史 | 初审/二审/处置并列、正确 exact 来源与 effect 文案；不显示“双审通过” | UI/Browser | NOT_RUN |
| F02 | 两语关联新 revision、显示pending/approved/rejected及记录后变化；一侧来源无效 | 原/目标与记录时状态清楚；来源无效不返回半份投影，无误导“问题已解决/已批准” | UI/Browser | NOT_RUN |
| F03 | 请求延迟、来源/目标/项目快速切换、返回再进入、refresh/paging | 旧响应丢弃，preview 与 owner精确绑定，不污染新选择/焦点 | UI/Browser | NOT_RUN |
| F04 | 网络UNKNOWN、重试按钮、viewer轮换/owner变化、离开页面 | 同命令原键恢复；无自动换key/新body重发，未证实结果不报FAILED/SUCCESS | UI/Browser | NOT_RUN |
| F05 | 原生 Tab/Enter/Space，受控慢 context/提交；刷新/分页disabled与卸载 | 有效焦点落点和返回顺序；不落BODY，不用脚本click冒充键盘 | UI/Browser | NOT_RUN |
| F06 | approved+concern分别 no_change/revision_linked；rejected+concern同样两种 | 原approved保持旧release资格；rejected仍不合格；newpending不发布，二审/处置不是授权 | API | NOT_RUN |
| F07 | #79 READY/#80快照在两种处置前后字节/manifest/权限对照 | 真实PG+Python3.11：既存产物/审批不变；两次独立初审链不混淆 | API/PG | NOT_RUN |
| F08 | bulk/export/download/file/media/storage及客户端伪造处置字段 | 不能新增发布权或答案旁路；无审计导出新endpoint，现有下载权限负例仍通过 | API | NOT_RUN |
| F09 | 完整既有审核/抽审/返工/三类交接/二审/焦点回归 | 旧 wire/hash/guard/subject约束和两语行为保持；原有skip单列，不冒充通过 | API/DB/UI/Browser | NOT_RUN |
| F10 | 将来授权后的实际 PR/远端CI/最终SHA和merge/main CI | 复用既有治理：完整inventory、same-head warm/cold/真实部分重跑和producer/view核验；本轮不触发 | CI | NOT_RUN |

## 执行与审查规则

确认并授权开发后，先以新增意义明确的负例做 RED，再开发 GREEN；测试不可仅复刻实现。
PG 并发必须使用独立连接和可控 barrier，SQLite 不替代 PG 的锁/事务并发证据。
DB guard 要覆盖 ORM/bulk/native SQL、损坏存储与 FK 删除，不能只测 API happy path。
预算要同时提供 SQL/decoder spy 和计数证据，恢复分支证明零 live origin/target/scorer 读取。
浏览器需要真实中英文、受控异步和原生键盘事件；保留 exact source/runtime/attempt 回执。

本矩阵的本地功能通过、独立审查、远端 CI、实际 merge/main CI、部署和真实业务验收分别报告。
只有实际执行的项才能从 NOT_RUN 更新；SKIPPED、平台阻塞、UNKNOWN 不计入通过。
审计下载的产品语义和仲裁关联另行设计，不以本矩阵关闭 #84 或 #81。
