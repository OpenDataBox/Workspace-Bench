"""
pipeline — LLM-Driven File Crawling Pipeline

模块结构:
  config.py        全局配置与常量
  utils.py         通用工具（日志、目录解析、空文件扫描）
  llm_agent.py     LLM 搜索（按文件名构造 Instruction、调用、解析）
  downloader.py    文件下载与技术验证（预检、下载、magic bytes、哈希去重）
  validator.py     LLM 后验证（提取文件文本、判断内容与标题的相关性）
  synth_agent.py   协同文件生成（LLM 规划 + Claude Agent SDK 执行）
  main.py          主流程入口
"""
