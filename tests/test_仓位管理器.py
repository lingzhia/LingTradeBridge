#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""仓位管理器单元测试"""

import json
import os
import tempfile
import pytest
from 仓位管理器 import PositionManager


@pytest.fixture
def temp_dir():
    """创建临时目录用于测试"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def pos_mgr(temp_dir):
    """创建测试用的仓位管理器实例"""
    return PositionManager(temp_dir)


class TestPositionManager:
    """仓位管理器测试"""

    def test_ensure_strategy_creates_new(self, pos_mgr):
        """测试创建新策略"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        info = pos_mgr.get_strategy_info("测试策略")
        assert info["initial_capital"] == 100000
        assert info["available_capital"] == 100000
        assert info["holdings"] == {}

    def test_ensure_strategy_idempotent(self, pos_mgr):
        """测试重复创建同一策略不会覆盖"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.5)
        pos_mgr.ensure_strategy("测试策略", initial_capital=200000)
        info = pos_mgr.get_strategy_info("测试策略")
        # 资金应该被更新
        assert info["initial_capital"] == 200000
        # 但持仓应该保留
        assert "000001" in info["holdings"]

    def test_buy_new_holding(self, pos_mgr):
        """测试买入新建持仓"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.5)
        holdings = pos_mgr.get_strategy_info("测试策略")["holdings"]
        assert "000001" in holdings
        h = holdings["000001"]
        assert h["shares"] == 1000
        assert h["cost_price"] == 10.5
        assert h["name"] == "平安银行"

    def test_buy_merge_weighted_average(self, pos_mgr):
        """测试买入合并持仓（加权平均成本）"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        # 第一次买入: 1000股 @ 10.0
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        # 第二次买入: 2000股 @ 11.0
        pos_mgr.buy("测试策略", "000001", "平安银行", 2000, 11.0)
        holdings = pos_mgr.get_strategy_info("测试策略")["holdings"]
        h = holdings["000001"]
        # 总股数应该是 3000
        assert h["shares"] == 3000
        # 加权平均成本 = (1000*10 + 2000*11) / 3000 = 32000/3000 = 10.666...
        expected_cost = (1000 * 10.0 + 2000 * 11.0) / 3000
        assert abs(h["cost_price"] - expected_cost) < 0.01

    def test_sell_reduce_shares(self, pos_mgr):
        """测试卖出减少持仓"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        pos_mgr.sell("测试策略", "000001", 300)
        holdings = pos_mgr.get_strategy_info("测试策略")["holdings"]
        assert holdings["000001"]["shares"] == 700

    def test_sell_delete_holding(self, pos_mgr):
        """测试卖出全部持仓后删除"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        pos_mgr.sell("测试策略", "000001", 1000)
        holdings = pos_mgr.get_strategy_info("测试策略")["holdings"]
        assert "000001" not in holdings

    def test_get_used_capital(self, pos_mgr):
        """测试已用资金计算"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        pos_mgr.buy("测试策略", "000002", "万科A", 500, 20.0)
        used = pos_mgr.get_used_capital("测试策略")
        # 1000*10 + 500*20 = 10000 + 10000 = 20000
        assert used == 20000.0

    def test_get_available_capital_fallback(self, pos_mgr):
        """测试可用资金计算（回退模式：initial - used）"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        # 删除 available_capital 字段以触发回退计算
        del pos_mgr.data["测试策略"]["available_capital"]
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        available = pos_mgr.get_available_capital("测试策略")
        # 回退模式: 100000 - 1000*10 = 90000
        assert available == 90000.0

    def test_get_available_capital_stored(self, pos_mgr):
        """测试可用资金（存储值，不随买卖自动变化）"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        # available_capital 默认等于 initial_capital
        assert pos_mgr.get_available_capital("测试策略") == 100000.0
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        # 买入后 available_capital 不变（需要手动更新）
        assert pos_mgr.get_available_capital("测试策略") == 100000.0
        # 手动设置可用资金
        pos_mgr.set_available_capital("测试策略", 85000)
        assert pos_mgr.get_available_capital("测试策略") == 85000.0

    def test_strategy_isolation(self, pos_mgr):
        """测试策略资金池隔离"""
        pos_mgr.ensure_strategy("策略A", initial_capital=100000)
        pos_mgr.ensure_strategy("策略B", initial_capital=50000)
        pos_mgr.buy("策略A", "000001", "平安银行", 1000, 10.0)
        # 策略A的买入不应影响策略B的持仓
        holdings_b = pos_mgr.get_strategy_info("策略B")["holdings"]
        assert len(holdings_b) == 0
        # 策略A的已用资金不影响策略B
        assert pos_mgr.get_used_capital("策略B") == 0.0

    def test_atomic_save(self, pos_mgr, temp_dir):
        """测试原子写入：写入过程中断不会损坏文件"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        # 验证文件存在且内容正确
        data_file = os.path.join(temp_dir, "跟单仓位管理.json")
        assert os.path.exists(data_file)
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "测试策略" in data
        assert "000001" in data["测试策略"]["holdings"]
        # 验证没有残留的 .tmp 文件
        assert not os.path.exists(data_file + ".tmp")

    def test_persistence_across_reload(self, temp_dir):
        """测试数据持久化：重新加载后数据不丢失"""
        mgr1 = PositionManager(temp_dir)
        mgr1.ensure_strategy("测试策略", initial_capital=100000)
        mgr1.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        # 创建新实例（模拟重启）
        mgr2 = PositionManager(temp_dir)
        info = mgr2.get_strategy_info("测试策略")
        assert info["initial_capital"] == 100000
        assert "000001" in info["holdings"]
        assert info["holdings"]["000001"]["shares"] == 1000

    def test_delete_strategy(self, pos_mgr):
        """测试删除策略"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.buy("测试策略", "000001", "平安银行", 1000, 10.0)
        pos_mgr.delete_strategy("测试策略")
        assert "测试策略" not in pos_mgr.get_strategies()

    def test_rename_strategy(self, pos_mgr):
        """测试重命名策略"""
        pos_mgr.ensure_strategy("旧名称", initial_capital=100000)
        pos_mgr.buy("旧名称", "000001", "平安银行", 1000, 10.0)
        pos_mgr.rename_strategy("旧名称", "新名称")
        assert "旧名称" not in pos_mgr.get_strategies()
        assert "新名称" in pos_mgr.get_strategies()
        info = pos_mgr.get_strategy_info("新名称")
        assert "000001" in info["holdings"]

    def test_get_summary(self, pos_mgr):
        """测试汇总统计"""
        pos_mgr.ensure_strategy("策略A", initial_capital=100000)
        pos_mgr.ensure_strategy("策略B", initial_capital=50000)
        pos_mgr.buy("策略A", "000001", "平安银行", 1000, 10.0)
        summary = pos_mgr.get_summary()
        assert len(summary) == 2
        # 验证策略A的统计
        summary_a = next(s for s in summary if s["name"] == "策略A")
        assert summary_a["used_capital"] == 10000.0
        # available_capital 是存储值，不会自动扣减
        assert summary_a["available_capital"] == 100000.0
        assert summary_a["holding_count"] == 1


class TestPositionManagerRatio:
    """跟单比例测试"""

    def test_default_ratio(self, pos_mgr):
        """测试默认跟单比例"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        assert pos_mgr.get_strategy_ratio_buy("测试策略") == 1.0
        assert pos_mgr.get_strategy_ratio_sell("测试策略") == 1.0

    def test_set_ratio(self, pos_mgr):
        """测试设置跟单比例"""
        pos_mgr.ensure_strategy("测试策略", initial_capital=100000)
        pos_mgr.set_strategy_ratio_buy("测试策略", 0.5)
        pos_mgr.set_strategy_ratio_sell("测试策略", 0.8)
        assert pos_mgr.get_strategy_ratio_buy("测试策略") == 0.5
        assert pos_mgr.get_strategy_ratio_sell("测试策略") == 0.8

    def test_ratio_nonexistent_strategy(self, pos_mgr):
        """测试查询不存在策略的比例"""
        assert pos_mgr.get_strategy_ratio_buy("不存在") is None
        assert pos_mgr.get_strategy_ratio_sell("不存在") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
