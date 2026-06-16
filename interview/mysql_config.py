"""MySQL 连接配置 — 统一入口，避免多文件重复硬编码。"""

import pymysql

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "123456",
    "database": "ai_jobs_db",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


def get_conn():
    return pymysql.connect(**DB_CONFIG)
