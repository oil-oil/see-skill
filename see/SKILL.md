---
name: see
description: View and understand images or videos with multimodal models. Use when the user asks to inspect, describe, compare, summarize, or extract text from image/video files or URLs. Supports native video understanding, parallel analysis, multi-image comparison, and automatic local image fallback.
---

# See

只运行 `scripts/see.sh`，不要自行调用模型 API。

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

图片原图直传。视频优先使用 Gemini 3.1 Flash-Lite，平台不可用时使用 Qwen3.7 Plus；自动保留清晰度、音频和完整时间线。`--task` 原样发送；没有特殊问题时不要添加。

首次使用先运行：

```bash
python3 scripts/onboard.py
```

让用户在隐藏输入框中填写 Key，不要要求用户把 Key 发到对话里。重复运行可添加或更换供应商；用 `python3 scripts/onboard.py --status` 查看状态。

供应商：`zenmux`、`bailian`、`openrouter`、`tokendance`、`local`。图片默认 Qwen3.7 Plus；视频在 ZenMux/OpenRouter 默认 Gemini 3.1 Flash-Lite，其余平台默认 Qwen3.7 Plus。覆盖视频模型用 `SEE_VIDEO_MODEL`。

也兼容厂商变量：`ZENMUX_API_KEY`、`DASHSCOPE_API_KEY`、`OPENROUTER_API_KEY`、`TOKENDANCE_API_KEY`。配置读取顺序为环境变量 → `.env.local` → 用户私有配置。

Windows 私有配置位于 `%APPDATA%\see\config.env`；macOS/Linux 位于 `~/.config/see/config.env`。配置文件权限仅限当前用户，不得复制进 Skill 或项目仓库。

本地降级：

- macOS：Vision 场景/人物/人脸/条码/图形结构 + OCR → Tesseract
- Windows：Windows OCR → Tesseract
- Linux：Tesseract

可选参数只在需要时使用：`--together`、`--provider`、`--model`、`--task`、`--jobs`、`--ocr-backend`。本地视觉结果不等同于多模态模型的完整语义理解。

视频需要任一云端 Key；同一个 Key 同时用于图片和视频。主 Agent 只传路径并读取 `output_path`，不要自行调用 ffmpeg、抽帧或上传。
