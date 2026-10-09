"""MCP Server 集合。

每个文件都是一个可独立运行的 MCP Server，由宿主（agent 或 IDE）以子进程方式拉起，
通过 stdio 上的 JSON-RPC 通信。
"""
