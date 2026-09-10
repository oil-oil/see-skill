---
name: see
description: "使用外部视觉服务或本地 OCR 分析本地图片、截图、视频及明确提供的媒体 URL，输出 Markdown。用户明确要求 see，或当前主模型缺少完成媒体分析所需的视觉能力时使用。不拦截普通文本、任意 URL 或已有原生视觉可以直接完成的任务；不用于生图、编辑图片或自动操作屏幕。使用外部服务前说明发送的媒体与服务范围，无法识别时如实说明限制。"
---

# See

先检查已有附件、可读媒体路径和当前宿主的视觉能力。原生视觉足够时直接完成；需要外部视觉时，说明将发送哪些媒体给哪个服务，并继承当前任务已经给出的授权。缺少可读输入时才索取路径或 URL。

将当前 Skill 目录解析为绝对路径后调用 `scripts/see.sh`。外部服务或 OCR 均不可用时如实说明，不隐瞒限制，不把 OCR 文字结果称为完整视觉理解。

安装 Skill 不会更换当前主模型；界面继续显示原来的文本模型是正常的。拖拽或粘贴图片若被拒绝，说明附件在 Skill 启动前已被拦截。不要反复尝试直接读取附件。

首次使用，或用户反馈模型说了「不支持视觉」却没有调用 see 时，先运行：

```bash
python3 scripts/onboard.py --status
```

仅用户明确要求修改全局规则时，才使用 `onboard.py --install-agents`；它是可选的宿主适配，不是媒体分析的前置步骤。缺少配置时由安全入口收集凭据，不让用户把密钥发到对话。

```bash
# 单图
scripts/see.sh image.png

# 视频
scripts/see.sh video.mp4

# 多图并行
scripts/see.sh a.png b.png c.png

# 多图比较或联合判断
scripts/see.sh --together before.png after.png --task "比较界面变化"

# 可选关注点
scripts/see.sh screenshot.png --task "重点识别界面文字"
```

成功后读取 stdout 中 `output_path=<path>` 指向的 Markdown。

脚本自动完成：识别图片或视频 → 选择供应商 → 失败时切换供应商。图片无云端时降级到本地视觉；视频自动压缩后原生输入模型，不自行抽帧。多文件默认并行。

图片原图直传。视频按所选服务的模型配置处理；自动保留清晰度、音频和完整时间线。`--task` 原样发送；没有特殊问题时不要添加。

新配置按上述 API Key 配置说明打开本机页面，由用户亲自保存；选择的配置名、业务 `--provider` 必须对应。通过 run 包装云端分析命令，不能绕过包装器读取页面凭据。原 onboard 只用于兼容配置、状态或用户明确选择的终端方式。

供应商：`zenmux`、`bailian`、`openrouter`、`tokendance`、`local`。图片默认 Qwen3.7 Plus；视频在 ZenMux/OpenRouter 默认 Gemini 3.1 Flash-Lite，其余平台默认 Qwen3.7 Plus。覆盖视频模型用 `SEE_VIDEO_MODEL`。

也兼容厂商变量：`ZENMUX_API_KEY`、`DASHSCOPE_API_KEY`、`OPENROUTER_API_KEY`、`TOKENDANCE_API_KEY`。配置读取顺序为环境变量 → `.env.local` → 用户私有配置。

Windows 私有配置位于 `%APPDATA%\see\config.env`；macOS/Linux 位于 `~/.config/see/config.env`。配置文件权限仅限当前用户，不得复制进 Skill 或项目仓库。

本地降级：

- macOS：系统 Vision OCR；有 Swift 时增加场景/人物/人脸/条码/图形结构 → Tesseract
- Windows：Windows OCR → Tesseract
- Linux：Tesseract

本地后端报错时先运行 `python3 scripts/onboard.py --status`。macOS 10.15+ 不需要 Xcode；Windows 需要安装系统 OCR 语言；Linux 需要安装 Tesseract。

可选参数只在需要时使用：`--together`、`--provider`、`--model`、`--task`、`--jobs`、`--ocr-backend`。本地视觉结果不等同于多模态模型的完整语义理解。

视频需要任一云端 Key；同一个 Key 同时用于图片和视频。主 Agent 只传路径并读取 `output_path`，不要自行调用 ffmpeg、抽帧或上传。

## API Key 配置入口

需要外部服务凭据时先读[API Key 配置与业务读取](references/api-key-setup.md)：复用已有安全入口；本机缺少 Key 时使用随附固定页面，保存后通过业务包装入口读取。内置能力与纯本地流程不要求配置 Key。

## 凭据与数据边界

云端分析会把选定媒体发给所选供应商，可能计费；本地 OCR 不等于完整视觉理解。新密钥通过固定本机页面存入系统凭据库，普通配置只保存引用。先用任务 Python 环境安装 `see/scripts/requirements-credentials.txt`（Skill 内相对路径为 `scripts/requirements-credentials.txt`）。不把 Key 发到聊天、命令参数或日志。旧明文配置不会自动迁移或覆盖：入口会先停止，提示备份后重新配置。状态检查只确认来源，API 验证成功才代表可用。全局指令写入仅在明确调用 `--install-agents` 时发生。
