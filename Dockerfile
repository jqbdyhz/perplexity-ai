# Perplexity MCP Server Docker Image
# 使用多阶段构建优化镜像大小

# ============ 阶段1: 构建阶段 ============
FROM python:3.12-slim AS builder

WORKDIR /build

# 安装构建依赖 (curl_cffi 需要编译)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker 缓存
COPY pyproject.toml README.md ./
COPY perplexity/ ./perplexity/
COPY perplexity_async/ ./perplexity_async/

# 安装到独立目录，方便后续复制
RUN pip install --no-cache-dir --prefix=/install .

# ============ 阶段2: 运行时镜像 ============
FROM python:3.12-slim

WORKDIR /app

# 只安装运行时必需的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制已安装的 Python 包
COPY --from=builder /install/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# 复制应用代码
COPY perplexity/ ./perplexity/

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "-m", "perplexity.mcp_server", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
