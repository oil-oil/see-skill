---
name: see
description: View and understand images with multimodal models. Use when the user asks to inspect, describe, compare, or extract text from one or more image files or URLs. Supports parallel analysis, multi-image comparison, and automatic local vision/OCR fallback.
---

# See

只运行 `scripts/ask_media.sh`，不要自行调用模型 API。

```bash
# 单图
scripts/ask_media.sh image.png

# 多图并行
scripts/ask_media.sh a.png b.png c.png

# 多图比较或联合判断
scripts/ask_media.sh --together before.png after.png --task "比较界面变化"

# 可选关注点
scripts/ask_media.sh screenshot.png --task "重点识别界面文字"
```

成功后读取 stdout 中 `output_path=<path>` 指向的 Markdown。

脚本自动完成：选择已配置的多模态供应商 → 失败时切换供应商 → 最后降级到本地视觉分析。多图默认并行，结果按输入顺序汇总。

云端会把原图直接交给多模态模型，不预先 OCR、缩放或压缩。`--task` 会作为用户问题原样发送；没有特殊问题时不要添加。

首次使用先运行：

```bash
python3 scripts/onboard.py
```

让用户在隐藏输入框中填写 Key，不要要求用户把 Key 发到对话里。重复运行可添加或更换供应商；用 `python3 scripts/onboard.py --status` 查看状态。

供应商：`zenmux`、`bailian`、`openrouter`、`tokendance`、`local`。默认模型均为各平台的 Qwen3.7 Plus 对应 ID。需要覆盖时设置 `SEE_MODEL`；供应商地址用 `SEE_BASE_URL`。

也兼容厂商变量：`ZENMUX_API_KEY`、`DASHSCOPE_API_KEY`、`OPENROUTER_API_KEY`、`TOKENDANCE_API_KEY`。配置读取顺序为环境变量 → `.env.local` → 用户私有配置。

Windows 私有配置位于 `%APPDATA%\see\config.env`；macOS/Linux 位于 `~/.config/see/config.env`。配置文件权限仅限当前用户，不得复制进 Skill 或项目仓库。

本地降级：

- macOS：Vision 场景/人物/人脸/条码/图形结构 + OCR → Tesseract
- Windows：Windows OCR → Tesseract
- Linux：Tesseract

可选参数只在需要时使用：`--together`、`--provider`、`--model`、`--task`、`--jobs`、`--ocr-backend`。本地视觉结果不等同于多模态模型的完整语义理解。
