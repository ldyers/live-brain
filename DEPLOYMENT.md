# live-brain 直播弹幕自动回复中枢 @ 101 部署与使用文档

> 弹幕自动回复中枢：监控弹幕日志 → RAG 取知识 + LLM 生成口播 → VSA TTS 合成 → 本机声卡自动播放（OBS 虚拟声卡采集进直播）
> **播报期间经 OBS websocket 自动静音全部 VLC 视频源（Ducking），播完立即恢复**，避免视频原声与语音播报打架。

## 整体链路

```
[弹幕抓取]✅ FlyAiLive → logs\用户发言记录_*.txt
      ↓ (live-brain 增量tail, 1.5s轮询)
[过滤/队列] ✅ 短文本/重复/同用户15s冷却
      ↓
[RAG+LLM] ✅ LightRAG(:9621)取知识 + LLM(192.168.5.100:8002 deepseek-v4-flash)生成≤40字口播
      ↓
[VSA TTS] ✅ :23456/voice/bert-vits2 合成WAV(音色zxy)
      ↓                    ↕ 播放期间: OBS ws(4455) 静音全部vlc_source
[自动播放] ✅ winmm直写本机默认声卡 → OBS桌面音频 → 直播间
      ↓ 播完立即恢复视频源声音
```

## OBS 音频联动（Ducking）说明

- **触发时机**：每次 TTS 播报开始前 `obs duck ON`，播放结束（含异常）后 `obs duck OFF`，日志在 brain.log 可查
- **作用对象**：OBS 全部 `vlc_source` 输入（当前 10 个：6米/8米/10米…袜子/VLC视频源等），不限当前场景——lua 插件播报中途切场景也不怕
- **智能恢复**：只恢复「本次由系统静音」的源；用户手动静音过的源保持静音不被误解开
- **互斥保护**：连续播报靠 PLAY_LOCK 串行化，ON→OFF 严格交替不会乱
- **容错**：OBS 未启动/websocket 关闭时仅记日志跳过联动，不影响语音播报本身
- 协议实现为纯标准库最小 websocket 客户端（obs-websocket 5.x，本机无鉴权）；如 OBS 开启了密码，设置环境变量 `OBS_PASSWORD` 即可

场景切换仍由 lua 插件独立完成，与本服务互不干扰。

## 部署信息

| 项 | 值 |
|---|---|
| 位置 | 101 机器 `C:\live-brain\` |
| 服务端口 | **23460**（状态页/控制 API） |
| Python | `C:\Python314\python.exe`（系统自带，纯标准库零依赖） |
| 自启 | 计划任务 `LiveBrain_Guard`(ONSTART/SYSTEM) → guard_win.bat 守护循环 |
| 守护 | 每 60s 检查 23460，掉线先清残留进程再拉起，宽限 60s，日志 `logs\guard.log` |
| 面板卡片 | 9000 面板「🤖 直播大脑(弹幕自动回复)」卡，可远程启停 |

## 配置（环境变量或脚本内默认值）

| 变量 | 默认 | 说明 |
|---|---|---|
| BRAIN_PORT | 23460 | 服务端口 |
| VSA_TTS_URL | http://127.0.0.1:23456/voice/bert-vits2 | TTS 接口 |
| BRAIN_VOICE_ID | 1 | 音色 id：0=sdd 1=zxy |
| RAG_URL | http://127.0.0.1:9621/query | RAG 查询 |
| LLM_BASE | http://192.168.5.100:8002/v1 | LLM 上游 |
| LLM_MODEL | deepseek-v4-flash | 模型名 |
| USER_COOLDOWN | 15 | 同一用户回复冷却秒数 |
| POLL_INTERVAL | 1.5 | 日志轮询间隔秒 |

## 使用

### 状态页（推荐）
浏览器开 `http://192.168.5.101:23460`：
- 实时指标：监控文件/队列/已回复/失败/去重/冷却计数
- 最近处理记录（弹幕→回复→耗时，📚 标记表示带了 RAG 知识）
- 按钮：暂停/恢复回复、模拟弹幕、直接播报任意文字

### API

```bash
# 健康
curl http://192.168.5.101:23460/health            # -> ok
# 状态(JSON)
curl http://192.168.5.101:23460/api/status
# 模拟一条弹幕走全链路(测试用, 不写文件)
curl "http://192.168.5.101:23460/test?text=黄水晶有什么功效"
# 直接播报文字(跳过LLM)
curl "http://192.168.5.101:23460/say?text=大家好"
# 暂停/恢复(只停新回复, 不中断播放)
curl http://192.168.5.101:23460/pause
curl http://192.168.5.101:23460/resume
```

### 启停命令（101 本机）
```powershell
powershell -File C:\live-brain\brain_ctl.ps1 -Action restart   # 或 start / stop
```

## 实测记录（2026-08-26）

| 测试 | 结果 |
|---|---|
| 全链路：模拟弹幕「黄水晶有什么功效」 | ✅ 7.9s 回复「黄水晶主财运和能量场，戴着挺提气的」并播出 |
| 商品价格类「16的手串多少钱」「10mm款多少钱」 | ✅ 正确口播报价引导 |
| 夸赞互动「主播今天状态真不错」 | ✅ 感谢+引导关注 |
| **真实观众弹幕**（「加油」「10mm款多少钱」等） | ✅ 自动回复播出 |
| 重复文本拦截 | ✅ skip_dup 计数生效 |
| 同用户 15s 冷却 | ✅ skip_cooldown 计数生效 |
| 短文本/口头禅过滤 | ✅ skip_short 生效 |
| 脏字符免疫（\x00 等控制符） | ✅ 清洗后正常处理 |
| 上游 429/瞬断 | ✅ LLM 3次重试 + TTS 15s 重试兜底 |
| 典型耗时 | RAG 0.3s + LLM 2~3s + TTS 4~7s ≈ **7~10 秒/条** |
| OBS Ducking 联动 | ✅ 播报开始 10 个 VLC 源全静音(muted=10/10)，播完全部恢复(10/10) |
| Ducking 时序 | ✅ 连续播报 ON→OFF→ON→OFF 严格交替，无泄漏 |
| 手动静音保护 | ✅ 只恢复系统本次静音的源，用户手动静音不被误解开 |

## 播报设置 WebUI（:23460/config）

独立配置页 `http://192.168.5.101:23460/config`（状态页有入口按钮），改动**即时生效并持久化到 config.json**（重启不丢）：

| 配置 | 说明 | 范围 |
|---|---|---|
| 静音后延迟播放 | 视频源静音完成后等多久再开播报（给混音缓冲） | 0~3000ms |
| 视频声音淡出时长 | 0=立刻硬切静音；>0 渐弱到静音 | 0~3000ms |
| 播完后淡入恢复时长 | 0=立刻满音量；>0 从0渐强回原音量 | 0~3000ms |
| TTS 音色 | sdd / zxy | - |
| 同一观众回复冷却 | 防刷屏 | 0~120s |
| RAG 知识库增强 | 开关 | - |

预设按钮：⚡极速硬切(0/0/0) · 🌊平滑过渡(500/500/1000) · 🎬综艺感(1000/1500/2000)，另有「用当前配置试听」按钮。

API：`GET /api/config` 读配置，`POST /api/config`(JSON) 写配置。

### Ducking 行为实测（2026-08-26, 平滑过渡配置）

对 VLC 源「6米」高频采样音量曲线：

```
57.9s vol=0.411        ← TTS合成中(先合成后静音,不占静音窗口)
58.5s vol=0.329 ↓      ← 淡出开始
58.7s vol=0.206 ↓
58.8s vol=0.082 ↓
59.0s vol=0.000 MUTED  ← 播报开始
68.6s vol=0.070 ↑      ← 播完淡入开始
69.5s vol=0.411        ← 精确恢复原始音量
RESULT: PASS (final_vol==orig, muted=False)
```

改进说明：播放顺序为 **TTS合成(4~7s) → 静音视频源 → 延迟 → 播放 → 恢复**——静音窗口只覆盖真实播放段，不再让观众看"哑巴视频"。淡入淡出按 50ms 步进线性渐变，恢复时精确回到每个源各自的原始音量（含用户手动调过的）。

## 大模型配置与主备切换（:23460/config）

### 配置项（设置页「🧠 大模型」卡片）

| 项 | 说明 |
|---|---|
| 主模型 Base URL / 名称 / API Key | OpenAI 兼容 `/v1/chat/completions`。Key 留空提交=不修改已存 key；本地服务填 `EMPTY` |
| 备用模型 Base URL / 名称 / API Key | **留空 Base URL = 不启用备用**。免费/不稳定 API 强烈建议配上 |
| 系统提示词 | AI 人设（是谁/卖什么/语气）。输出长度、禁表情等硬规则由系统自动附加 |
| 回复带观众昵称 | 开：回复自然带上名字如「小美你好呀～」；关：泛称 |
| 回复先复述观众发言 | 开：「你问黄水晶呀——」确认式互动；默认关 |
| RAG 知识库增强 | 开关，关闭后跳过 :9621 查询直接 LLM |

「🧪 测试当前主模型连通」按钮：按表单当前填写的地址即时发一条测试消息，返回模型回复原文。

### 故障自动切换机制

```
主模型连续失败 ≥2 次 ──→ 自动切到备用模型（粘性, 后续请求都走备用）
备用模型连续失败 ≥5 次 ──→ 自动切回主模型探测（主恢复则粘回主）
任一模型成功一次     ──→ 失败计数清零
```

- 切换动作写日志 `LLM FAILOVER -> backup/main`，状态页实时显示「当前模型」和「模型切换次数」（模型名带 `(备)` 后缀表示正在用备用）
- 单模型内还有 3 次重试（429/瞬时抖动不触发切换），重试间隔 4s/8s

### 实测记录（2026-08-26）

| 测试 | 结果 |
|---|---|
| 主模型指向坏端口(9999) + 备用指向正常服务 | ✅ 连挂2次 → 日志 `LLM FAILOVER -> backup` → 回复成功播出 |
| 切换后状态 | ✅ 状态页显示 `deepseek-v4-flash(备)`，failovers=1 |
| 恢复正确配置 + 回归播报 | ✅ 正常 |
| 🧪 连通性测试 API (`POST /api/llmtest`) | ✅ 返回 `{ok:true, reply:"正常！"}` |
| api_key 脱敏 | ✅ GET /api/config 不回传 key，前端留空提交不覆盖 |

## 弹幕切场景引擎（Python版，已替代OBS lua插件）

原 `Desktop\obs-plugin\danmaku_scene_switcher.lua` 的全部功能已用 Python 重写进 live-brain，lua 已停用（文件改名 `.disabled-by-live-brain` 备份于原目录，2026-08-26 OBS重启后卸载）。

### 触发规则（与lua v5.4 逐条对齐）
- 触发词：看 / 卡 / 试 / 展示（无触发词的聊天弹幕完全静默）
- 尺寸：阿拉伯数字优先，兼容中文数字（「十六米的」=16；「一下」的"一"不干扰）；就近归档（5米→6档，20米→16档）
- 含泡泡/泡子/珠珠 → 泡泡珠场景(10-16档)；不含 → 白水场景(6-16档)
- 11 个场景 cmd→OBS实际名映射按本机 OBS 实测对齐（6/8/10/12/14/16/N泡）

### 切换行为
- 检测到指令后延迟 N 秒切换（默认3s，窗口内新指令覆盖旧定时）
- 切换后锁定 N 秒（默认180s），期间新指令忽略；到期自动回「默认」场景
- 状态页实时显示：当前场景、切景检测描述；设置页可开关/调参/手动点按钮切 11 个场景

### 实测记录（2026-08-26, 无lua干扰环境）
| 测试 | 结果 |
|---|---|
| 意图识别单测（16组用例含中文数字/泡泡词/静默） | ✅ 16/16 |
| 弹幕「十六米的给我看看」自动切场景16 | ✅ `✓ 16 -> 场景[16]` |
| 弹幕「看看12泡的效果」自动切场景12泡 | ✅ |
| 锁定期内新指令被忽略 | ✅ `⏳ 锁定中剩余26s，忽略 12` |
| 锁定期满自动回默认 | ✅ |
| `/scene?cmd=N` 手动切换 API | ✅ |

## 已知事项 / 踩坑记录

1. **VSA `streaming=True` 返回 MP3 流**（忽略 format=wav），本服务不带该参数拿标准 WAV。
2. **真实弹幕文本可能混入不可见控制字符**，会导致 RAG 422——已在解析层统一清洗。
3. **LightRAG 知识库当前为空**（`C:\lightrag\data`、`inputs` 无文档），RAG 全部返回无资料，回复靠 LLM 常识。要启用知识增强：往 WebUI(:9621) 上传产品资料即可，无需改代码。
4. LightRAG 加载模型需 ~2.5 分钟，guard_win.bat 已设 4 分钟宽限；live-brain 对 VSA 冷启动有 15s 重试兜底。
5. 弹幕文件按 mtime 选最新一个监控；首次见到文件会跳过已有内容（防重启重播历史），之后增量读取。
6. 播放走 Windows 默认输出设备——确认 OBS 的「桌面音频」采集的是该设备即可进直播间。

## 文件清单

```
C:\live-brain\
├── live_brain.py      主程序(约550行, 纯标准库)
├── brain_ctl.ps1      start/stop/restart 控制脚本
├── guard_win.bat      守护循环(掉线清残留后重启)
├── start_win.bat      schtasks 方式启动(SSH 会话结束后存活)
└── logs\
    ├── brain.log      业务日志(每条回复/错误)
    ├── stdout.log     进程标准输出
    └── guard.log      守护动作日志
```

## 2026-08-26 启动链路修复（guard 拉起失效）

### 故障现象
07:39 主进程被控制台 ^C 杀死；guard 在 18:22/18:26 两次检测到端口挂并"restarting"，但 23460 始终没起来。

### 根因（3 个叠加）
1. `guard_win.bat` 用 `start /min` 拉起：SYSTEM 会话里无交互桌面，winmm 音频播放不可用，进程起不来且输出未重定向、失败被完全吞掉。
2. `brain_ctl.ps1` 首行被插入编码语句，`param()` 不在第一行导致脚本报错。
3. schtasks 默认 ExecutionTimeLimit=72h：任务每 3 天强杀一次进程；且 ONCE 任务午夜会自动重触发 → 双实例抢端口。旧 brain_start.bat 的 /TR 还拼了坏路径 `C:\live-brain\C:\Python314\python.exe`。

### 修复
| 文件 | 改动 |
|---|---|
| guard_win.bat | 不再自己 start，改为调用 `brain_ctl.ps1 -Action start`（计划任务→Administrator Interactive 会话），清理动作日志落盘 |
| brain_ctl.ps1 | param 归位首行；Start-Brain 重写：Register-ScheduledTask LiveBrain_run（Interactive+Highest、IgnoreNew 防双实例、ExecutionTimeLimit PT0S 去 72h 限制、触发器设为过去时间永不自动跑）；新增 status 动作 |
| brain_start.bat | 改为一行委托 brain_ctl.ps1 |

### 实测记录（2026-08-26 18:43）
| 测试 | 结果 |
|---|---|
| brain_ctl.ps1 restart | ✅ killed 旧PID → healthy after 0s |
| 自愈演练：stop 杀掉进程(16396) | ✅ guard 18:43:28 检测→拉起新 PID 5432，60s 内恢复 |
| 新实例启动日志 | ✅ obs-ws 连上 127.0.0.1:4455, probe ok v31.1.2, monitor watching FlyAiLive logs |
| LiveBrain_run 配置 | ✅ State=Running, IgnoreNew, ExecutionTimeLimit=PT0S, Administrator/Interactive/Highest |
| WebUI | ✅ http://192.168.5.101:23460/health = ok, 状态页标题正常 |

### 运维速查
```powershell
powershell -File C:\live-brain\brain_ctl.ps1 -Action restart   # 手动重启
powershell -File C:\live-brain\brain_ctl.ps1 -Action status    # 查状态
schtasks /Run /TN LiveBrain_Guard                                # 手动跑一次守护循环
```
注意：不要在 SSH 控制台前台直跑 live_brain.py 后直接关窗口——^C 会杀进程（本次事故根因），一律走 brain_ctl.ps1 或计划任务。
