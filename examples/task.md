# 示例任务：为一个小项目补充导出功能

目标：给项目增加 CSV 导出，并保留原有命令行行为。

建议账本：

```text
context-guard init --name csv-export-demo
context-guard requirements add "新增 CSV 导出命令" --kind must
context-guard requirements add "不得修改已有命令的输出格式" --kind avoid
context-guard requirements add "补充一个导出测试并运行全部测试" --kind acceptance
```

完成每项后，先记录证据，再把 requirement 标记为 done：

```text
context-guard evidence add --for R001 --summary "导出命令已实现并检查目标文件" --result success --target "src/export.py"
context-guard requirements update R001 --status done
context-guard evidence add --for R003 --summary "全部测试通过" --result success --command "python -m unittest discover -s tests"
context-guard requirements update R003 --status done
```

过程中可以把经验单独放入旁记事本：

```text
context-guard note add "导出功能先锁定字段顺序，再补测试，返工最少" --kind experience --source ai
```

最后调用：

```text
context-guard recovery packet
context-guard gate check
```

