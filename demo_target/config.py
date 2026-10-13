# ⚠️ 演示用靶标（非真实凭据）—— 硬编码密钥已移除，统一从环境变量读取
import os

MYAPP_API_KEY = os.environ.get("MYAPP_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_client():
    return {"api_key": MYAPP_API_KEY, "db": DATABASE_URL}
