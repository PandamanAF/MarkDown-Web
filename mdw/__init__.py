"""
MDW — 基于类 Markdown 文档的建站框架，支持热重载与可扩展管线。

公开 API：
    MDWSite       — 主站点构建器，调用 .run() 即可启动
    Extension     — 自定义解析器 / 渲染器 / 中间件的基类
    Route         — 路由对象
"""
from .site import MDWSite
from .extensions import Extension
from .router import Route

__all__ = ["MDWSite", "Extension", "Route"]
