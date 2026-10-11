"""给一个工作区灌一批示例订单与客户。

    python scripts/seed_cs_demo.py --workspace-id <id>
    python scripts/seed_cs_demo.py --user-email admin@example.com
    python scripts/seed_cs_demo.py --workspace-id <id> --dry-run

**为什么需要它**：接真 ERP 之前，本仓库没有任何一段生产代码会创建订单
（``CsOrder`` 只在测试与离线评估里被构造过）。于是刚装好的实例每一张工单走到
``lookup_order`` 都是"查无此单"，退款闸门、幂等台账、自动解决率全都没有输入。
评估集自带数据，所以 CI 一直是绿的——这条路径只有在真实部署里才露馅。

**为什么走 ``cs_import`` 而不是直接 ``db.add``**：这份示例要受和真实导入完全一样的
约束——同一套状态枚举、同一套金额校验、同样不写台账、同样幂等。绕过校验的示例
数据是一种特殊的坑：它证明不了任何东西，而真数据第一次导入时会撞上示例早就
躲过去的那条规则。

跑两遍不会越跑越多：订单按 ``order_no`` upsert。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from models import User  # noqa: E402
from services.ticket import cs_import  # noqa: E402

# 每一个都挑一个治理规则来踩，这样"跑通了"意味着闸门真的有过输入：
#   paid            —— 可全额退，退款主路径
#   packed          —— 还没发货，改地址允许
#   shipped         —— 已锁；查物流要有运单号，退款必须被状态挡下
#   delivered+部分退 —— 可退余额不是总额，超额那条才测得到
#   refunding       —— 在途但不许再动钱
DEMO = {
    "customers": [
        {
            "external_ref": "demo-buyer-1",
            "email": "buyer.demo@example.com",
            "phone": "+86 138-0000-0001",
            "display_name": "演示客户·张三",
            "tier": "vip",
            "lifetime_amount": "12800.00",
        },
        {
            "external_ref": "demo-buyer-2",
            "email": "second.demo@example.com",
            "phone": "+86 138-0000-0002",
            "display_name": "演示客户·李四",
            "tier": "standard",
            "risk_flags": ["曾提及投诉"],
        },
    ],
    "orders": [
        {
            "order_no": "DEMO-PAID-001",
            "status": "paid",
            "total_amount": "350.00",
            "currency": "CNY",
            "receiver_name": "张三",
            "receiver_phone": "+86 138-0000-0001",
            "address_text": "北京市海淀区中关村大街 1 号 3 层 305",
            "created_at": "2026-09-28T10:12:00",
            "customer": {"email": "buyer.demo@example.com"},
            "items": [
                {"sku": "EAR-Black", "title": "降噪蓝牙耳机", "quantity": 1, "unit_price": "299.00"},
                {"sku": "EAR-CASE", "title": "耳机收纳硬壳", "quantity": 1, "unit_price": "51.00"},
            ],
        },
        {
            "order_no": "DEMO-PACKED-002",
            "status": "packed",
            "total_amount": "1299.00",
            "receiver_name": "张三",
            "address_text": "北京市海淀区中关村大街 1 号",
            "created_at": "2026-10-01T09:00:00",
            "customer": {"email": "buyer.demo@example.com"},
            "items": [{"sku": "KETTLE-1.7", "title": "恒温电热水壶", "quantity": 1, "unit_price": "1299.00"}],
        },
        {
            "order_no": "DEMO-SHIPPED-003",
            "status": "shipped",
            "total_amount": "89.00",
            "receiver_name": "李四",
            "address_text": "上海市浦东新区世纪大道 100 号",
            "created_at": "2026-09-20T14:30:00",
            "customer": {"email": "second.demo@example.com"},
            "items": [{"sku": "MUG-Ceramic", "title": "陶瓷马克杯", "quantity": 1, "unit_price": "89.00"}],
            "shipments": [
                {
                    "carrier": "顺丰",
                    "tracking_no": "SF-Demo-0003",
                    "status": "in_transit",
                    "last_event": "已到达上海中转场",
                    "last_event_at": "2026-09-22T08:05:00",
                }
            ],
        },
        {
            "order_no": "DEMO-PARTIAL-004",
            "status": "delivered",
            "total_amount": "600.00",
            "refunded_amount": "150.00",
            "receiver_name": "李四",
            "created_at": "2026-09-10T11:00:00",
            "customer": {"email": "second.demo@example.com"},
            "items": [
                {"sku": "LAMPS-H01", "title": "落地灯", "quantity": 1, "unit_price": "450.00"},
                {"sku": "BULB-E27", "title": "备用灯泡", "quantity": 3, "unit_price": "50.00"},
            ],
            "shipments": [
                {
                    "carrier": "京东物流",
                    "tracking_no": "JD-Demo-0004",
                    "status": "delivered",
                    "last_event": "已签收，本人收取",
                    "last_event_at": "2026-09-13T16:40:00",
                }
            ],
        },
        {
            "order_no": "DEMO-REFUNDING-005",
            "status": "refunding",
            "total_amount": "1200.00",
            "receiver_name": "张三",
            "created_at": "2026-09-25T20:15:00",
            "customer": {"email": "buyer.demo@example.com"},
            "items": [{"sku": "WATCH-S2", "title": "智能手表", "quantity": 1, "unit_price": "1200.00"}],
        },
    ],
}


def _workspace_from_email(db, email: str) -> str | None:
    row = db.query(User).filter(User.email == email).first()
    return row.workspace_id if row else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灌一批示例订单与客户")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--workspace-id", help="直接指定工作区")
    group.add_argument("--user-email", help="从这个用户取他所在的工作区")
    parser.add_argument("--dry-run", action="store_true", help="只校验，不落库")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        workspace_id = args.workspace_id
        if args.user_email:
            workspace_id = _workspace_from_email(db, args.user_email.strip().lower())
            if not workspace_id:
                print(f"找不到用户 {args.user_email}，也就定不出工作区")
                return 2

        result = cs_import.import_cs_data(db, workspace_id, DEMO)
        if not result.applied:
            print("示例数据被自己的校验挡下了——这属于回归，不该发生：")
            for row in result.rejected:
                print(f"  {row.ref}: {row.reason}")
            return 1
        if args.dry_run:
            db.rollback()
            print(f"校验通过（未落库）：{len(DEMO['customers'])} 客户 / {len(DEMO['orders'])} 订单")
            return 0
        db.commit()
        print(
            f"工作区 {workspace_id}：客户 新建{result.customers_created}/更新{result.customers_updated}，"
            f"订单 新建{result.orders_created}/更新{result.orders_updated}，"
            f"商品行 {result.items_written}，包裹 {result.shipments_written}"
        )
        print("现在拿 DEMO-PAID-001 提一张退款工单，走的就是真数据了。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
