# Fork CI 治理 V1 设计（待确认）

2026-11-02；基线main `15ca19c5abf48505042ee6b54bbb7194c5bf8d1e`。
配套[验收矩阵](ci-governance-v1-acceptance.md)。**仅设计；workflow、脚本、配置尚未修改，
没有运行新CI或本地容器。** 功能和二审设计见[二次复核V1](second-review-v1-design.md)。

## 1. 问题、证据与顺序

实际main push run `41312708400`：
attempt1 job `124866257424` 因60分钟上限被cancelled；最后导入断言打印6 passing，
不代表进程/产物收尾成功。attempt2 job `124882263371` 同SHA成功，耗时58分04秒，
只剩1分56秒余量；11个独立locale场景串行占24分37秒。
能确认串行预算缺乏余量；首次额外耗时的具体来源没有完全定位。
19workflow/23个逻辑job的最终成功与首次cancelled历史并存，不能将其改记全attempt1成功。
[实际run](https://github.com/henryxu1008/annotation-engine-label-studio/actions/runs/41312708400)。

当前`enterprise-browser-e2e.yml` 把依赖、目录/单测、build、22个唯一spec的23次调用
放在一个60分钟job；full-enterprise-workflow按en-US/zh-CN各调用一次，
其余部分spec内部自行双语，不能机械再乘2。
confidence专用browser spec/config/seed已有本地验收，未接入任何remote workflow；
后端confidence测试已随完整`review_batches/tests`执行，不能误称远端完全没有confidence覆盖。

建议CI治理单独小切片先实施，再开发二审。设计可并行推进；不将CI修复混入二审业务实现。
首轮不删spec、不放宽断言、不增加Cypress自动重试、不以简单延长总timeout替代拆分，
不改产品代码/权限/数据库schema/branch ruleset，不接入收费并行服务。

## 2. 固定job图和全量清单

保留现有workflow文件、name、全部pull_request事件（opened/synchronize/reopened/
ready_for_review/edited）、push main、workflow_dispatch及现有concurrency语义。
所有main PR含docs-only均触发；不加paths/draft/changed-file shortcut。
`Fork PR Gate / Authorization foundation tests` 及其他workflow保持原合同。

将企业UI拆成：

1. `prepare-ui`：同checkout下完整保留现有目录/本地化/前端单测和一次production build，
   产出有界只含构建静态文件的artifact及receipt；不产出数据库、cookie、fixture密码或环境文件。
2. `browser`：固定9个shard，依赖prepare，原生GitHub matrix，`fail-fast:false`、
   `continue-on-error:false`、`max-parallel:3`；每job独立runner、PG service、服务进程和seed。
3. `current-ui-validation`：保留唯一job display name **Current enterprise UI validation**，
   用`needs`和`always()`聚合完整预期结果，不跑第二遍产品测试；任何必需证据缺失都不能SUCCESS。

以下是冻结的分片分配，不是已经执行的新job：

| Shard | spec（均在web/apps/labelstudio-e2e/src/e2e/） | 参数/fixture |
| --- | --- | --- |
| review-classification | review-batches.cy.ts；classification-disagreement.cy.ts；classification-batches.cy.ts | 原各自seed和项目mode；保留spec内双语 |
| enterprise-ui | enterprise-current-ui.cy.ts；project-members.cy.ts；task-assignments.cy.ts；reviewer-workspace.cy.ts；submission-release.cy.ts | 原seed，loginLocale=en-US |
| locale-a | dm-entity-boundary.cy.ts；auth-tips-timeout.cy.ts；app-locale.cy.ts；dm-editor-locale.cy.ts | 每spec前原seed；dm-entity-boundary保留5m INT/15s kill限额 |
| locale-b | dm-task-switch.cy.ts；dm-keyboard-ime.cy.ts；dm-entity-draft.cy.ts；dm-image-draft.cy.ts | 每spec前原seed |
| locale-c | annotator-label-stream.cy.ts；dm-export-locale.cy.ts；collaboration-locale.cy.ts | 每spec前原seed |
| full-en | full-enterprise-workflow.cy.ts | 原seed；fullFlowLocale=en-US |
| full-zh | full-enterprise-workflow.cy.ts | 原seed；fullFlowLocale=zh-CN |
| packages-import | fixed-task-packages.cy.ts；import-initialization.cy.ts | 每spec前原seed，原顺序 |
| confidence | review-confidence.cy.ts | 专用cypress.review-confidence.config.ts、一次全新confidence seed；spec内部两语 |

旧22唯一spec/23调用全部保留，加confidence后23唯一spec/24调用。
除confidence专用config外均保留`cypress.config.ts`、baseUrl、video和原locale/env；
默认Cypress Electron保持，不顺带迁移Chrome、不把Windows Chrome UNKNOWN改成PASS。
不得跨job共享seed DB；seed的幂等/拒绝复用语义保持，原每spec reset顺序不简化。

prepare保留以下9条校验调用及其全部参数/路径：
`yarn i18n:catalogs`、`yarn i18n:literals`、
`node --test apps/labelstudio-e2e/src/support/failure-diagnostics.test.cjs`、
`yarn nx run i18n:unit`、`yarn ls:unit`、`yarn nx run app-common:unit`、
原3个UI test path的`yarn jest --config libs/ui/jest.config.ts --runInBand --runTestsByPath`、
`yarn dm:unit`、`yarn lsf:unit`；再执行`yarn ls:build`。
Poetry/Python/Node版本及lockfile install不变；沿用已使用的checkout/setup/upload/download Actions，
不引入跨workflow可写凭据、pull_request_target或产品权限配置。
prepare receipt另逐项记录这9条实际命令、退出状态、适用的结构化测试数量/skip清单，
以及production build结束状态、checkout与artifact manifest hash。
聚合将它们与固定prepare清单双向核对，不能仅因存在dist或prepare job成功就推定全部命令执行。

## 3. 固定inventory、receipt与fail-closed聚合

未来在仓库增加小的CI inventory/验证helper（名称拟议），不用通用调度平台。
`ui-journeys/v1` inventory列出9个shard以及每次调用的
spec/config/locale/env/browser/seed/reset/timeout、期望语言用例、source文件hash。
先与本设计的迁移清单双向比对，再产生matrix，不能仅以“矩阵生成成功”证明覆盖完整。
未知/空shard、重复项、少一个spec/语言、变更seed/config、静默skip均失败。
后续增删清单须正常review；不能在脚本捕获错误后产生空matrix来绿灯。

每shard执行后输出有界`ui-shard-receipt/v1`，包含repository/run ID、event、workflow identity、
producer execution attempt、checkout SHA、PR head/base（适用时）、inventory digest、shard、
实际调用顺序与参数、每spec的结构化测试结果/语言用例、exit状态、timing、artifact哈希。
使用已有Cypress/Mocha结构化report能力；不得只grep“passing”或仅检验进程exit0。
期望suite为0测试、少语言、重复/缺spec、额外pending/skipped均不能算通过；
既有明确条件skip需冻结具体用例及原因，不能泛化成允许全部skip。
最小报告/结果解析helper复用已有库，新增依赖须单独说明，不手写YAML解析器或测试运行器。

明确预算：单prepare/shard receipt≤256KiB且≤128条实际调用记录；单结构化report≤4MiB，
每job全部结构化report≤16MiB；每job诊断artifact≤128MiB、≤500文件、单文件≤32MiB。
聚合最多接受10份receipt（prepare+9shard）与160MiB结构化report；按文件/流逐个有界解析，
不把所有报告一次装进内存。所有限额含JSON wrapper和UTF-8；超界为证据失败，不截断后算完整。
诊断可在保存前限量，但丢弃必须声明，不用诊断代替必需结构化结果。

聚合job始终尝试收集结果；完整结论需同时满足：

- prepare及每个必需逻辑shard的实际job均completed/success；失败、cancelled、timed_out、
  缺失、neutral、意外skipped不接受。workflow整体取消时聚合可能取消，本身也不得变绿。
- 固定9shard和全部24调用恰好一次，结构化结果逐项成功，语言/参数/fixture/timeout匹配。
- 所有receipt、运行metadata、checkout证明、构建manifest与同一源码身份一致；
  不仅依赖matrix父job聚合result，也不把artifact存在等同job成功。
- 聚合输出完整job ID/attempt/check URL和逻辑项→实际execution映射。
  使用本run的只读、分页Actions job API，明确`filter=all`或逐attempt枚举，
  最多256条job/20个attempt，超界报告证据预算失败、不截短取绿。
  以固定逻辑job/shard标识选择最新实际execution，同attempt同逻辑标识重复须拒绝。
  仅聚合job新增`actions:read`以确认实际job状态；
  不授予checks:write/contents:write，不自行伪造check。

必需check仍由GitHub Actions app `15368`提供；live ruleset、另一个必需check和无bypass约束不变。
官方说明：条件跳过可能满足required check，依赖失败可令下游skip，故需always+显式结果判断：
[GitHub required checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)、
[jobs/needs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs)。
矩阵与fail-fast使用[原生GitHub能力](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations)。

## 4. 源码/构建身份与重跑

所有job均默认checkout：PR为当前virtual merge，push main为实际pushed SHA，
dispatch为所选ref；分别记录`git rev-parse HEAD`和event metadata，不只记录PR head。
prepare manifest绑定checkout SHA、run/attempt、inventory digest、lockfile/recipe hash、
每个构建文件path/size/SHA256，总量上限256MiB、单文件64MiB、文件数5000（未来先量测再确认；
超界拒绝，不静默增额）。只归档白名单静态dist，不含源码secret/合成登录fixture。

consumer只从同run明确artifact ID下载，校验producer checkout/inventory/recipe及所有文件hash；
禁止按“最新成功”跨run/跨SHA取构建，禁止将cache当构建证明。
artifact工具的自动digest不符可能只产生warning，故consumer自身manifest校验必须硬失败：
[GitHub artifact validation](https://docs.github.com/en/actions/tutorials/store-and-share-data)。
archive路径/重复文件/绝对路径/父级跳转/symlink拒绝；从同SHA代码和lockfiles安装后校验dist，
不覆盖源代码或工作区之外路径。

重跑保持逻辑结果与实际execution分开：旧失败/取消永久记录。
artifact name含run ID、producer attempt和shard/prepare标识，使用不可变artifact，
不覆盖或按同名猜取；明确artifact ID和manifest仍是验证依据。
同一run/SHA可复用更早attempt的成功prepare/shard，但必须取明确artifact ID、原producer
attempt/job ID与hash，并用Actions metadata证明；不伪装成当前attempt重新执行。
同逻辑job选本run最新实际execution，不能选较旧成功来遮住最新失败。
若partial rerun后matrix输出/artifact映射无法无歧义复原，聚合失败并明确要求完整workflow重跑，
不按名称猜结果、不自动无限重试。此治理切片首次远端验收使用完整workflow运行；
另行受控partial-rerun负例验证混合attempt语义。
源码改变必须重新review/CI；不同SHA不能沿用产物/运行通过结论。
API历史超过证据预算时，同run继续重跑不能消除历史；需另行触发完整新run并保留旧记录，
不得清理旧证据来绕过上限。

## 5. confidence专用环境与收尾

confidence shard使用全新数据库/独立TEST_ENVIRONMENT，执行既有
`seed_review_confidence_e2e`一次；其输出包含合成登录信息，仅在本job私有文件使用。
seed输出的`settings`是合成fixture配置，不会自动留在后续runserver进程。
未来需checked-in的窄CI启动wrapper，验证fixture schema、受控settings allowlist、合成项目归属，
将instance/profiles/grants/modes/ConfidenceE2EPolicy仅装载到该job的Django进程。
不接受任意import/settings路径，不将此wrapper暴露到产品启动/保留环境，不依赖外置本地报告脚本。
readiness后执行专用config/spec的完整两语流程；零模型调用。不能仅加specPattern就声称完成接入。
密码、cookie、fixture settings文件不进入artifact/log；仅保留无凭据的结果/截图/诊断。

每job只结束自己启动的PID，CI服务container由runner生命周期管理；
本地Docker/#76没有任何操作。诊断在失败/取消可执行时尽力保留，artifact上传失败使所需证据缺失，
不能算全验收。将测试、诊断、收尾各自结果区分，不能用最后6 passing推定job成功。

## 6. 预算、阶段与验收

拟议hard timeout：prepare25分钟、每browser shard40分钟、聚合5分钟；
目标cold/warm每browser实际≤30分钟、至少10分钟收尾余量，不以两次样本宣称P95。
原dm-entity-boundary单spec5分钟及其他Cypress限额不扩大。
max-parallel3限制并行资源；总墙钟含排队和多波次，设计不承诺固定总时长。
测量cold/warm各一次的实际job/step/critical-path、cache命中、构建大小、排队与费用；
不合目标时先按真实测组重平衡，不删测试/增自动重试/无限抬timeout。
目标未达要如实报告，不以单次绿色当预算治理完成。

未来修改仅限`.github/workflows/enterprise-browser-e2e.yml`、窄CI inventory/validation/server wrapper/
构建receipt脚本、相应CI合同测试和文档；不顺带改其他18workflow或产品。
新二审用例将来另经review加入固定inventory，不继续堆回长串行job。
本轮无workflow YAML变更；actionlint/功能/PG/浏览器/remote预算验证均NOT_RUN。
只做设计文件、链接/清单/算式静态核对和独立review。#81/#84保持OPEN，Fork-only，
不开发、推送、PR、合并、部署，不动Docker/#76、旧worktree/数据/服务/会话。
