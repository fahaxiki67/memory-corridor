# Changelog

本项目所有显著变更都记录在本文件中。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.1.0] - 2026-09-04

### 新增

- CLI 增加 `--version`，可打印当前版本号。
- 补齐开源发布要素：MIT `LICENSE`、本 `CHANGELOG.md`、GitHub Actions CI（Ubuntu / macOS / Windows × Python 3.11–3.13 矩阵）。
- `pyproject.toml` 补全发布元数据：readme、license、authors、keywords、classifiers、项目 URLs。
- 新增边界测试：重复 `init` 报错、空账本门禁阻塞、failed evidence 阻塞、空笔记报错、恢复包自定义输出路径、`--version`。

### 修复

- `recovery packet` 不再重复构建恢复包两次（`write_packet` 支持直接接收已生成的文本）。
- `atomic_write` 由模块私有改为公开：`recovery.py` 跨模块使用它写入自定义路径。

### 变更

- 版本号在 `pyproject.toml` 与 `context_guard_lite.__version__` 两处保持一致（2.1.0）。

## [2.0.0] - 2026-08-31

### 新增

- 首个公开版本：本地任务账本（requirements / evidence / notes）、旁记事本 `notebook.md`、追加式事件日志 `events.jsonl`、恢复包 `recovery packet`、完成门禁 `gate check`。
- requirement 带 revision 与历史：修改文本或类型后，旧 evidence 不再自动适用于新版本。
- 纯标准库实现，零第三方依赖，跨 macOS / Windows 同一套项目目录结构。
