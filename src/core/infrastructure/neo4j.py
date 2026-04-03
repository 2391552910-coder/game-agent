from neo4j import AsyncGraphDatabase

from src.config import settings

_driver = None


async def init_neo4j():
    """初始化 Neo4j 驱动。应用启动时调用。"""
    global _driver

    uri = settings.neo4j_uri
    if uri.startswith("bolt://"):
        uri = uri.replace("bolt://", "neo4j://", 1)

    _driver = AsyncGraphDatabase.driver(
        uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
        max_connection_pool_size=50,
    )
    async with _driver.session(database=settings.neo4j_database) as session:
        await session.run("RETURN 1")
    return _driver


def get_neo4j_driver():
    """获取 Neo4j 驱动实例。"""
    if _driver is None:
        raise RuntimeError("Neo4j 未初始化，请先调用 init_neo4j()")
    return _driver


async def close_neo4j() -> None:
    """关闭驱动。应用关闭时调用。"""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None
