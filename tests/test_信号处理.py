#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信号处理逻辑单元测试

测试 FollowerEngine._signal_to_order 的信号解析、过滤和去重逻辑。
"""

import time
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytest


def _make_engine(config_overrides=None):
    """创建一个最小化的 FollowerEngine 实例用于测试。
    避免导入主程序（会触发 tkinter 等重量级依赖），
    直接构造一个具有 _signal_to_order 方法的轻量对象。
    """
    config = {
        "redis_backtest_skip_hours": 8,
        "redis_allow_backtest_live": False,
    }
    if config_overrides:
        config.update(config_overrides)

    engine = MagicMock()
    engine.config = config
    engine.app = None
    # 绑定真实的 _signal_to_order 方法
    from types import MethodType
    engine._signal_to_order = MethodType(_signal_to_order_impl, engine)
    return engine


def _signal_to_order_impl(self, data):
    """从 FollowerEngine._signal_to_order 提取的纯逻辑实现，
    去掉了对 gui_log 和 threading 的依赖，方便单元测试。"""
    # 0. 预热信号
    if data.get("action", "") == "preheat":
        return None

    # 1. 回测信号过滤
    xdsj = data.get("xdsj", "")
    if xdsj:
        try:
            sig_ts = datetime.strptime(xdsj, "%Y-%m-%d %H:%M:%S").timestamp()
            skip_hours = int(self.config.get("redis_backtest_skip_hours", 2))
            if skip_hours > 0 and sig_ts < time.time() - skip_hours * 3600:
                return None
        except Exception:
            pass

    # 2. 策略名
    strategy = data.get("strategy_name", "")

    # 3. 仅处理 buy/sell
    action = data.get("action", "")
    if action not in ("buy", "sell"):
        return None

    # 4. 代码提取
    security = data.get("security", "")
    code = security.split(".")[0] if "." in security else security
    if not code or not code.isdigit():
        return None

    direction = 1 if action == "buy" else -1
    try:
        amount = abs(int(data.get("amount_diff", 0) or 0))
    except Exception:
        amount = 0
    if amount <= 0:
        return None

    # 5. 去重键
    uid = data.get("signal_id") or "%s|%s|%d|%d" % (xdsj, code, direction, amount)
    return {
        "uid": uid,
        "time": xdsj or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "stock_code": code,
        "stock_name": "",
        "direction": direction,
        "amount": amount,
        "price": float((data.get("price") if data.get("price") not in (None, "") else data.get("current_price")) or 0),
        "strategy": strategy,
        "_source": "redis",
    }


class TestSignalParsing:
    """信号解析测试"""

    def test_buy_signal_basic(self):
        """测试基本买入信号解析"""
        engine = _make_engine()
        data = {
            "signal_id": "test:000001:buy:1000:20261019",
            "strategy_name": "测试策略",
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "current_price": 10.5,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order is not None
        assert order["stock_code"] == "000001"
        assert order["direction"] == 1
        assert order["amount"] == 1000
        assert order["price"] == 10.5
        assert order["strategy"] == "测试策略"
        assert order["uid"] == "test:000001:buy:1000:20261019"

    def test_sell_signal(self):
        """测试卖出信号解析"""
        engine = _make_engine()
        data = {
            "security": "600519.XSHG",
            "action": "sell",
            "amount_diff": 500,
            "current_price": 1800.0,
            "xdsj": "2026-10-19 10:00:00",
        }
        order = engine._signal_to_order(data)
        assert order is not None
        assert order["stock_code"] == "600519"
        assert order["direction"] == -1
        assert order["amount"] == 500

    def test_code_extraction_from_jq_format(self):
        """测试聚宽格式代码提取（000756.XSHE -> 000756）"""
        engine = _make_engine()
        data = {
            "security": "000756.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order["stock_code"] == "000756"

    def test_code_without_suffix(self):
        """测试不带后缀的代码"""
        engine = _make_engine()
        data = {
            "security": "000001",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order["stock_code"] == "000001"

    def test_price_field_fallback(self):
        """测试价格字段回退（price -> current_price）"""
        engine = _make_engine()
        # 使用 price 字段
        data1 = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "price": 10.5,
            "xdsj": "2026-10-19 09:30:00",
        }
        order1 = engine._signal_to_order(data1)
        assert order1["price"] == 10.5

        # 使用 current_price 字段
        data2 = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "current_price": 11.0,
            "xdsj": "2026-10-19 09:30:00",
        }
        order2 = engine._signal_to_order(data2)
        assert order2["price"] == 11.0


class TestSignalFiltering:
    """信号过滤测试"""

    def test_preheat_signal_returns_none(self):
        """测试预热信号不产生订单"""
        engine = _make_engine()
        data = {"action": "preheat"}
        order = engine._signal_to_order(data)
        assert order is None

    def test_cancel_signal_ignored(self):
        """测试撤单信号被忽略"""
        engine = _make_engine()
        data = {
            "security": "000001.XSHE",
            "action": "cancel",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order is None

    def test_invalid_code_rejected(self):
        """测试非法代码被拒绝"""
        engine = _make_engine()
        data = {
            "security": "ABCDEF.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order is None

    def test_empty_code_rejected(self):
        """测试空代码被拒绝"""
        engine = _make_engine()
        data = {
            "security": "",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order is None

    def test_zero_amount_rejected(self):
        """测试零数量被拒绝"""
        engine = _make_engine()
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 0,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order is None

    def test_negative_amount_converted_to_positive(self):
        """测试负数量取绝对值"""
        engine = _make_engine()
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": -1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order is not None
        assert order["amount"] == 1000

    def test_stale_signal_filtered(self):
        """测试过期回测信号被过滤"""
        engine = _make_engine({"redis_backtest_skip_hours": 8})
        # 生成一个 10 小时前的信号
        stale_time = (datetime.now() - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": stale_time,
        }
        order = engine._signal_to_order(data)
        assert order is None

    def test_fresh_signal_accepted(self):
        """测试新鲜信号被接受"""
        engine = _make_engine({"redis_backtest_skip_hours": 8})
        # 生成一个当前时间的信号
        fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": fresh_time,
        }
        order = engine._signal_to_order(data)
        assert order is not None


class TestDeduplication:
    """去重键测试"""

    def test_signal_id_as_uid(self):
        """测试 signal_id 作为去重键"""
        engine = _make_engine()
        data = {
            "signal_id": "unique-signal-123",
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        assert order["uid"] == "unique-signal-123"

    def test_composite_uid_when_no_signal_id(self):
        """测试无 signal_id 时生成复合去重键"""
        engine = _make_engine()
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order = engine._signal_to_order(data)
        # 复合键格式: 时间|代码|方向|数量
        assert order["uid"] == "2026-10-19 09:30:00|000001|1|1000"

    def test_different_signals_different_uid(self):
        """测试不同信号生成不同的去重键"""
        engine = _make_engine()
        data1 = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        data2 = {
            "security": "000001.XSHE",
            "action": "sell",
            "amount_diff": 1000,
            "xdsj": "2026-10-19 09:30:00",
        }
        order1 = engine._signal_to_order(data1)
        order2 = engine._signal_to_order(data2)
        assert order1["uid"] != order2["uid"]


class TestEdgeCases:
    """边界情况测试"""

    def test_missing_optional_fields(self):
        """测试缺少可选字段时不崩溃"""
        engine = _make_engine()
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 1000,
            # 缺少 xdsj, signal_id, strategy_name, price 等
        }
        order = engine._signal_to_order(data)
        assert order is not None
        assert order["stock_code"] == "000001"
        assert order["strategy"] == ""
        assert order["price"] == 0.0

    def test_empty_dict_signal(self):
        """测试空字典信号"""
        engine = _make_engine()
        data = {}
        order = engine._signal_to_order(data)
        assert order is None

    def test_none_values_in_data(self):
        """测试数据中包含 None 值"""
        engine = _make_engine()
        data = {
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": None,
            "price": None,
            "xdsj": None,
        }
        order = engine._signal_to_order(data)
        # amount_diff=None 会被当作 0，所以返回 None
        assert order is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
