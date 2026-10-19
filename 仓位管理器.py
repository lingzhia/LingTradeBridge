#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
仓位管理器
===========
管理各策略的独立资金池、持仓记录、资金计算。
数据持久化到 JSON 文件，支持手动编辑。

位置: 本地 Windows 电脑，与「聚宽转同花顺跟单器.py」放在同一目录，
由跟单器自动调用，不需要单独运行。

设计:
- 独立资金池: 每个策略有独立初始资金，互不挪用
- 加权平均成本: 买入同一股票时按加权平均计算持仓成本
- 已用资金 = Σ(持仓股数 × 成本均价)
- 可用资金 = 初始资金 - 已用资金
- 现价: 手动维护的当前股价，用于估算市值和浮动盈亏
- 实际总资产: 手动填入的真实总资产（如聚宽上的值），与估算市值的差值自动计算
"""

import json
import os


class PositionManager:
    """仓位管理器: 策略资金分配 + 持仓记录 + 资金计算"""

    DATA_FILE = "跟单仓位管理.json"

    def __init__(self, base_dir):
        self.path = os.path.join(base_dir, self.DATA_FILE)
        self.data = {}
        self.load()

    # ==================== 持久化 ====================

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        # 初始化顺序列表
        if "__order__" not in self.data:
            self.data["__order__"] = sorted(
                [k for k in self.data.keys() if k != "__order__"]
            )
    
    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print("保存仓位数据失败: %s" % e)

    # ==================== 策略管理 ====================

    def ensure_strategy(self, name, initial_capital=None):
        """确保策略存在，若不存在则创建；若指定了 initial_capital 且策略刚创建则设置"""
        if name not in self.data:
            self.data[name] = {
                "initial_capital": initial_capital if initial_capital is not None else 0,
                "available_capital": initial_capital if initial_capital is not None else 0,
                "ratio_buy": 1.0,
                "ratio_sell": 1.0,
                "jq_total_asset": 0.0,
                "follow_total_asset": 0.0,
                "follow_asset_manual": False,
                "holdings": {}
            }
            self.data.setdefault("__order__", []).append(name)
            self.save()
        elif initial_capital is not None:
            old_ic = self.data[name].get("initial_capital", 0)
            diff = initial_capital - old_ic
            self.data[name]["initial_capital"] = initial_capital
            # 可用资金同步增减
            old_avail = self.data[name].get("available_capital", old_ic)
            self.data[name]["available_capital"] = round(old_avail + diff, 2)
            self.save()

    def delete_strategy(self, name):
        """删除策略及其所有持仓"""
        if name in self.data:
            del self.data[name]
        order = self.data.get("__order__", [])
        if name in order:
            order.remove(name)
        self.save()

    def rename_strategy(self, old_name, new_name):
        """重命名策略"""
        if old_name in self.data and new_name not in self.data:
            self.data[new_name] = self.data.pop(old_name)
            order = self.data.get("__order__", [])
            if old_name in order:
                order[order.index(old_name)] = new_name
            self.save()
            return True
        return False

    def get_strategies(self):
        """返回所有策略名列表（排除 __order__ 内部键）"""
        return [k for k in self.data.keys() if k != "__order__"]

    def get_strategy_info(self, name):
        """返回策略完整信息 dict"""
        return self.data.get(name, {"initial_capital": 0, "holdings": {}})

    def set_initial_capital(self, name, capital):
        """设置/修改策略初始资金, 同时同步到聚宽策略总资产"""
        self.ensure_strategy(name, initial_capital=capital)
        if name in self.data:
            self.data[name]["jq_total_asset"] = float(capital)
            self.save()

    def set_available_capital(self, name, capital):
        """手动设置策略可用资金"""
        self.ensure_strategy(name)
        self.data[name]["available_capital"] = round(float(capital), 2)
        self.save()

    def get_strategy_order(self):
        """返回策略显示顺序列表"""
        return list(self.data.get("__order__", []))

    def reorder_strategy(self, name, new_index):
        """将策略移动到指定索引位置"""
        order = self.data.get("__order__", [])
        if name not in order:
            return
        order.remove(name)
        order.insert(new_index, name)
        self.save()

    # ==================== 跟单比例 ====================

    def get_strategy_ratio_buy(self, name):
        """返回指定策略的买入跟单比例，不存在返回 None"""
        info = self.data.get(name)
        if info is None:
            return None
        # 兼容旧数据: 旧字段 ratio 作为买卖共同默认
        if "ratio_buy" not in info and "ratio" in info:
            return info.get("ratio", 1.0)
        return info.get("ratio_buy", 1.0)

    def get_strategy_ratio_sell(self, name):
        """返回指定策略的卖出跟单比例，不存在返回 None"""
        info = self.data.get(name)
        if info is None:
            return None
        # 兼容旧数据: 旧字段 ratio 作为买卖共同默认
        if "ratio_sell" not in info and "ratio" in info:
            return info.get("ratio", 1.0)
        return info.get("ratio_sell", 1.0)

    def set_strategy_ratio_buy(self, name, ratio):
        """设置策略的买入跟单比例"""
        self.ensure_strategy(name)
        self.data[name]["ratio_buy"] = float(ratio)
        self.save()

    def set_strategy_ratio_sell(self, name, ratio):
        """设置策略的卖出跟单比例"""
        self.ensure_strategy(name)
        self.data[name]["ratio_sell"] = float(ratio)
        self.save()

    # ==================== 资金计算 ====================

    def get_used_capital(self, strategy):
        """计算已用资金 = Σ(持仓股数 × 成本均价)"""
        info = self.data.get(strategy, {})
        holdings = info.get("holdings", {})
        total = 0.0
        for code, h in holdings.items():
            total += h.get("shares", 0) * h.get("cost_price", 0)
        return round(total, 2)

    def get_available_capital(self, strategy):
        """可用资金 = 已存储的可用资金值（支持手动编辑）"""
        info = self.data.get(strategy, {})
        # 向后兼容：旧数据没有 available_capital 时用初始资金-已用资金
        if "available_capital" not in info:
            return round(info.get("initial_capital", 0) - self.get_used_capital(strategy), 2)
        return info.get("available_capital", 0)

    # ==================== 持仓操作 ====================

    def buy(self, strategy, code, name, shares, price):
        """
        买入: 合并持仓, 加权平均成本
        - 若已有该股票: 加权平均 cost_price = (old_shares*old_cost + new_shares*price) / total
        - 若没有: 新建持仓
        - 同时更新 current_price 为最新成交价
        """
        self.ensure_strategy(strategy)
        holdings = self.data[strategy]["holdings"]
        if code in holdings:
            old = holdings[code]
            old_shares = old["shares"]
            old_cost = old["cost_price"]
            new_shares = old_shares + shares
            new_cost = (old_shares * old_cost + shares * price) / new_shares
            old["shares"] = new_shares
            old["cost_price"] = round(new_cost, 3)
            old["name"] = name
            old["current_price"] = price
        else:
            holdings[code] = {
                "name": name,
                "shares": shares,
                "cost_price": price,
                "current_price": price
            }
        self.save()
        return True

    def sell(self, strategy, code, shares):
        """
        卖出: 减少持仓
        - 从现有持仓中扣减股数
        - 若扣减后股数 ≤ 0 则删除该持仓
        - 不检查持仓是否足够（按原逻辑执行）
        """
        self.ensure_strategy(strategy)
        holdings = self.data[strategy]["holdings"]
        if code in holdings:
            old = holdings[code]
            old["shares"] -= shares
            if old["shares"] <= 0:
                del holdings[code]
        self.save()
        return True

    # ==================== 手动编辑 ====================

    def update_holding(self, strategy, code, name=None, shares=None, cost_price=None, current_price=None):
        """手动修改指定持仓的字段，None 表示不修改"""
        self.ensure_strategy(strategy)
        holdings = self.data[strategy]["holdings"]
        if code not in holdings:
            holdings[code] = {"name": "", "shares": 0, "cost_price": 0.0, "current_price": 0.0}
        h = holdings[code]
        if name is not None:
            h["name"] = name
        if shares is not None:
            h["shares"] = shares
        if cost_price is not None:
            h["cost_price"] = cost_price
        if current_price is not None:
            h["current_price"] = current_price
        if h["shares"] <= 0:
            del holdings[code]
        self.save()

    def delete_holding(self, strategy, code):
        """删除指定持仓"""
        holdings = self.data.get(strategy, {}).get("holdings", {})
        if code in holdings:
            del holdings[code]
            self.save()

    def add_holding(self, strategy, code, name, shares, cost_price, current_price=None):
        """手动添加一条持仓（不合并，直接覆盖）"""
        self.ensure_strategy(strategy)
        h = self.data[strategy]["holdings"]
        if current_price is None:
            current_price = cost_price
        if code in h:
            # 如果已有，合并处理（加权平均）
            old = h[code]
            total_shares = old["shares"] + shares
            avg_price = (old["shares"] * old["cost_price"] + shares * cost_price) / total_shares
            h[code] = {"name": name, "shares": total_shares, "cost_price": round(avg_price, 3), "current_price": current_price}
        else:
            h[code] = {"name": name, "shares": shares, "cost_price": cost_price, "current_price": current_price}
        self.save()

    # ==================== 统计数据 ====================

    def get_summary(self):
        """返回所有策略的汇总统计（含估算市值、浮动盈亏、实际总资产）"""
        rows = []
        order = self.data.get("__order__", [])
        if not order:
            order = sorted([k for k in self.data.keys() if k != "__order__"])
        for name in order:
            if name == "__order__" or name not in self.data:
                continue
            info = self.data[name]
            used = self.get_used_capital(name)
            available = self.get_available_capital(name)
            # 计算估算市值 = Σ(股数 × 现价)
            holdings = info.get("holdings", {})
            market_value = 0.0
            for code, h in holdings.items():
                cur = h.get("current_price", h.get("cost_price", 0))
                market_value += h.get("shares", 0) * cur
            market_value = round(market_value, 2)
            floating_pnl = round(market_value - used, 2)
            # 实际总资产 = 可用资金 + 估算市值
            actual_total_asset = round(available + market_value, 2)
            _b = self.get_strategy_ratio_buy(name)
            _s = self.get_strategy_ratio_sell(name)
            rows.append({
                "name": name,
                "ratio_buy": _b if _b is not None else 1.0,
                "ratio_sell": _s if _s is not None else 1.0,
                "used_capital": used,
                "available_capital": available,
                "market_value": market_value,
                "floating_pnl": floating_pnl,
                "actual_total_asset": actual_total_asset,
                "jq_total_asset": self.get_jq_total_asset(name),
                "follow_total_asset": self.get_follow_total_asset(name),
                "holding_count": len(holdings)
            })
        return rows

    def get_holdings(self, strategy):
        """返回指定策略的持仓明细列表（含策略名称、现价、市值、浮动盈亏）"""
        info = self.data.get(strategy, {})
        holdings = info.get("holdings", {})
        rows = []
        for code in sorted(holdings.keys()):
            h = holdings[code]
            shares = h.get("shares", 0)
            cost_price = h.get("cost_price", 0.0)
            current_price = h.get("current_price", cost_price)
            used = round(shares * cost_price, 2)
            market_value = round(shares * current_price, 2)
            floating_pnl = round(market_value - used, 2)
            rows.append({
                "strategy": strategy,
                "code": code,
                "name": h.get("name", ""),
                "shares": shares,
                "cost_price": cost_price,
                "current_price": current_price,
                "used": used,
                "market_value": market_value,
                "floating_pnl": floating_pnl
            })
        return rows

    def get_all_holdings(self):
        """返回所有策略的全部持仓明细列表"""
        rows = []
        for name in sorted(self.get_strategies()):
            rows.extend(self.get_holdings(name))
        return rows

    def get_holding_shares(self, strategy, code):
        """返回指定策略下指定股票的持仓股数；无该标的或策略不存在返回 None"""
        info = self.data.get(strategy)
        if info is None:
            return None
        holdings = info.get("holdings", {})
        h = holdings.get(code)
        if h is None:
            return None
        return h.get("shares", 0)

    # ==================== 现价 & 实际总资产 ====================

    def set_current_price(self, strategy, code, price):
        """设置持仓的当前价格"""
        self.ensure_strategy(strategy)
        holdings = self.data[strategy].get("holdings", {})
        if code in holdings:
            holdings[code]["current_price"] = float(price)
            self.save()

    def set_actual_total_asset(self, strategy, asset):
        """设置策略的实际总资产"""
        self.ensure_strategy(strategy)
        self.data[strategy]["actual_total_asset"] = float(asset)
        self.save()

    # ==================== 聚宽策略总资产 & 按比例买入可用资金 ====================

    def get_jq_total_asset(self, name):
        """返回策略的聚宽策略总资产(手动填写)"""
        return float(self.data.get(name, {}).get("jq_total_asset", 0) or 0)

    def set_jq_total_asset(self, name, asset):
        """设置策略的聚宽策略总资产(手动填写);
        未手动覆盖按比例买入可用资金时, 其显示值会自动按公式(聚宽总资产×买入比例)重算"""
        self.ensure_strategy(name)
        self.data[name]["jq_total_asset"] = float(asset)
        self.save()

    def set_follow_total_asset(self, name, asset):
        """手动设置策略的按比例买入可用资金(设置后不再随公式自动重算, 即手动覆盖)"""
        self.ensure_strategy(name)
        self.data[name]["follow_total_asset"] = float(asset)
        self.data[name]["follow_asset_manual"] = True
        self.save()

    def get_follow_total_asset(self, name):
        """返回策略的按比例买入可用资金: 默认=聚宽策略总资产×买入比例, 手动覆盖过则返回手动值"""
        info = self.data.get(name, {})
        _b = self.get_strategy_ratio_buy(name)
        r_buy = _b if _b is not None else 1.0
        if info.get("follow_asset_manual"):
            return round(float(info.get("follow_total_asset", 0) or 0), 2)
        jq = self.get_jq_total_asset(name)
        return round(jq * r_buy, 2)

    def reset_follow_total_asset(self, name):
        """清除手动覆盖, 按比例买入可用资金恢复为公式值"""
        if name in self.data:
            self.data[name]["follow_asset_manual"] = False
            self.save()