# CI 治理 V1 验收矩阵（设计阶段）

对应[设计](ci-governance-v1-design.md)，基线`15ca19c5abf48505042ee6b54bbb7194c5bf8d1e`。
**以下24项是未来实现要求，当前全部NOT_RUN。** 历史#130绿色不算新job图执行证明。
未来CI合同故障注入使用本地纯fixture或隔离非main工作流，不对protected main做破坏性探针。

| ID | 场景 | 必须观测的结果 | 未来验证 |
| --- | --- | --- | --- |
| CI01 | 原22唯一spec/23调用迁移 | 全部保留参数/config/browser/fixture/reset/timeout；无漏项/重复/机械双语翻倍 | 静态inventory合同 |
| CI02 | 新confidence加入 | 23唯一spec/24调用、固定9shard；专用config与spec内两语均执行 | 合同/真实CI |
| CI03 | 9条目录/单测及production build | 原命令和3个UI path完整保留；逐命令实际receipt/测试统计缺失或失败挡聚合，不以dist存在代替 | 静态/故障注入 |
| CI04 | 删除shard/spec/语言、空matrix、改seed/config/单spec限额 | inventory双向完整性失败，不能绿灯 | 合同负例 |
| CI05 | assertion失败/exit非0/0测试/新增pending或skipped | 结构化结果拒绝，不用“passing”或artifact存在判成功 | parser负例 |
| CI06 | 任一job失败/cancel/timeout/skipped/neutral/缺失 | 同名required聚合非SUCCESS，即使matrix父结果或部分receipt成功 | 聚合fixture/隔离CI |
| CI07 | 正常prepare+9shard | 聚合逐项核对24调用/全部语言、实际job IDs、SHA、结果后SUCCESS | 完整CI |
| CI08 | docs-only/draft/base编辑/相同head edited | 原事件全部触发、无paths/条件绕过，必需check唯一且不skip | YAML合同/隔离PR |
| CI09 | PR virtual merge、push main、dispatch ref | 各job实际checkout匹配对应事件，PRhead与virtual merge分列 | 运行日志 |
| CI10 | 不同SHA/run/recipe/inventory产物或“latest success” | 拒绝，cache不作为build证明，不能串用来源 | artifact负例 |
| CI11 | dist文件改bytes/size/path、删文件、symlink/目录穿越 | 显式manifest硬失败，工具warning不能放行 | artifact负例 |
| CI12 | build/receipt/report/diagnostics/job-API条数与bytes全部边界 | 逐项界值/超界验证；有界解析，超额非绿，无截短/静默增额 | fixture/真实build |
| CI13 | partial rerun复用旧attempt、最新失败，API默认latest遗漏旧job | filter=all/逐attempt分页；明确producer job/attempt，最新失败不被旧绿遮盖 | 聚合fixture/隔离CI |
| CI14 | partial rerun歧义/receipt缺失/同名冲突/重复逻辑execution | artifact含run/attempt/shard且不可覆盖，ID权威；歧义非绿，不猜测/无限retry | 聚合fixture |
| CI15 | 原第1次cancelled+第2次成功 | 两次历史均留，分别归因；逻辑成功不改写旧取消 | 报告合同 |
| CI16 | confidence seed settings跨进程装载 | allowlist/合成scope/profile/grant/policy生效；两语完整流程，0模型 | 专用job |
| CI17 | confidence配置错误/外置路径/任意settings导入 | CI wrapper拒绝；无产品启动或保留环境入口 | wrapper负例 |
| CI18 | shard数据库/seed/session相互隔离 | 互不覆盖；保留逐spec reseed和身份校验，max-parallel3 | 并行真实CI |
| CI19 | 原Electron/locale/browser限制 | 不偷换Chrome、不放宽5m INT/15s kill；Windows Chrome UNKNOWN保持 | inventory/运行日志 |
| CI20 | cold/warm各一次 | 每browser≤30m且hard40m；prepare≤25m；记录队列/收尾/critical path，无P95虚称 | 实测预算 |
| CI21 | 主测试结束但收尾超时/诊断上传失败 | 明确非全验收成功，测试/收尾/证据结论分列 | 隔离故障注入 |
| CI22 | 产物/日志含cookie/密码/fixture settings | 拒绝归档；仅白名单静态dist及无凭据诊断 | artifact审计 |
| CI23 | required名称/app/rules/其他workflow | Current enterprise UI validation唯一、app15368，另一必需check和规则不变 | API只读核验 |
| CI24 | local Docker/#76、旧worktree、开发/远端动作 | 设计阶段不操作；未来每阶段另授权，#81/#84OPEN | 边界审计 |

设计阶段另核对固定清单计数、证据算式、source/attempt语义、链接和独立review。
本轮不运行actionlint（workflow未改）、产品测试、浏览器、容器或远端CI。
