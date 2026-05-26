"""本地启动 Prefect analysis_flow runner。"""

from src.core.scheduler.flows.analysis_flow import DEPLOYMENT_SHORT_NAME, analysis_flow


if __name__ == "__main__":
    analysis_flow.serve(name=DEPLOYMENT_SHORT_NAME)
