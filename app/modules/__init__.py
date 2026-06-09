"""扩展模块容器 - 资产监控/主动收集/Nuclei 扫描等独立功能放这里。

每个子模块约定: 提供 get_module() -> Module, 然后在 app/web.py 的
ENABLED_MODULES 列表里追加即可。

参考 app/modules/_example.py 作为模板。
"""
