"""EAS Mail Exporter 的核心实现（Exchange ActiveSync 客户端 + 导出引擎）。

为什么用 ActiveSync 而不是 EWS/IMAP：不少邮件系统只开放 ActiveSync，
EWS 被按路径封禁（连接在发出请求后被重置）、IMAP 端口直接关闭。
ActiveSync 是手机邮件客户端走的通道，通常可用，而且能拿到完整 MIME 原文。

模块划分：

    wbtokens.py   从微软官方 [MS-ASWBXML] 规范生成的字段表
    wbxml.py      WBXML 二进制编解码
    mime.py       邮件头解码与正文还原
    easclient.py  HTTP + 协议命令
    exporter.py   导出引擎（断点续传、索引、报告）
"""
