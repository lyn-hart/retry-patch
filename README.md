# Retry / Backoff 补丁脚本 — 使用说明

> 操作前先**退出正在运行的 codex / claude**；用户 npm 目录（`~/.npm-global`）下的安装**无需 sudo**；两个脚本都支持预览（`--dry-run`）和 `--restore` 还原，打补丁前自动备份为 `<目标文件>.orig`。

本仓库只保留 Linux / macOS（ELF / Mach-O）版本：

| 工具 | 脚本 | 已验证目标 |
|---|---|---|
| codex | `codex-linux.py` | codex 0.172.0（Linux x64 ELF） |
| claude | `claude-linux.py` | Claude Code 2.1.331（Linux x64 ELF） |

两个脚本启动时都会**检测当前系统环境**（`os.name`），在 Windows 上直接拒绝运行；Windows 版脚本已移除，如仍需要请在 WSL 内使用这里的 Linux 版。

- 两边都遵循同一条定位原则：**能用语义就不用字节串**，判据必须多条件同时成立，且可以在 `--dry-run` 里打印出来供人核对。
- codex 补丁处理的是**真实机器码**（ELF/Mach-O 的 `vaddr − offset` 段映射）；claude 补丁处理的是二进制内嵌的**压缩 JS 文本**。两者互不影响。

---

## codex

codex 有 **7 处**补丁站点：1–5 和 7 管等待与重试预算，站点 6 把裸 429 重新变成可重试；间隔全部压成固定 1 秒：

| # | 位置 | 原行为 | 补丁 |
|---|---|---|---|
| 1 | `codex-client/src/retry.rs::backoff` | `base × 2^n × jitter` | 固定 `RETRY_MS` |
| 2 | `core/src/util.rs::backoff` | `200ms × 2^n × jitter` | 固定 `RETRY_MS` |
| 3 | `stream_max_retries().min(100)` | 上限硬编码 100 | 上限抬到 `STREAM_MAX_RETRIES` |
| 4 | `err.retry_delay().unwrap_or_else(backoff)` | 服务端 `Retry-After` 优先，站点 2 被跳过 | NOP 掉 `jne`，退避无条件生效 |
| 5 | `unbounded_connection_retries` 阶梯 | `from_secs(5)` 倍增到 60s | 每次都读成 1s |
| 6 | `ApiRetryConfig.retry_429` | 硬编码 `false`：裸 429 直接终态，1-4 站点全不执行 | 翻成 `true`，429 变成 HTTP 层可重试 |
| 7 | `request_max_retries().min(100)` | 上限硬编码 100，站点 6 的预算被卡死 | 上限抬到 `REQUEST_MAX_RETRIES` |

> **只打 1、2 是不够的**：站点 4/5 是 0.153.x 里真正决定间隔的两条路。站点 4 让 codex **无视 `Retry-After`**——这正是补丁的目的，但别拿它去打会因为高频重试而惩罚你的端点。站点 5 的字段只能存整秒，`RETRY_MS` 会四舍五入到最少 1s。

> **站点 6 是裸 429 能被重试的前提**：codex 的 HTTP 层 `RetryOn::should_retry` 本来就写了 429，但 provider 构造里 `retry_429: false` 把它关了，429 于是变成 `CodexErr::RetryLimit`；而 `retry_delay()` 对 `RetryLimit` 只认服务端 `Retry-After`，没有 `backoff()` 兜底——不带 `Retry-After` 的 429 会**一次都不重试**就结束这一轮。只打站点 1-5 时，这个 429 永远绕不到它们。
>
> 注意重试**次数**走的是 `request_max_retries`（默认 4，原硬上限 100，站点 7 抬到 9999），不是 `stream_max_retries`。要让长时限流墙多等一会，请在 `config.toml` 里显式设置它（例如 `request_max_retries = 50`）。

> **站点 7 是站点 6 的预算开关**：`request_max_retries()` 有 `.min(100)` 硬上限（就是上面那个 100），站点 3 只抬了 stream 那一半；不抬这个的话，`config.toml` 里写 9999 也只跑 100 次。站点 3 和站点 7 分别管 stream 与 request 两个上限。

### 核心规则：只改「失败之后等多久」

**补丁只允许修改「已经判定失败之后，下一次尝试前等多久」。绝不修改「什么算失败」，也绝不修改「多久才判定为失败」。** 这是 codex / claude 补丁的硬性约束，新增站点也必须遵守。

理由：等待时长是纯粹的数值，改错了最多是节奏不对；而失败判定改错了会把「模型正在思考」误判成断流、把成功的流当成错误中断，甚至把不可重试的致命错误变成死循环。这两类逻辑本来就有配置项，不该用改机器码的方式碰。

> **唯一的例外是站点 6，且必须保持得很窄。** `retry_429` 确实属于「什么可重试」，但 codex 本身已经把 429 归类为限流信号，`RetryOn::should_retry` 里也有现成的 429 分支，只是 provider 字面量把它关掉了；站点 6 只是把这个开关拨回去。它只会重发一个**已经被服务端拒绝**的请求（且下一次请求仍要先等满间隔），不可能把「模型正在思考」变成失败，也不会把致命错误变成死循环。新增其他任何站点仍须遵守上面的硬性约束。

由此推出的三条：

1. **看到的间隔 = 判定失败耗时 + 1s**，前半段脚本管不到。最坏情况是 SSE 静默满 `stream_idle_timeout_ms`（默认 300000ms = 5 分钟），所以间隔可能是 301 秒而不是 1 秒——这不是补丁失效。
2. **想压缩前半段只能改 `config.toml`**（`stream_idle_timeout_ms`），不要去改二进制里的超时常量。
3. **审计方式**：打完补丁后 `.orig` 与新二进制的字节 diff 只能落在抖动退避、Retry-After、连接阶梯、stream cap、`retry_429`、request cap 这六类改写上（站点 6 固定 1 字节、站点 7 固定一段）。站点 4 比 0.156.1 多了两处对 `retry.rs::backoff` 的调用。地址随版本变化，段数变了要重新对 `--dry-run`。下面是 0.159.1 Linux x64 的 11 段示例（那一版还没有站点 6/7）：

```
[0x51633c8, 0x51633f7)  47B  站点2 util.rs     [0x6ee34cd, 0x6ee34d1)   4B  站点3
[0x51c5fc6, 0x51c600a)  68B  站点1 retry.rs    [0x74d4819, 0x74d481b)   2B  站点4
[0x6dcdbca, 0x6dcdbd1)   7B  站点5             [0x7569978, 0x756997a)   2B  站点4
[0x6dccfe0, 0x6dccfe2)   2B  站点4             [0x756a584, 0x756a58b)   7B  站点5
[0x6e9f132, 0x6e9f136)   4B  站点3             [0x760fe76, 0x760fe7a)   4B  站点3
                                              [0x89853a3, 0x89853a5)   2B  站点4
```

### 什么算「失败」（脚本不碰这部分，仅供对照）

判定入口是 `core/src/session/turn.rs`：`if !err.is_retryable() { return Err(err); }`，只有返回 true 的错误才会走到重试等待。分类见 `protocol/src/error.rs::is_retryable`：

| 会重试（等 1s 后再来） | 不重试（当场结束这一轮） |
|---|---|
| `Stream` — SSE 在 `response.completed` 之前断开，**空闲超时也算这一类**（`codex-api/src/sse/responses.rs` 发的是 `ApiError::Stream("idle timeout waiting for SSE")`） | `UsageLimitReached` — 套餐额度用尽 |
| `RateLimitExceeded` — 流内的上游限流 | `ServerOverloaded` — 所选模型满载 |
| `RequestTimeout` — HTTP 请求超时 | `QuotaExceeded` / `UsageNotIncluded` — 计费问题 |
| `ConnectionFailed` — 连不上（**另走站点 5 的阶梯**） | `ContextWindowExceeded` — 上下文塞满 |
| `UnexpectedStatus` — 非预期 HTTP 状态码 | `InvalidRequest` / `InvalidImageRequest` |
| `ResponseStreamFailed` | `CyberPolicy` / `MisalignmentPolicyViolation` — 策略拦截 |
| `InternalServerError` — 上游 5xx | `Interrupted` / `TurnAborted` — 用户 Ctrl-C 或中止 |
| `Io` / `Json` / `TokioJoin` — 传输和解析层 | `RetryLimit` — 已经重试到上限 |
| `Timeout` — 子进程等待超时（与网络无关） | `Sandbox` / `Spawn` — 本地执行问题 |

HTTP 请求层（站点 1，受 `request_max_retries` 管）另有一套更窄的判定，见 `codex-client/src/retry.rs::RetryOn::should_retry`：只重试 **429**、**5xx**，以及 `Timeout` / `Connection` / `Network` 三类传输错误，其他状态码一律直接返回。

> 这套判定里 429 那一行一直被 provider 构造的 `retry_429: false` 关着（见站点 6），所以 429 实际会落成 `CodexErr::RetryLimit`：没带 `Retry-After` 就**一次都不重试**，带 `Retry-After` 则按它等待。站点 6 把开关拨回去后，裸 429 才真正走上面的 HTTP 重试，固定在 1s 一次。

> 注意 `UsageLimitReached` 不可重试，所以站点 4「无视 `Retry-After`」影响的不是真正的配额墙（那种情况直接结束，根本不等），而是 429 / 流内限流这些**可重试**错误上服务端要求的等待时间。

### Linux / macOS — `codex-linux.py`

```bash
# 1. 预览（不修改）
python3 codex-linux.py --dry-run

# 2. 打补丁：7 处合计固定 1s + stream/request 上限各抬到 9999
python3 codex-linux.py

# 3. 还原
python3 codex-linux.py --restore
```

> 重试间隔与两个上限已固定为脚本内常量 `RETRY_MS = 1000`(毫秒)、
> `STREAM_MAX_RETRIES = 9999`、`REQUEST_MAX_RETRIES = 9999`，**不提供
> `--ms` / `--max-retries` 命令行参数**；如需改值，直接编辑脚本顶部这三个常量。

实际重试**次数**仍由 `config.toml` 决定（脚本只抬高上限，不填就还是默认的 `stream_max_retries = 5` / `request_max_retries = 4`）：

```toml
[model_providers.custom]
stream_max_retries  = 9999
# 站点 6 让裸 429 可重试后，重试预算就是这个（默认 4）。站点 7 把原来的硬上限
# 100 抬到了 9999，所以这里写多少就是多少（≤ 9999）。
request_max_retries = 50
# 顺带把“多久才算这次失败”也压下来：默认 stream_idle_timeout_ms 是 300000（5 分钟），
# SSE 卡住时要耗满它才会计一次重试，看起来就像间隔远大于 1s。
stream_idle_timeout_ms = 15000
```

### 参数（codex）

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只检查打印，不写入、不备份 |
| `--restore` | 从 `.orig` 还原 |
| `--binary <路径>` | 手动指定原生 `codex` 二进制 |
| `--self-test` | 内部自检 |

### 版本适配说明（重要）

脚本靠识别二进制里特定的机器码模式来打补丁，**codex 升级后模式可能失效**。**codex 0.172.0** 已在本机 Linux x64 ELF 上通过自检、`--dry-run`、副本打补丁后的重复识别与启动检查（`--version`），并已写入安装的二进制，站点 1-7 全部命中：抖动 3 处、stream cap 5 处、Retry-After 25 处、连接阶梯 3 处、429 开关 1 处、request cap 1 处；未验证真实 API 重试行为。此前 **codex 0.167.2** 已在本机 Linux x64 ELF 上通过自检与 `--dry-run`，站点 1-7 全部命中（未向安装的二进制写入补丁）：抖动 3 处、stream cap 3 处、Retry-After 3 处、连接阶梯 2 处、429 开关 1 处、request cap 1 处。此前已验证 **codex 0.162.1**（Linux x64 ELF，站点 1-7 全部命中）、**0.159.1**（Linux x64 ELF，站点 1-5 全部命中）。更早的 **v0.156.1 / v0.153.4**（Linux x64 ELF）以及 **v0.143.0 / v0.144.1**（Linux x64 ELF 与 Windows x64 PE 的站点 1-3）也适配过；Windows 版脚本（含 0.159.2 PE 上的站点 1-5 验证）已从仓库移除，下面保留的 PE 细节仅作历史参照。

- 每次 codex 升级后，先跑 `--dry-run`：站点 1/2 必须同时列出 `retry.rs::backoff` 和 `util.rs::backoff`（缺一个脚本会直接退出）。站点 5 在 Linux 0.172.0 上是 3 处；站点 4 是 25 处，都要带 `verified: call ... lands in a patched ...backoff`。站点 6 是 1 处（`HTTP 429 retryability` 小节，`movw $0x0100`）；若该节报 `NOT FOUND`，裸 429 仍会一次不重试。站点 7 也是 1 处（`request_max_retries hard cap` 小节），带 `linked: result feeds max_attempts ...`；若该节报 `NOT FOUND`，该上限保持原值，汇总明确显示 `NOT PATCHED`，不会误报已抬到 9999。若报错（如 `expected at least 2 jittered backoff sites` 或 `expected both retry.rs::backoff and util.rs::backoff`）说明字节码又变了，需要重新适配。
- **内置自检**：`python3 codex-linux.py --self-test` 覆盖站点 6/7 的纯字节用例（disp8/disp32/REX.B、四条 store 缺一不可、站点 6→7 的寄存器链接），以及 0.167.2 的内存 Option、保留标志的寄存器 MOV、拒绝覆写标志/累加器、重复识别、截断输入和未命中的汇总；0.172.0 新增分离映射下的抖动常量解析、站点 4 的 `jne` 直落布局和站点 5 的 GOT 尾部。
- **v0.167.2 → v0.172.0 变了什么**：
  1. **`.rodata` 与 `.text` 不再同一映射**：两个 PT_LOAD 的 `vaddr − offset` 分别是 0 和 0x1000。旧脚本在文件偏移空间解析 RIP 相对操作数，算出的抖动常量地址整体偏 0x1000，站点 1/2 一处都找不到（`found 0`）。现在统一经段表做 VA↔文件偏移换算。
  2. **站点 4 多了一种布局**：共享的 `CodexErr::retry_delay` 变成 `cmp r32,-1 ; jne <Some 分支> ; <backoff 参数> ; call backoff ; ret`，backoff 成了直落分支。处理方式是把 `jne` 换成等长 NOP；判据是直落处解码出参数装载并落到 unwind 表确认的 backoff 入口、call 之后紧跟返回 epilogue、Some 分支以解码出的「剩余等待」结果加同一 epilogue 收尾。同函数里另一条 `cmp r32,-1 ; je <返回 None>`（RetryLimit，无回退）够不到 backoff 调用，不会被改。其余 24 处是 `je` 形式的请求重试单态化副本，沿用 0.164.0 的判据。
  3. **站点 5 的尾部**：两条 store 之后的 `warn!` 等级检查从 `lea r64,[rip]` 变成经 GOT 的 `mov r64,[rip]`，两种都接受；阶梯夹取离加载点最远 0xf0e 字节，窗口从 0x1000 放到 0x2000（整个镜像只有 5 对这样的比较）。
- **v0.167.2 的站点 7**：Option 判别改为 `cmp byte[option],0`，中间还调度了一条保存 Option 指针的寄存器 `mov`。新版检测器要求此前的配置值加载来自同一 Option 的 `+8` 字段，只允许一条不覆盖累加器、不修改标志的寄存器 MOV；实际改写仍只覆盖 `cmp/mov/cmovb` 的 13 字节，Option 判别、默认值 4 和后续代码全部保留。
- **v0.156.1 → v0.159.1 变了什么**：两个 backoff 的结构判据没变（116 个 `from_millis` 尾部里仍恰好 2 处）。`retry.rs` 把 `Duration` 换成毫秒的 `imul * 1000` 挪到了尾部前 445 字节，超出原来的 320 字节抖动窗口，标签要单独回扫 1024 字节，否则两处都会被印成 `util.rs::backoff`。站点 4 从 2 处变成 4 处，多出来的两处 call 落在 `retry.rs::backoff` 里，验证通过就照打。
- **v0.142.4 → v0.143.0 变了什么**（供下次排查参照）：
  1. 抖动 `random_range(0.9..1.1)` 的编译产物从「相邻 `0.9`/`1.1` 常量对」改成「下限 `0.9` + 区间宽度 `0.2`」，且 `0.9` 常量被两个 backoff 去重共享，旧的「相邻 0.9/1.1 对」定位失效。
  2. 两个 backoff 函数都被**内联**进各自的 async poll，不再有独立入口，旧的「覆盖函数入口写返回 stub」打法会毁掉整个 poll 函数。
- **v0.144.x 起第二个 backoff 用 `movsd [0.9]`**（Linux ELF 与 Windows PE 均如此），所以指令助记符本身不能作为判据；脚本对 `movsd` / `addsd` / `subsd` 一视同仁，靠下游结构定案。

### 定位原则：尽量不靠字节模式定案

**能用语义就不用字节串。** 每个站点都要求「多个独立条件同时成立」，而不是匹配一段特定字节；单条件命中一律不改。这样 codex 换寄存器分配、调参数、换指令助记符时脚本仍然有效，而且判据本身可以打印出来供人核对（`--dry-run` 里每个站点都有一行 `verified:` 或 `anchor:`）。

| 站点 | 判据（全部满足才改） | 不再依赖 |
|---|---|---|
| 1/2 | 遍历**全部** `from_millis` 内联尾部（0.153.4 有 110 处），要求①之前有寄存器形式 `mulsd`②再往前有一个「抖动倍率」区间常量（`0.5 < c < 2.0` 且 `≠ 1.0`）③两者之间跨度合理。三条同时成立的恰好 2 处 | 硬编码 `0.9`、「全局只能有一个 0.9」、指令助记符 |
| 3 | 只匹配固定中段（`cmovb` + Option 判别），**上限和 `unwrap_or` 默认值都当立即数读出** | 硬编码 `mov eax,5`、上限 100 |
| 4 | 跟着 `call` 穿过 GOT 槽拿到函数入口，确认站点 2 改过的抖动代码落在函数体内 | 寄存器编号、`cmp/jne` 的具体编码 |
| 5 | ①7 字节 load pair 形状②后随两条 store + RIP 相对 `lea`（0.172.0 起为经 GOT 的 RIP 相对 `mov`，两者都接受）③同一函数内有 `.min(cap)` 的**成对** `cmp r64,cap+1` / `cmp r64,cap`（上限值读出来，不假设是 60） | 硬编码 60s、单条 `cmp`（会误命中 tracing 等级检查） |
| 6 | ①一条 `movl $nanos,disp`（nanos 取值落在合法 Duration 区间）②同基址、位移恰为 `disp+0x08` 的 `mov r64, mem`（记下源寄存器给站点 7）③同基址、位移恰为 `disp+0x10` 的 `movw $0x0100`④紧随其后、位移为 `disp+0x12` 的 `movb $1`。四条 store 都用同一套 mem 操作数解码（disp8/disp32/REX.B），位移关系而非绝对值定案，`movw` 的高字节（retry_5xx=1）同时校验 | 硬编码 `0x98`/`0xa8` 偏移、硬编码 base_delay 200ms、硬编码寄存器 |
| 7 | ①`cmp val,imm8` + `mov acc,imm32`（两个立即数必须相等，正值且小于 128）+ `cmovb` + Option 判别（`test opt,opt`，或 `cmp byte[option],0` 且前置 `mov val,[option+8]`）+ 可选一条保留标志且不覆盖 acc 的寄存器 MOV + `mov res,default` + `cmovne`，寄存器逐一对应②结果寄存器必须等于站点 6 字面量装载 `max_attempts` 的寄存器，已打补丁形态也必须满足③必须位于该字面量 ±0x800 范围内。②是语义链接，排除无关同形夹取 | 硬编码寄存器（r13/rax/rsi、0.167.2 的 rax/rcx/rbp）、硬编码默认 4、硬编码偏移 |

配合几道兜底：站点 5 命中超过 4 处、站点 6/7 命中超过 1 处直接拒绝（宁可不打也不猜），站点 1/2 覆写区间必须落在 5~256 字节。

实测收益：站点 1/2 换成结构判据后，把抖动区间改成 `0.75..1.25` 或 `0.8..1.2` 都还能命中；站点 3 换掉默认值 5 或上限 100 都还能命中；站点 5 把 60s 上限改成 30s 也还能命中；站点 6 在 disp8/disp32/REX.B 三种编码下都能命中，站点 7 用同一套解码跟上（含 REX.B/disp8 的寄存器链接）。这些都在 `--self-test` 里有对应用例。
- **站点 4/5 怎么定位的**（0.153.4 起新增，两处都是「就地等长改写」）：
  - 站点 4 靠 `Option<Duration>` 的 niche 编码找：`nanos == 1_000_000_000` 就是 `None`，所以分支是 `cmp <r32>,0x3b9aca00 / jne <跳过 backoff>`。光有这个模式不够（整个二进制里 `cmp <r32>,1e9` 有 680 处），所以脚本**必须**跟着 `call` 走到目标函数、确认它包含站点 2 改过的抖动代码，才肯把 `jne` 换成等长 NOP。0.153.4 里 backoff 是通过 GOT 间接调用（`call [rip+…]`），所以这里需要 VA↔文件偏移双向映射。
  - 站点 5 改的是**读取**：`mov <secs64>,[<base>+0x10] / mov <nanos32>,[<base>+0x18]` → `mov <secs32>,1 / xor <nanos32>,<nanos32>`，两边都正好 7 字节。识别条件是紧跟其后的「两条 disp32 store + 一条 RIP 相对 `lea`」——那条 `lea` 是必需的：无关代码里 `mov eax,60 / xor ecx,ecx` 加两条 store 长得跟已打过补丁的站点一模一样（实测有 6 处），只有 RIP 相对 `lea`（`warn!` 的静态等级检查）能把它们排掉。命中数超过 4 处时脚本直接拒绝打站点 5。
  - 站点 6 锚在 `movw $0x0100` 上：`rustc` 把 `ApiRetryConfig` 字段重排为「`base_delay`(16B) → `max_attempts`(8B) → `retry_429`/`retry_5xx`/`retry_transport`」，所以字面量编译成「nanos 的 `movl` → max_attempts 的 `mov r64` → 打包二布尔的 `movw` → transport 的 `movb`」。四条 store 都要在，判据只用**相对位移**（max_attempts = nanos + 0x08，布尔 = +0x10，transport = +0x12），不写死 0x98/0xa8；并用 `Duration::nanos ≤ 999_999_999` 把无关 nanosecond 运算筛掉。max_attempts 的源寄存器留给站点 7。0.162.1 上恰好 1 处，多了就拒绝。
  - 站点 7 用**寄存器链接**定位：`request_max_retries()` 的 `.min(100)` 与站点 6 同在 `to_api_provider`，其相位就是站点 6 字面量装载 `max_attempts` 的那个寄存器。二进制里同形的 `min`+`unwrap_or` 有 11 处（大多是无关的 `unwrap_or(1).min(...)`），只有这一处的结果寄存器与该字面量一致。改写是把整段 13/14 字节的 `cmp/mov/cmovb` 换成真正的 `.min(9999)`（`cmp imm8` 装不下 9999，只能整体改写）。

#### 平台相关的实现细节（历史参照）

当前脚本只处理 Linux / macOS。Windows PE 版脚本已移除，以下两条记录当时 PE 与 ELF 的编码差异，供将来重新适配时参考：

- **RIP 相对寻址必须在虚拟地址(VA)空间解析**。Linux 版按段表做 VA↔文件偏移换算（0.172.0 起 `.rodata` 与 `.text` 的 `vaddr − offset` 分别是 0 和 0x1000，不能直接用文件偏移比较）；Windows 版则需走 `build_pe_off2va()` 换算到 VA，因为 PE 的 `.text` 与 `.rdata` 的 `VA − 文件偏移` 增量不同（`FileAlignment 0x200 ≠ SectionAlignment 0x1000`）。
- **MSVC 把毫秒值分配进扩展寄存器 `r8`/`r9`**：`from_millis` 尾部是 `4c 89 c0`（REX.W+REX.R 的 `mov rax,r8`）而非 musl 的 `48 89`。识别时需同时接受 `48`/`4c`，补丁对 `r8–r15` 需加 `41`(REX.B) 前缀写出 `41 b8 <imm32>`。

---

## claude

`claude-linux.py` 仅针对 **Claude Code 2.1.331** 的代码结构，不保留旧版兼容。

Linux/macOS 脚本处理以下 6 类重试逻辑：

| # | 位置 | 原行为 | 补丁 |
|---|---|---|---|
| 1 | `CLAUDE_CODE_MAX_RETRIES` 读取函数 | 将环境变量夹到内部上限 | 将夹取条件改为恒假 |
| 2 | API 退避包装函数 | 委托共享函数计算指数退避，接受自定义基础延迟 | 默认 base→1000ms、传给共享函数的 attempt→1；忽略 Retry-After |
| 3 | SDK `calculateDefaultRetryTimeoutMillis` | `min(0.5×2^n, 8)` 秒 | 系数→1、指数底→1，约 0.75–1s |
| 4 | `Retry-After` / `ratelimit-reset` 读取 | 服务端要求的等待优先于退避；`Retry-After` 超过 60s 时直接放弃（`retry_after_too_long`） | 忽略头值，回落到补丁后的退避；放弃守卫读到的头值改为 0 |
| 5 | 限流（fast mode）冷却 | `max(头值 ?? 30 分钟/10 分钟, 10 分钟)` | 冷却表达式就地写死 `1000`；内联睡眠阈值抬到 99999ms |
| 6 | 重试循环里的 2 处延迟赋值 | `min(reset头 ?? 退避(...), 上限)` 或带自定义 base 的退避 | 就地写死 `=1000` |

> **站点 6 不是冗余**：reset 头非空时，`??` 会短路掉退避函数；另一路调用可显式传入基础延迟。必须改赋值本身，才能避免这两条路径绕过默认 base 的补丁。
>
> **站点 4 的放弃守卫**：2.1.306 及之前该守卫比较的是计算出的延迟，站点 6 写死 `=1000` 后自然不触发；2.1.331 改为重新读取 `Retry-After` 头，因此需要单独一处改写才能保持「长 Retry-After 不终止重试」的原有补丁效果。30 分钟默认冷却常量被其他 fast mode 冷却共用，故站点 5 不改常量、只改这一处表达式。

### 核心规则：不改失败判定和超时

除解除重试次数上限外，**只修改已经判定失败之后、下一次尝试前的等待**；不修改「什么算失败」，也不修改「多久才判定为失败」。

### Linux / macOS — `claude-linux.py`

```bash
python3 claude-linux.py --dry-run     # 预览
python3 claude-linux.py               # 打补丁
python3 claude-linux.py --restore     # 还原
```

打补丁后设置环境变量再运行 claude（脚本不会自动修改 shell 配置）：

```bash
export CLAUDE_CODE_MAX_RETRIES=9999            # 重试次数上限（站点 1 已解除内部夹取）
export CLAUDE_CODE_RETRY_WATCHDOG=1            # 429/过载持续重试，并解除 15 次上限
export BUN_JSC_forceDebuggerBytecodeGeneration=1 # 让 standalone Bun 使用已修改的源码
```

> npm 包入口 `bin/claude.exe` 在本机 Linux 上是 ELF 二进制，扩展名不代表格式。写入使用同目录临时文件原子替换，保留原所有者与权限；正在运行的会话仍使用旧二进制，需要重启才生效。

### 定位原则：只认代码形状，不认压缩名和魔数

补丁修改二进制内嵌的压缩 JS 文本。名字和数值从结构匹配结果中读取，不写死压缩标识符：

| 站点 | 当前判据 |
|---|---|
| 1 | 唯一的夹取守卫及警告字符串；比较条件、警告和返回值必须绑定相同变量 |
| 2 | 完整的 `Math.round(shared({attempt,baseMs,capMs,jitter,random}))` 包装函数（含其后的 `Retry-After` 解析与 `Math.max` 返回）；默认 base 参数及各处引用必须一致，base/ratio 成对声明只在函数上方 4KB 内唯一解析。只固定这个包装函数的 attempt，不改共享函数及无关重试路径 |
| 3 | SDK 稳定方法名 `calculateDefaultRetryTimeoutMillis` |
| 4 | 协议字符串 `retry-after` / `retry-after-ms` / `anthropic-ratelimit-unified-reset`；重试循环内 `err instanceof X ? parser(header(err)) ?? null : null` 及放弃守卫（锚定遥测事件名 `tengu_api_retry_after_too_long`）使用的解析器必须与站点 2 包装函数调用的是同一个 |
| 5 | 阈值变量与冷却表达式都绑定站点 4 读出的同一 Retry-After 变量；冷却表达式须在其后 2KB 内唯一，并以 `"overloaded":"rate_limit"` 及 `Date.now()+冷却` 调用收尾；阈值声明须全局唯一 |
| 6 | 遥测事件 `"tengu_api_retry"` 的 `delayMs` 字段绑定延迟变量，只替换附近完整的退避赋值表达式 |

安全约束：

1. **拒绝歧义**：目标形状不存在或不唯一时跳过并打印诊断，不猜测位置。短压缩名可在无关模块复用，常量分组定位避免误改同名变量。
2. **所有改写等长**：必要时使用指数写法（`500`→`1e3`）或尾部空格，不用可能被解析成八进制的前导零。
3. **识别已打补丁状态**：同样按结构识别当前值。重复 dry-run 不写文件；全部站点已打补丁时，正式写入会拒绝，保护原始 `.orig` 备份。

### 验证方式

```bash
python3 claude-linux.py --dry-run
# 可在原始二进制副本上验证打补丁与重复识别：
cp <binary>.orig /tmp/claude.test
python3 claude-linux.py           --binary /tmp/claude.test
python3 claude-linux.py --dry-run --binary /tmp/claude.test
```

**Claude Code 2.1.331（npm 包 linux-x64 ELF 的副本）**：14 处站点全部命中，零警告、零跳过；等长改写及补丁后重复识别通过，改写所在的 3 个 JS chunk 经 `node --check` 语法检查通过（重试循环所在 chunk 需先把 Node 22 尚不支持的 `using` 声明替换为 `let`，原始 chunk 同样处理后结果一致），补丁后二进制可正常启动（`--version` / `--help`）；随后已写入本机安装的 2.1.331，重复 dry-run 识别为 14 处已打补丁。不以此声称已经验证真实 API 重试行为。

### 参数（claude）

| 参数 | 说明 |
|---|---|
| `--dry-run` | 显示改写前后，不修改二进制，也不创建备份 |
| `--restore` | 从 `.orig` 备份还原 |
| `--binary <路径>` | 手动指定 `claude` 二进制 |

升级后先跑 `--dry-run`：确认探测阶段没有 `WARN` / `SKIP`，且预期站点全部命中，不能只看最后的 `Patches skipped`。上游结构变化时需重新适配；本次未验证其他版本或 macOS 构建。

---

## 打坏了怎么恢复

打补丁前脚本会自动把原始二进制备份为 `<binary>.orig`，按从易到难三选一：

```bash
# 1. 一键还原（推荐，靠 .orig 备份）
python3 codex-linux.py --restore
python3 claude-linux.py --restore

# 2. 手动从 .orig 拷回（脚本本身跑不了时）
cp <binary>.orig <binary> && chmod 755 <binary>

# 3. 重装（保底，.orig 也没了时；配置不受影响）
npm install -g @openai/codex@0.172.0                # codex（用户 npm 目录，脚本已验证的版本）
npm install -g @anthropic-ai/claude-code            # claude（用户 npm 目录）
```

> 重装后是未打补丁的全新二进制；想再要补丁，重跑对应脚本即可。
