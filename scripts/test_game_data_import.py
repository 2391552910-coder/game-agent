"""测试游戏场景数据导入"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.engine.lightrag_engine import get_rag, shutdown_rag


async def test_import():
    print("Testing game data import...")
    
    rag = await get_rag()
    
    # 读取游戏场景数据
    game_data_path = PROJECT_ROOT / "game_docs" / "游戏场景数据.md"
    content = game_data_path.read_text(encoding="utf-8")
    print(f"File content length: {len(content)} chars")
    print(f"First 200 chars:\n{content[:200]}...")
    
    try:
        await rag.ainsert([content], file_paths=["游戏场景数据.md"])
        print("Import successful!")
    except Exception as e:
        print(f"Import failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await shutdown_rag()


if __name__ == "__main__":
    asyncio.run(test_import())