# #84 隔离验收支撑：实现与运行边界

2026-11-04。本轮只实现、离线验证和独立审查支撑代码。**没有启动服务、建库、迁移、seed、执行隔离验收或实际启用。**
原设计及 [72 项矩阵](issue84-v1-closeout-activation-acceptance.md) 保留；69 E / 2 P / 1 O 的运行状态仍为 NOT_RUN。

## 交付内容

显式入口是 `scripts/acceptance/ls84.py`。它不被产品 settings、URL、worker、CI workflow 或应用启动器自动引用。
复用既有开源库 Django、django-rq/RQ、Redis、psycopg、requests，不新增依赖或通用部署平台。

| 支撑 | 行为与边界 |
|---|---|
| `plan` | 只读取当前 Git/源文件与矩阵，输出内容摘要、分层和前置；不读取凭据、不连接服务、不创建运行目录 |
| 配置桥接 | manifest、独立执行授权记录和 fixture 均按原始字节 SHA256 绑定；配置只由固定白名单生成；拒绝任意 settings/provider/command 输入 |
| E0 | 在导入 Django 前核对实际原生 PG cluster/system_identifier/port/user/PID、Redis run_id/PID/dir/port/role/全部 keyspace；fresh 和 resume 分开 |
| 组合 seed | 一次复用 `seed_second_review_e2e` 的空库/所有权保护、ReviewBatch 和二审历史；追加合成跟进人、auditor、producer、无项目成员资格的 outsider、两人一致/冲突 pending 修订、可信合成 confidence 输入及冻结 ClassificationBatch |
| 权限 | 工具专用 provider 仅给 exact org/project/creator/title 的合成 scope 增加操作；非匹配 scope 返回空操作集，没有 Legacy fallback。项目角色、分派及独立性仍由原服务检查 |
| Web / worker | 相同入口、manifest/fixture/configuration 绑定；环境不继承云凭据、proxy、任意 provider 或数据库默认值。仅 loopback；worker 仅原生 POSIX fork，不改成同步 worker |
| HTTP/RQ 执行器 | 两语分别从真实 HTTP 新建 capture，核对原命令键、DB package/attempt、RQ job/参数/finished、独立 worker 和 workhorse PID，再经 HTTP 下载，复用 `audit_format.verify` 验证冻结 ZIP |
| 结果 | 创建 72 项 NOT_RUN 清单；真实链路即使成功也只写窄范围 observation，不自动给 G01/G02/H01 整条 case 或完整矩阵 PASS |

二审 instance 保留原 seeder 生成并冻结的值，audit/confidence 使用 manifest 的稳定 instance；不得事后重写历史记录或用新实例规避恢复。
`configuration-private.json` 冻结私有凭据文件摘要和实际 formatter/runtime 摘要；后续进程拒绝任何漂移。
进程回执绑定同一 configuration 摘要，不包含 secret、密码、token、原文或答案。
Web 工具专用 WSGI 包装器通过一次性 challenge/HMAC 证明实际 listener 的 PID 和配置；执行器在发送合成 token/业务请求前校验，
不能仅凭历史 `web-process` JSON 推断端口属于该进程。该握手仅存在于显式工具入口，不新增产品业务接口。

## 仅离线验证（本轮可以执行）

在本 worktree 根目录运行下面的支撑单元测试及 `plan`；`-B` 防止在工作目录生成字节码。
测试仅用临时文件、纯函数、伪对象/传输和本地纯 Python 子进程，不导入 Django/settings，不连接数据库或 Redis。

```text
python -B -S -m unittest discover -s scripts/acceptance/tests -v
python -B -S scripts/acceptance/ls84.py plan
```

## 未来运行准入（本轮没有执行授权）

调用运行命令需要另行明确授权的独占原生环境、资源 owner、有效期、服务/PID/文件 allowlist 和操作清单。
工具不购买、创建或启动 PG/Redis，不操作 Docker/WSL，不自动发现或复用现有资源，不导入未知 settings 来探测环境。
没有 POSIX 资源时，真实 fork worker/平台 case 保留 NOT_RUN；Windows 纯支撑测试通过不能代替它。

外部 operator 须提供一个位于所有受保护 worktree 之外的独占 run_root，包含以下已有目录和文件：

```text
run_root/
  pgdata/                   # 该 run 新建 PG cluster；真实 postmaster.pid
  redis/                    # 该 run 新建 Redis 实例的真实 CONFIG dir
  data/ exports/ reports/ tmp/ static/
  secret.txt                # 仅本 run，至少 32 字符，原样版本绑定；工具不生成/轮换
  postgres-password.txt     # 仅本 run，不使用默认密码回退
  postgres-ownership.json   # 复用 second-review-owned-native/v1 原始 bootstrap 证明
  authorization.json        # 真实另行授权的记录，不能由本轮设计/实现授权伪造
```

POSIX run_root 为 0700，凭据文件为 0600；Windows operator 另须确认独占 ACL。所有祖先目录的 symlink/junction/reparse point 拒绝。
`data/.env` 拒绝，防止产品 secret helper 加载环境污染。fresh seed 要求空业务目录/输出/私有导出、空用户表目录及 Redis **整个实例**空 keyspace；
仅新 DB number/queue 名或 TEST_ENVIRONMENT 标志不足以证明所有权。恢复只核验原身份，不错误地要求已运行的资源重新为空。

manifest 的闭合字段：

```json
{
  "schema": "ls84-isolated-run/v1",
  "run_id": "<canonical UUID>",
  "workspace": "<exact current worktree absolute path>",
  "source_digest": "<plan source_digest>",
  "run_root": "<approved independent absolute directory>",
  "instance_id": "<stable canonical UUID>",
  "http_port": 18484,
  "postgres": {
    "host": "127.0.0.1", "port": 18485, "database": "ui_ci_second_review_ls84_<unique>",
    "user": "<approved user>", "system_identifier": "<actual native identifier>", "pid": 12345
  },
  "redis": {"host": "127.0.0.1", "port": 18486, "run_id": "<actual 40-char hex>", "pid": 12346}
}
```

三个端口必须不同。PG 证明字段精确为 schema/database/port/cluster/system_identifier/fresh_public_tables，
cluster 指向 run_root/pgdata，fresh_public_tables 原始值为整数 0。
authorization 闭合字段为 schema=`ls84-execution-authorization/v1`、run_id、source_digest、phase=`isolated-synthetic`、
owner、approved_by、authorization_reference、actions（seed/web/worker/audit-cycle 的获准子集）、带时区 expires_at。
授权记录是已取得权限的留档，**不是身份认证或自授权能力**；没有真实用户授权不能通过自写 JSON 获得操作权限。
授权摘要及有效期绑定到各进程及握手；Web 每个新请求、worker 每次取任务/作业入场、执行器每个 HTTP 前重新检查期限。
到期后不再承认新的业务入场，worker 在正常 Redis 连接下的空闲等待受剩余期限限制；不杀正在执行的原作业、不回撤已提交命令或已开放 FD。
过期过程中队列/进程的必要收尾记录与原 UNKNOWN/lease 保留，不能把到期拒绝描述成全部取消或零副作用。
RQ 内部 Redis ConnectionError 的指数退避没有在本轮执行/证明有界退出；作业入场仍重新拒绝过期权限，
但这不等于 worker 进程已停止或已建立全局 D_closed。断连、进程停止和在途排空仍须未来分别验收。

未来显式命令的参数形式（仅说明，不代表本轮授权执行）：

```text
python -B scripts/acceptance/ls84.py <seed|web|worker|audit-cycle> --manifest <file> --manifest-sha256 <raw-bytes-hash> --authorization-sha256 <raw-bytes-hash> [--fixture-sha256 <seed-output-raw-bytes-hash>]
```

seed 不接收 fixture；其余三个命令必须传 seed 产出的 fixture 原始摘要。Web/worker 独立启动，不由执行器隐式拉起。
原 `approved_exports` queue、Redis URL 与 private root 继续复用，不发明 audit 专用 queue；工具 worker 对同一获准自有 queue
里的原 `data_export.approved.generate` 和 `data_export.snapshots.generate` 保持执行路径，不能因此宣称 #79/#80 回归已经通过。

## 预算、失败与结果

单 locale 的审计链端到端预算 1850 秒，包含 preview/capture/poll/download/verify；双语顺序运行，链路总上限 3700 秒，
独立于既有 CI/browser 的 300 秒 +15 秒规则，不修改任何原作业、清单、测试或上限。
HTTP 有连接/读取超时及累计 deadline/大小检查，JSON 最多 512 KiB（通用闭合 JSON 读取还受 256 KiB 限制），下载最多 16 MiB；
产品 SQL/byte/worker 等原上限不扩大。持续慢速响应也不得在超时后写成功回执；单次阻塞可能另有不超过 5 秒的 socket 收尾时间。

seed-intent、cycle-intent、每次 capture exact body/key 在相关写入前 exclusive 创建。失败留下原证据与 UNKNOWN，拒绝覆盖/自动重跑。
没有自动 retry/cancel/purge/delete/新 capture 替换旧 T、手改时间/lease/state 或清理受保护资源的入口。
原命令恢复、故障注入、并发、停用/排空/配置回退以及其余矩阵场景仍须未来按矩阵执行并逐项审查；本执行器不冒充全部 69 项自动化。
程序的 COMPLETED 只表示该支撑命令返回，不表示真实业务、P/O 准入或完整 E 验收通过。

实际权限 provider、真实 confidence producer/校准、Host 自定义 identity 完整审计兼容仍是原设计的独立前置/延期边界。
Fork-only、静态文本分类、supervisory_only、固定脱敏；#81/#84 保持 OPEN。Docker/#76、主仓、其他 worktree 和既有证据不动。

## PR #136 兼容性修订（本地，尚未授权推送）

远端对原提交的复审指出三个支撑缺陷：POSIX tracked symlink 被来源摘要误拒绝、seed 的 scope 登记晚于首次提交、
内部 APIClient 的 testserver 主机被工具桥接拒绝。旧提交的原有 CI 不发现本次支撑测试，不能证明这三个路径通过。

来源摘要现在按 Git index mode 处理：120000 仅哈希链接文本，不跟随文件/目录/悬空链接目标；
明确 core.symlinks=false 的检出占位文件按相同 link-text 语义哈希。普通 tracked/untracked 文件及其父路径的
symlink/junction/reparse 替换仍拒绝；冲突 index stage、gitlink 等不支持类型明确拒绝，不把平台兼容变成越界读取。

既有受保护 supervision 管理命令新增默认无操作的 on_project_ready hook，仅在本次新建 Project 和成员后、
任何 create_submission 前调用。工具专用命令子类登记真实新项目 exact scope；seed 阶段 provider 仅授予该 scope 的 SUBMIT，
未登记/跨 scope/未知阶段仍为空操作集。额外 pending 数据复用同一已登记 scope，完整授权只在组合 fixture 的 ready 阶段生成。
没有全局 post_save、Legacy fallback 或通用 TEST_ENV 授权。工具 ALLOWED_HOSTS 仅追加内部 testserver，产品默认配置不变。

新增独立 workflow `Isolated Acceptance Support Contracts`，单个 Linux Python 3.11 作业 **5 分钟**上限，
只运行 `python -B -S scripts/acceptance/check_support.py`。它自动发现全部支撑单元测试，阻断产品/数据库/队列导入及 socket 网络，
再执行只读 plan，不安装第三方依赖、不启动任何服务、不运行 migration/seed/web/worker/audit-cycle。
原有19个 workflow、UI 11 shards/26 calls 和授权 producer/gate 预算保持；若此修订获准推送，将多一个纯支撑 workflow/作业，
整轮理论上限增加5 runner分钟，不能把新增作业算作原有清单或声称实际费用已知。

本地 Windows 的原生 POSIX 文件/目录/悬空链接测试明确 SKIPPED，待新 SHA 获准推送后由 Linux CI 补证；
纯 link-text/fake 控制流验证不冒充真实 POSIX 或完整 seed 执行。72 项隔离运行矩阵仍全部 NOT_RUN。
