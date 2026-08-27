# live-brain — 直播弹幕 AI 自动回复中枢

淘宝/抖音直播间弹幕 → **RAG 知识检索 + LLM 主备双模型** → **Bert-VITS2 语音合成** → OBS 直播间自动播报。单文件零框架（纯 Python 标准库），Windows 单机即跑。

## 完整链路

```
观众弹幕(淘宝+抖音双源)
   │ FlyAiLive/Electron写日志文件 / tb-live桥接(23461)
   ▼
弹幕日志增量tail(1.5s轮询, 字节偏移防回放)
   │ 过滤: 触发词+用户冷却15s+队列32
   ▼
┌─ RAG (LightRAG :9621, only_need_context 0.3s熔断)
├─ LLM (主备自动切换: DeepSeek-V4-Flash ⇄ gemini-flash-lite, 5次交替重试, 失败回队一次)
▼
VSA TTS (:23456 Bert-VITS2流式合成)
   │ 播放端后处理: 句间随机停顿 + 音量增益 + 峰值归一化(-1dBFS) + 频域EQ对齐视频原声(110Hz高通+2.8kHz峰值+4.5dB)
   ▼
winmm 声卡直播 → OBS 虚拟声卡 → 直播间
   ↕ 同步联动: 播报开始=OBS全部视频源Ducking静音(fade 100ms), 播完恢复(fade 200ms), 手动静音源不碰
```

## 特性

- **单文件** `live_brain.py` (~1600行): HTTP服务/WebUI/OBS websocket/播放器/配置 全内置，仅标准库+numpy
- **弹幕切场景**: 触发词(看/卡/试/展示)+尺寸数字+泡泡词识别，11场景映射，3s延迟切换+180s锁定自动回默认（已替代OBS lua插件）
- **LLM 高可用**: GMI账号级429高峰实测0丢弹幕——主备交替重试+失败回队兜底
- **音频一致**: 内置频域EQ把TTS音色向视频原声频谱对齐（语速80%/EQ参数为2026-08-27实测校准值）
- **WebUI**: 状态页(实时播报/场景/Ducking状态) + 设置页(全参数热改, config.json持久化)

## 快速开始

```powershell
# 1. 配套服务(同机): VSA(Bert-VITS2)23456 / LightRAG 9621 / LLM代理8002
# 2. 启动(注册计划任务守护):
powershell -File brain_ctl.ps1 -Action start
# 3. 打开 http://127.0.0.1:23460 设置 LLM base_url/model/api_key 与 TTS 参数
# 4. 弹幕来源: FlyAiLive 写入 D:\program\FlyAiLive\logs\用户发言记录_*.txt 自动tail
```

环境变量: `LLM_BASE`(LLM地址, 默认http://192.168.5.100:8002/v1) `POLL_INTERVAL`(轮询1.5s) `USER_COOLDOWN`(冷却15s) `OBS_WS_PORT`(4455)

详细部署/踩坑见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 依赖的开源项目

| 项目 | 用途 | 许可 |
|---|---|---|
| [fishaudio/Bert-VITS2](https://github.com/fishaudio/Bert-VITS2)(经 [VSA](https://github.com/jansecond/VSA) 封装) | TTS语音合成 | AGPL-3.0 / 底模CC-BY-NC-SA禁商用 |
| [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) | 知识图谱RAG | MIT |
| [obsproject/obs-studio](https://github.com/obsproject/obs-studio) | 推流/场景/音频 | GPL-2.0 |

> 本仓库代码 MIT。TTS 底模有非商用限制，商用请自行评估授权。
