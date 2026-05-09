"""注册 Prefect Deployment。

部署前运行一次，把 analysis_flow 注册到 Prefect Server。
Deployment 永久存在于 Prefect Server，不需要每次启动都重新注册。

用法:
    uv run python scripts/setup_prefect.py

前提:
    - Prefect Server 已启动（docker-compose up -d prefect）
    - PREFECT_API_URL 已配置（默认 http://localhost:4200/api）
"""

import asyncio
import os
import sys

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> None:
    from prefect.client.orchestration import get_client
    from prefect.deployments import Deployment
    from prefect.infrastructure import Process

    from src.core.scheduler.flows.analysis_flow import FLOW_NAME, analysis_flow

    print(f"正在连接 Prefect Server: {os.getenv('PREFECT_API_URL', 'http://localhost:4200/api')}")

    # 确认 Prefect Server 可达
    async with get_client() as client:
        try:
            await client.api_healthcheck()
            print("Prefect Server 连接成功")
        except Exception as e:
            print(f"无法连接 Prefect Server: {e}")
            print("请确认 Prefect Server 已启动: docker-compose -f docker-compose.dev.yml up -d prefect")
            sys.exit(1)

    # 创建 Work Pool（如果不存在）
    work_pool_name = "default-agent-pool"
    async with get_client() as client:
        try:
            await client.read_work_pool(work_pool_name)
            print(f"Work Pool '{work_pool_name}' 已存在")
        except Exception:
            await client.create_work_pool(
                work_pool={"name": work_pool_name, "type": "process"}
            )
            print(f"Work Pool '{work_pool_name}' 创建成功")

    # 注册 Deployment
    deployment = await Deployment.build_from_flow(
        flow=analysis_flow,
        name=FLOW_NAME,
        work_pool_name=work_pool_name,
        work_queue_name="default",
    )
    deployment_id = await deployment.apply()

    print(f"\nDeployment 注册成功:")
    print(f"  名称:    analysis_flow/{FLOW_NAME}")
    print(f"  ID:      {deployment_id}")
    print(f"  Pool:    {work_pool_name}")
    print(f"\n启动 Worker:")
    print(f"  uv run prefect worker start --pool {work_pool_name}")
    print(f"\nPrefect UI: http://localhost:4200")


if __name__ == "__main__":
    asyncio.run(main())
