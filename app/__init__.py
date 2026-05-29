"""SuperBizAgent Python 版本

基于 LangChain 的智能业务代理系统
"""

__version__ = "1.0.0"

# 关键：先把 .env 的变量注入到 os.environ，使 langchain_qwq.ChatQwen 等
# 直接读取 os.environ 的库（如 DASHSCOPE_API_BASE / DASHSCOPE_API_KEY）能拿到正确值。
# 必须在任何业务模块 import 之前执行。
from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=False)

from app.utils import logger  # noqa: F401, E402
