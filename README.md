# 院内转诊资料

本目录整理首诊、检查、跨科转诊和费用确认的合成记录。每个来源保留就诊标识、记录类别与生成时间，不包含真实患者信息。

使用 Python 3.11 或更新版本运行基线检查：

```sh
python3 -m unittest discover -s tests
```

演练数据位于 `fixtures/sources.json`。项目不需要联网或第三方依赖。
