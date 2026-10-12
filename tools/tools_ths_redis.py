# =============================================================
# >>> 同花顺跟单专用·精简版 tools_ths_redis.py <<<
#   - 由 tools_v6.3.py 精简而来
#   - 原创：🐷猪头战法  From https://www.joinquant.com/view/community/detail/75424
#   - 仅保留 Redis 信号推送(直连模式)
#   - 已删除同花顺跟单用不到的功能: 企业微信/飞书推送、URL连接模式、
#     撤单信号上报、目标仓位(weight)计算
#   - 信号 JSON 格式与「聚宽转同花顺跟单器.py」完全对接
# =============================================================
# 使用方法（3 步）:
#   1. 修改下方配置区的 Redis 连接信息（与跟单器「信号源 · Redis」一致）
#   2. 把本文件上传到聚宽研究环境，策略里 import tools_ths_redis
#      （或直接把本文件内容复制到策略代码最前面）
#   3. 在策略 initialize 里加一行: tools_ths_redis.setup(context, '策略名')
#   之后 order / order_target 等下单会自动推送信号，策略代码无需任何改动
# =============================================================
import json, time, datetime, sys, redis

# ====================================================================================================== 配置开始
redis_enabled = True       # Redis 总开关：True=启用推送 | False=关闭
CHANNELS = ["jq_signals_ths", "jq_signals_ths_mac"]        # 同花顺跟单专用通道, 必须与跟单器的「通道」配置一致
# ---------------------------------------------------------------------------
# 调试开关：
#   True = 打印详细诊断日志（测试期建议开，可看到 wrapper 自修复、绑定等信息）
#   False = 只打印关键日志（正式上线后建议关，减少日志噪音）
DEBUG = False
# ---------------------------------------------------------------------------
# 【开盘前预热】True=到 PREHEAT_TIMES 设定时刻自动推送 preheat 信号
#        让跟单器提前把窗口激活好(切到控制台会话), 首个真实买入信号零延迟
# PREHEAT_TIMES = 预热触发时间点(建议填你固定交易时刻的前1分钟), 格式 HH:MM:SS
preheat_enabled = False
PREHEAT_TIMES = ["09:29:30"]
# ---------------------------------------------------------------------------
# 【Redis 直连】参数（与跟单器「信号源 · Redis」卡片保持一致）
# ⚠️ 必须填写【你自己的】Redis 连接信息(地址/端口/密码), 否则信号发不出去。
#    这三个值要与「网页跟单配置.json」里填的完全一致。
REDIS_HOST, REDIS_PORT, REDIS_PASSWORD = "", 6379, ""
# ====================================================================================================== 配置结束


def _log(msg):
    print(f"[👺tools] {msg}")


def _dbg(msg):
    if DEBUG:
        print(f"[👺tools][dbg] {msg}")


# ---------- 状态存取 ----------
_last_refresh_ts = {}
_last_morning_minute = None
_last_afternoon_minute = None


def _get_g():
    if 'kuanke.user_space_api' in sys.modules:
        return sys.modules['kuanke.user_space_api'].g
    return None


def _get_strategy_name():
    g = _get_g()
    if g and hasattr(g, '_strategy_name') and g._strategy_name and g._strategy_name != "未命名策略":
        return g._strategy_name

    # 从调用模块的全局变量读取 STRATEGY_NAME
    try:
        import inspect
        frame = inspect.currentframe()
        while frame:
            module = inspect.getmodule(frame)
            if module and hasattr(module, 'STRATEGY_NAME'):
                return module.STRATEGY_NAME
            frame = frame.f_back
    except:
        pass

    return "未命名策略"


# ---------- Redis ----------
_redis_client = None


def _init_redis():
    global _redis_client
    if not redis_enabled or redis is None:
        return False
    try:
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
            decode_responses=True, socket_connect_timeout=5, socket_keepalive=True
        )
        _redis_client.ping()
        return True
    except Exception as e:
        _redis_client = None
        _log(f"Redis 连接失败: {e}")
        return False


def _ensure_redis():
    global _redis_client
    if not redis_enabled or redis is None:
        return False
    if _redis_client:
        try:
            _redis_client.ping()
            return True
        except Exception:
            _redis_client = None
            _dbg("Redis 连接已失效，准备重建")
    return _init_redis()


def _send_signal(code, action, amount, price, dt_str=None):
    global _redis_client
    if not _ensure_redis():
        return False
    if dt_str is None:
        dt_str = time.strftime("%Y-%m-%d %H:%M:%S")
    signal_id = f"{_get_strategy_name()}:{code}:{action}:{abs(int(amount))}:{int(time.time() * 1000)}"
    # 买卖 action 归一化; 其它动作(preheat等)原样透传
    if action in ("buy", "买入"):
        act = "buy"
    elif action in ("sell", "卖出"):
        act = "sell"
    else:
        act = str(action)
    data = {
        "msg_version": "1.0",
        "signal_id": signal_id,
        "strategy_name": _get_strategy_name(),
        "security": str(code),
        "action": act,
        "current_price": float(price),
        "amount_diff": abs(int(amount)),
        "trade_value": round(float(price) * int(amount), 2),
        "xdsj": dt_str,
        "current_time": time.time()
    }
    try:
        for ch in CHANNELS:
            result = _redis_client.rpush(ch, json.dumps(data, ensure_ascii=False))
            _log(f"rpush {ch} 结果: {result} (类型: {type(result).__name__})")
        return True
    except Exception as e:
        _redis_client = None
        _log(f"Redis 发送异常: {e}")
        return False


# ---------- 统一订单上报（兼容 9:26-9:30 集合竞价数据未就绪） ----------
def _report_order(order):
    if order is None:
        return
    amt = order.amount
    if abs(amt) <= 0:
        _dbg(f"过滤零数量订单: {getattr(order, 'security', 'N/A')}")
        return

    # 【前置】先提取订单时间，后续多处使用
    order_time = getattr(order, 'add_time', None)

    # ========== 价格获取：兼容 9:26-9:30 集合竞价数据未就绪 ==========
    prc = 0.0

    # 1. 优先 order.price（委托价/成交价）
    try:
        if order.price and float(order.price) > 0:
            prc = float(order.price)
    except Exception:
        pass

    # 2. 其次 avg_cost（实际成交均价）—— 仅买入订单使用！
    # ⚠️ 卖出订单的 avg_cost 是持仓成本价，不能作为卖出价格
    if prc <= 0 and getattr(order, 'is_buy', False):
        try:
            avg_cost = float(getattr(order, 'avg_cost', 0) or 0)
            if avg_cost > 0:
                prc = avg_cost
                _dbg(f"avg_cost 买入成交: {order.security}={prc}")
        except Exception:
            pass

    # 3. 再试 get_current_data（聚宽注入到策略主模块的全局函数, 需通过 sys.modules 获取）
    if prc <= 0:
        try:
            gcd = None
            for mod_name in ('__main__', 'user_code', '__builtin__', 'builtins'):
                mod = sys.modules.get(mod_name)
                if mod and hasattr(mod, 'get_current_data'):
                    gcd = mod.get_current_data
                    break
            if gcd is None:
                gcd = globals().get('get_current_data')
            if gcd:
                data = gcd()[order.security]
                # 9:26-9:30 集合竞价期间，day_open 比 last_price 更可靠
                # last_price 此时可能是昨收，不是开盘价
                for field in ['day_open', 'last_price', 'high_limit', 'low_limit']:
                    val = getattr(data, field, None)
                    if val and float(val) > 0:
                        prc = float(val)
                        break
        except Exception as e:
            _dbg(f"get_current_data 获取失败: {e}")

    # 3.5 尝试 get_call_auction（聚宽专门取集合竞价数据的 API）
    if prc <= 0:
        try:
            gca = None
            for mod_name in ('__main__', 'user_code', '__builtin__', 'builtins'):
                mod = sys.modules.get(mod_name)
                if mod and hasattr(mod, 'get_call_auction'):
                    gca = mod.get_call_auction
                    break
            if gca is None:
                gca = globals().get('get_call_auction')
            if gca:
                today_str = order_time.strftime('%Y-%m-%d') if isinstance(order_time, datetime.datetime) else time.strftime('%Y-%m-%d')
                df = gca(order.security, start_date=today_str, end_date=today_str, fields=['current'])
                if df is not None and not df.empty:
                    price = float(df['current'].iloc[0])
                    if price > 0:
                        prc = price
                        _dbg(f"get_call_auction 兜底: {order.security}={prc}")
        except Exception as e:
            _dbg(f"get_call_auction 获取失败: {e}")

    # 4. 最终兜底：即使价格仍为 0，也强制发出信号
    #    （价格 0 也比"毛信号都没有"强，至少能知道发生了交易）
    if prc <= 0:
        prc = 0.0
        _log(f"⚠️ 警告: {order.security} 未能获取有效价格，使用 0 作为 fallback，请检查数据接口")

    # ========== 组装并推送信号 ==========
    s = str(order.security)
    a = "买入" if order.is_buy else "卖出"
    dt_str = order_time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(order_time, datetime.datetime) else time.strftime("%Y-%m-%d %H:%M:%S")
    _log(f"信号: {a} {s} {abs(amt)}股 @ {prc:.3f}")

    redis_ok = False
    redis_err = ""
    try:
        redis_ok = _send_signal(s, a, abs(amt), prc, dt_str=dt_str)
    except Exception as e:
        redis_err = str(e)
    _log(f"推送结果: {'Redis ✓' if redis_ok else ('Redis ✗ ' + redis_err if redis_err else 'Redis ✗')}")


# ---------- 装饰器 ----------
def _make_wrapper(original_func, func_name):
    if getattr(original_func, '_tools_wrapped', False):
        original_func = original_func._tools_original

    def wrapper(*a, **k):
        # 自修复: 聚宽可能在进程内重置 jqdata 函数, 每次调用时检查并重新绑定
        try:
            import jqdata
            current_jq = getattr(jqdata, func_name, None)
            if current_jq is not wrapper and not getattr(current_jq, '_tools_wrapped', False):
                _dbg(f"自修复: jqdata.{func_name} 被重置，重新绑定")
                setattr(jqdata, func_name, wrapper)
                um = _get_caller_module()
                if um and hasattr(um, func_name):
                    setattr(um, func_name, wrapper)
        except Exception:
            pass

        r = original_func(*a, **k)
        if r is not None:
            if isinstance(r, list):
                for x in r:
                    if x is not None:
                        _report_order(x)
            else:
                _report_order(r)
        return r

    wrapper.__name__ = getattr(original_func, '__name__', func_name)
    wrapper.__qualname__ = getattr(original_func, '__qualname__', func_name)
    wrapper.__module__ = getattr(original_func, '__module__', 'jqdata')
    wrapper.__doc__ = getattr(original_func, '__doc__', None)
    wrapper._tools_wrapped = True
    wrapper._tools_original = original_func
    wrapper._tools_func_name = func_name
    return wrapper


def _patch_orders_in_module(module, force=False):
    TARGET = ['order', 'order_value', 'order_target', 'order_target_value', 'order_target_percent']
    count = 0
    for fn_name in TARGET:
        if hasattr(module, fn_name):
            current_func = getattr(module, fn_name)
            if not callable(current_func):
                continue
            if force and getattr(current_func, '_tools_wrapped', False):
                current_func = current_func._tools_original
            if not force and getattr(current_func, '_tools_wrapped', False):
                continue
            setattr(module, fn_name, _make_wrapper(current_func, fn_name))
            count += 1
    return count


def _get_caller_module():
    if 'user_code' in sys.modules:
        return sys.modules['user_code']
    import inspect
    frame = inspect.currentframe()
    try:
        while frame:
            module = inspect.getmodule(frame)
            if module and module.__name__ not in ('tools_v5', 'kuanke.user_space_api', __name__) \
                    and not module.__name__.startswith('_') \
                    and not module.__name__.startswith('jq'):
                return module
            frame = frame.f_back
    finally:
        del frame
    return None


# ==================== 模块顶层钩子（可序列化）====================
def _enhanced_process_initialize(ctx):
    # 进程重启时自动刷新，并调用用户原始函数
    _log("进程重启自动刷新注入...")
    refresh(ctx)
    um = sys.modules.get('user_code')
    if um:
        orig = getattr(um, '_tools_original_process_initialize', None)
        if orig:
            try:
                orig(ctx)
            except Exception as e:
                _log(f"用户 process_initialize 异常: {e}")


def _morning_refresh(context):
    global _last_morning_minute
    now_minute = time.strftime('%H:%M')
    if _last_morning_minute == now_minute:
        return
    _last_morning_minute = now_minute
    _log("09:30 开盘自动恢复...")
    refresh(context)


def _afternoon_refresh(context):
    global _last_afternoon_minute
    now_minute = time.strftime('%H:%M')
    if _last_afternoon_minute == now_minute:
        return
    _last_afternoon_minute = now_minute
    _log("13:00 下午开盘自动恢复...")
    refresh(context)


def _preheat(context=None):
    """开盘前预热: 推送 preheat 信号, 让跟单器提前激活窗口(切到控制台会话),
    首个真实买卖信号到来时零延迟。仅在 preheat_enabled 时运行(由 run_daily 触发)。
    context 参数由 run_daily 回调传入, 此处不使用(仅为占位)。"""
    if not preheat_enabled:
        return
    try:
        _log("预热: 推送 preheat 信号, 提前激活下单窗口")
        _send_signal("000000", "preheat", 0, 0)
    except Exception as e:
        _log(f"预热推送异常: {e}")


# ==================== 对外接口 ====================
def setup(context, name):
    g = _get_g()
    if g:
        g._strategy_name = str(name)

    _init_redis()

    strategy_module = _get_caller_module()
    try:
        import jqdata
        _patch_orders_in_module(jqdata)
    except Exception as e:
        _log(f"jqdata 注入异常: {e}，将自动重试")

    if strategy_module:
        _patch_orders_in_module(strategy_module)
        original_process = getattr(strategy_module, 'process_initialize', None)
        if original_process and not getattr(original_process, '_tools_wrapped', False):
            strategy_module._tools_original_process_initialize = original_process
        if not getattr(getattr(strategy_module, 'process_initialize', None), '_tools_wrapped', False):
            strategy_module.process_initialize = _enhanced_process_initialize
    else:
        _log("注意：未检测到策略模块（研究环境正常），信号功能需手动调用 setup()")

    _try_register_run_daily()
    _log(f"策略 [{name}] 初始化完成 | Redis:{'ON' if redis_enabled else 'OFF'}")


def refresh(context, name=None):
    global _last_refresh_ts
    name = name or _get_strategy_name()
    now = time.time()
    if name in _last_refresh_ts and now - _last_refresh_ts[name] < 30:
        _dbg(f"refresh 去重: 策略 [{name}] 距上次刷新仅 {now - _last_refresh_ts[name]:.1f} 秒，跳过")
        return
    _last_refresh_ts[name] = now

    g = _get_g()
    if g:
        if name and name != "未命名策略":
            g._strategy_name = str(name)

    try:
        import jqdata
        _patch_orders_in_module(jqdata, force=True)
    except Exception as e:
        _log(f"jqdata 刷新异常: {e}，将自动重试")

    strategy_module = _get_caller_module()
    if not strategy_module and 'user_code' in sys.modules:
        strategy_module = sys.modules['user_code']
        _dbg("refresh: 通过 sys.modules 获取策略模块")

    if strategy_module:
        _patch_orders_in_module(strategy_module, force=True)
        try:
            import jqdata
            for fn_name in ['order', 'order_value', 'order_target', 'order_target_value', 'order_target_percent']:
                if hasattr(jqdata, fn_name):
                    jq_func = getattr(jqdata, fn_name)
                    if getattr(jq_func, '_tools_wrapped', False):
                        setattr(strategy_module, fn_name, jq_func)
        except Exception as e:
            _dbg(f"强制同步异常: {e}")
    else:
        _dbg("策略模块未就绪，稍后 check_and_fix 会兜底")

    _log(f"策略 [{name}] 热更新恢复完成")


def _try_register_run_daily():
    try:
        uc = sys.modules.get('user_code')
        if uc and 'run_daily' in uc.__dict__:
            rd = uc.__dict__['run_daily']
        else:
            import inspect
            frame = inspect.currentframe()
            rd = None
            while frame:
                if 'run_daily' in frame.f_globals:
                    rd = frame.f_globals['run_daily']
                    break
                frame = frame.f_back
            if rd is None:
                raise NameError("run_daily not found")

        rd(_morning_refresh, time='09:30')
        rd(_afternoon_refresh, time='13:00')
        # 开盘前预热: 每个固定交易时刻前1分钟推送 preheat, 提前激活下单窗口
        if preheat_enabled:
            for t in PREHEAT_TIMES:
                try:
                    rd(_preheat, time=t)
                except Exception as e:
                    _dbg(f"预热任务注册失败 ({t}): {e}")
            _dbg(f"已注册 预热任务: {PREHEAT_TIMES}")
        _dbg("已注册 09:30/13:00 自动恢复任务")
    except Exception:
        _dbg(f"run_daily 跳过（非 initialize 阶段，由 check_and_fix 兜底）")


def check_and_fix(context):
    try:
        uc = sys.modules.get('user_code')
        uc_order = getattr(uc, 'order', None) if uc else None
        uc_ok = getattr(uc_order, '_tools_wrapped', False) if uc_order else False

        if not uc_ok:
            _log("🔄 wrapper 需要刷新 → 正在自动修复...")
            refresh(context)
            _log("✅ 信号通道已就绪")
    except Exception as e:
        _dbg(f"check_and_fix 异常: {e}")
