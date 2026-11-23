"""确保目标库存在（容器/新环境首启必需，幂等）。

SeekDB 官方镜像的入口语义：**不带参数=常驻服务；带参数=执行该 SQL 后退出**，
因此无法用一次性容器去"给另一个实例建库"，库必须由平台侧建。
"""
import asyncio
import os
import sys

import pymysql


async def main() -> int:
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", "2881"))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "")
    name = os.getenv("DB_NAME", "aiops")
    last = None
    # D235：等待窗口 30 次（60s）→ 150 次（5 分钟）。真机实测：SeekDB 首次初始化
    # （复用已有数据卷时也要校验口令与恢复）常要 60~120s，60s 窗口必然超时 →
    # 日志里刷一串「[db_init] 失败」，用户以为部署坏了（实测平台最终会自愈，
    # 但用户看到的是失败 + curl Connection reset）。放宽窗口，并把措辞改成
    # 「未在窗口内就绪」——真失败与「还在等」要能一眼分开。
    total = 150
    for attempt in range(1, total + 1):
        try:
            cn = pymysql.connect(host=host, port=port, user=user, password=password, connect_timeout=5)
            with cn.cursor() as c:
                c.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` DEFAULT CHARACTER SET utf8mb4")
            cn.close()
            print(f"[db_init] 库 {name} 就绪（第 {attempt} 次尝试）", flush=True)
            return 0
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt % 10 == 0 or attempt == 1:
                print(f"[db_init] 等待数据库…（{attempt}/{total}，约 {attempt * 2}s）"
                      f"{type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(2)
    print(f"[db_init] 数据库在 {total * 2}s 内仍未就绪（最后一次：{type(last).__name__}: {last}）。\n"
          f"           排查：docker logs aiops-seekdb --tail 30（数据库可能仍在首次初始化或反复重启）；\n"
          f"           容器会由 restart 策略自动重试，数据库就绪后平台即正常启动。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
