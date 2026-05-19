

## 📚 LightRAG 查询模式详解

### **1. naive（朴素检索）**
```
原理：仅使用向量数据库进行语义相似度检索
特点：
  - 最简单的检索方式
  - 直接将查询向量化，在向量数据库中查找相似的文本块
  - 不使用知识图谱
适用场景：简单的文本匹配、快速查询
```

### **2. local（本地检索）**
```
原理：先通过向量检索找到相关实体，再在知识图谱中查询该实体的直接关系
特点：
  - 以实体为中心的检索
  - 只查询与目标实体直接相连的关系
  - 结果精确，但范围有限
适用场景：查询特定实体的属性或直接关联信息
```

### **3. global（全局检索）**
```
原理：通过向量检索找到相关实体后，在知识图谱中进行多跳查询
特点：
  - 可以发现实体之间的间接关系
  - 查询范围更广，可能发现隐藏的关联
  - 计算复杂度较高
适用场景：复杂关系查询、发现实体间的间接联系
```

### **4. hybrid（混合检索）**
```
原理：综合使用向量检索 + 知识图谱 + 关键词检索
特点：
  - LightRAG 的默认模式
  - 结合多种检索方式的优点
  - 先通过向量和关键词找到候选，再用知识图谱补充关系
  - 效果最好，但响应时间稍长
适用场景：需要综合信息的复杂查询
```

## 🔧 如何指定使用特定模式

在 `scripts/tests/test_lightrag.py` 中，查询是这样调用的：

```python
# hybrid 模式（默认）
answer = await rag.aquery(query, rerank=True)

# local 模式
answer = await rag.aquery(query, mode="local", rerank=True)

# global 模式
answer = await rag.aquery(query, mode="global", rerank=True)

# naive 模式
answer = await rag.aquery(query, mode="naive", rerank=True)
```

**关键参数**：`mode` 参数用于指定检索模式
- `mode="hybrid"`（默认）
- `mode="local"`
- `mode="global"`
- `mode="naive"`

## 📊 模式对比表

| 特性 | naive | local | global | hybrid |
|-----|-------|-------|--------|--------|
| 向量检索 | ✅ | ✅ | ✅ | ✅ |
| 知识图谱 | ❌ | ✅ | ✅ | ✅ |
| 多跳查询 | ❌ | ❌ | ✅ | ✅ |
| 关键词检索 | ❌ | ❌ | ❌ | ✅ |
| 响应速度 | 最快 | 较快 | 较慢 | 中等 |
| 查询精度 | 一般 | 较高 | 高 | 最高 |

## 💡 使用建议

- **简单查询**：使用 `naive` 或 `local`
- **关系查询**：使用 `global`
- **复杂综合查询**：使用 `hybrid`（默认）
- **需要 rerank**：添加 `rerank=True` 参数

如果您需要修改默认查询模式，可以在调用 `aquery()` 时显式指定 `mode` 参数。