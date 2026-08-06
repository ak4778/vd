"""
国电电力 机组运行日报表 自动获取工具

功能：
  1. 获取指定日期的报表
  2. 批量获取连续 N 天的报表
  3. 解析报表中的关键指标（日发电量、平均风速、辐照强度等）

用法：
  python daily.py                      # 获取今天的报表
  python daily.py 2026-03-19           # 获取指定日期的报表
  python daily.py 2026-03-10 7         # 获取从 2026-03-10 开始连续 7 天的报表
"""
import requests
import json
import time
import re
import sys
import os
from datetime import datetime, timedelta
from urllib.parse import quote
from bs4 import BeautifulSoup

# ==================== 统计模块 ====================
class RequestStats:
    """请求统计器：记录成功、超时、错误等指标"""

    def __init__(self):
        self.total_requests = 0
        self.success = 0
        self.timeout = 0
        self.connection_error = 0
        self.session_expired = 0
        self.other_error = 0
        self.parse_error = 0
        self.total_time = 0.0
        # 按阶段分别统计
        self.stage_stats = {
            'form_getsource': {'success': 0, 'fail': 0, 'timeout': 0},
            'parameters_d': {'success': 0, 'fail': 0, 'timeout': 0},
            'read_w_content': {'success': 0, 'fail': 0, 'timeout': 0},
        }

    def record_success(self, stage=None, elapsed=0.0):
        self.success += 1
        self.total_requests += 1
        self.total_time += elapsed
        if stage and stage in self.stage_stats:
            self.stage_stats[stage]['success'] += 1

    def record_timeout(self, stage=None, elapsed=0.0):
        self.timeout += 1
        self.total_requests += 1
        self.total_time += elapsed
        if stage and stage in self.stage_stats:
            self.stage_stats[stage]['timeout'] += 1

    def record_connection_error(self, stage=None, elapsed=0.0):
        self.connection_error += 1
        self.total_requests += 1
        self.total_time += elapsed
        if stage and stage in self.stage_stats:
            self.stage_stats[stage]['fail'] += 1

    def record_session_expired(self, stage=None, elapsed=0.0):
        self.session_expired += 1
        self.total_requests += 1
        self.total_time += elapsed
        if stage and stage in self.stage_stats:
            self.stage_stats[stage]['fail'] += 1

    def record_other_error(self, stage=None, elapsed=0.0):
        self.other_error += 1
        self.total_requests += 1
        self.total_time += elapsed
        if stage and stage in self.stage_stats:
            self.stage_stats[stage]['fail'] += 1

    def record_parse_error(self):
        self.parse_error += 1

    def get_summary(self):
        elapsed = self.total_time
        avg = elapsed / self.total_requests if self.total_requests > 0 else 0
        success_rate = (self.success / self.total_requests * 100) if self.total_requests > 0 else 0
        return {
            'total': self.total_requests,
            'success': self.success,
            'timeout': self.timeout,
            'connection_error': self.connection_error,
            'session_expired': self.session_expired,
            'other_error': self.other_error,
            'parse_error': self.parse_error,
            'total_time': elapsed,
            'avg_time': avg,
            'success_rate': success_rate,
        }


# 全局统计实例
stats = RequestStats()


# ==================== 配置 ====================
BASE_URL = 'http://10.170.243.41'
REFERER = ('http://10.170.243.41/webroot/decision/view/report?viewlet='
           '%252Fenvision%252F%25E7%2594%259F%25E4%25BA%25A7%25E6%2597%25A5%25E6%258A%25A5%252F'
           '%25E6%259C%25BA%25E7%25BB%2584%25E8%25BF%2590%25E8%25A1%258C%25E6%2597%25A5%25E6%25B1%2587%25E6%2599%25BB%25E8%25A1%25A8_GD.cpt')
CONSTITUENT = '4094f09bb4b2443a926bad9840c42841'
CONSTITUENTNAME = '国电电力'
SESSION_ID = 'EAB035E6381D2295490A948BA6BE1C34'




# 请求超时（秒）
REQUEST_TIMEOUT = 60
# 失败重试次数（不含首次请求）
MAX_RETRIES = 3
# 重试间隔（秒）
RETRY_INTERVAL = 1.0
# SessionId 过期标志（响应中出现以下任一关键词即判定为过期）
SESSION_EXPIRED_MARKERS = [
    'login', '登录', '重新登录', 'session timeout', '会话过期',
    '请重新登录', '未登录', 'not logged in', 'authentication required',
]


# ==================== 自定义异常 ====================
class SessionIdExpiredError(Exception):
    """SessionId 过期或无效异常"""
    pass


class ReportFetchError(Exception):
    """报表获取失败异常（网络、解析等）"""
    pass


# ==================== 网络请求 ====================
def get_headers(content_type=False):
    """构建请求 headers"""
    h = {
        'Accept': 'application/json, text/plain, */*',
        'Host': '10.170.243.41',
        'Referer': REFERER,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'sessionID': SESSION_ID,
    }
    if content_type:
        h['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
    return h


def _check_session_expired(resp):
    """
    检测响应是否表示 SessionId 已过期/无效。
    判定条件：
      1. HTTP 状态码 401/403/440
      2. 响应文本包含登录页/会话过期关键词
    """
    if resp.status_code in (401, 403, 440):
        return True
    text = resp.text or ''
    lower_text = text.lower()
    for marker in SESSION_EXPIRED_MARKERS:
        if marker.lower() in lower_text:
            return True
    return False


def _safe_request(method, url, stage=None, **kwargs):
    """
    统一请求封装：
      - 设置超时
      - 捕获网络异常
      - 检测 SessionId 过期
      - 网络异常自动重试（最多 MAX_RETRIES 次）
      - 统计请求指标
    返回 response 对象
    抛出 SessionIdExpiredError / ReportFetchError
    注意：SessionId 过期不重试（重试必然再次过期）
    """
    kwargs.setdefault('timeout', REQUEST_TIMEOUT)
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        start = time.time()
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.Timeout:
            elapsed = time.time() - start
            stats.record_timeout(stage, elapsed)
            last_error = ReportFetchError(f"请求超时（>{REQUEST_TIMEOUT}s）: {url}")
            if attempt < MAX_RETRIES:
                print(f"  ⟳ 超时，第 {attempt+1}/{MAX_RETRIES} 次重试（等待 {RETRY_INTERVAL}s）...")
                time.sleep(RETRY_INTERVAL)
                continue
            raise last_error
        except requests.exceptions.ConnectionError as e:
            elapsed = time.time() - start
            stats.record_connection_error(stage, elapsed)
            last_error = ReportFetchError(f"网络连接失败: {e}")
            if attempt < MAX_RETRIES:
                print(f"  ⟳ 连接失败，第 {attempt+1}/{MAX_RETRIES} 次重试（等待 {RETRY_INTERVAL}s）...")
                time.sleep(RETRY_INTERVAL)
                continue
            raise last_error
        except requests.exceptions.RequestException as e:
            elapsed = time.time() - start
            stats.record_other_error(stage, elapsed)
            last_error = ReportFetchError(f"请求异常: {e}")
            if attempt < MAX_RETRIES:
                print(f"  ⟳ 请求异常，第 {attempt+1}/{MAX_RETRIES} 次重试（等待 {RETRY_INTERVAL}s）...")
                time.sleep(RETRY_INTERVAL)
                continue
            raise last_error

        elapsed = time.time() - start
        if _check_session_expired(resp):
            stats.record_session_expired(stage, elapsed)
            # SessionId 过期不重试，直接抛出
            raise SessionIdExpiredError(
                f"SessionId 已过期或无效（HTTP {resp.status_code}）。"
                f"请登录 http://10.170.243.41 后从浏览器复制新的 SessionId，"
                f"更新 daily.py 第 110 行 SESSION_ID 变量。"
            )
        stats.record_success(stage, elapsed)
        return resp

    # 理论上不会走到这里
    raise last_error if last_error else ReportFetchError("未知错误")


def calc_related_dates(date_str):
    """
    Step 1: 根据 DATE 参数获取相关日期（LASTDATE, LASTDAY 等）
    返回 dict: {"LASTDATE": ..., "LASTDAY": ..., "LASTMONTH": ..., "NEXTDATE": ..., "EXPORTDATE": ...}
    """
    params = {
        "DATE": date_str,
        "LASTDATE": "",
        "LASTDAY": "",
        "LASTMONTH": "",
        "NEXTDATE": "",
        "EXPORTDATE": "",
        "查询时间:": "查询时间:",
        "CONSTITUENTNAME": CONSTITUENTNAME,
        "CONSTITUENT": CONSTITUENT,
    }
    body = '__parameters__=' + quote(json.dumps(params, ensure_ascii=False))
    widget_list = quote('["LASTDATE","LASTDAY","LASTMONTH","NEXTDATE","EXPORTDATE"]')
    url = (f'{BASE_URL}/webroot/decision/view/report?_={int(time.time()*1000)}'
           f'&op=fr_form&cmd=form_getsource'
           f'&__widgetname__={widget_list}&__widgetvalue__={widget_list}')

    resp = _safe_request('POST', url, stage='form_getsource', headers=get_headers(content_type=True), data=body)
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            print(f"  ✗ 日期参数响应非 JSON: {resp.text[:100]}")
            return {}
    print(f"  ✗ 获取日期参数失败: {resp.status_code}")
    return {}


def set_report_params(date_str, date_calc):
    """
    Step 2: 设置报表参数（提交日期到服务器）
    """
    params = {
        "DATE": date_str,
        "LASTDATE": date_calc.get("LASTDATE", ""),
        "LASTDAY": date_calc.get("LASTDAY", ""),
        "LASTMONTH": date_calc.get("LASTMONTH", ""),
        "NEXTDATE": date_calc.get("NEXTDATE", ""),
        "EXPORTDATE": date_calc.get("EXPORTDATE", ""),
        "查询时间:": "查询时间:",
        "CONSTITUENTNAME": CONSTITUENTNAME,
        "CONSTITUENT": CONSTITUENT,
    }
    body = '__parameters__=' + quote(json.dumps(params, ensure_ascii=False)) + f'&_={int(time.time()*1000)}'
    url = f'{BASE_URL}/webroot/decision/view/report?op=fr_dialog&cmd=parameters_d'

    resp = _safe_request('POST', url, stage='parameters_d', headers=get_headers(content_type=True), data=body)
    if resp.status_code == 200 and 'success' in resp.text:
        return True
    print(f"  ✗ 设置参数失败: {resp.status_code} {resp.text[:100]}")
    return False


def fetch_report_content():
    """
    Step 3: 获取报表 HTML 内容
    """
    url = (f'{BASE_URL}/webroot/decision/view/report?_={int(time.time()*1000)}'
           f'&__boxModel__=true&op=fr_write&cmd=read_w_content&reportIndex=3'
           f'&browserWidth=1625&iid=0.5797018282470899&pn=1'
           f'&__webpage__=true&_paperWidth=1625&_paperHeight=1231&__fit__=false')

    resp = _safe_request('GET', url, stage='read_w_content', headers=get_headers())
    if resp.status_code == 200:
        try:
            return resp.json().get('html', '')
        except ValueError:
            print(f"  ✗ 报表响应非 JSON: {resp.text[:100]}")
            return ''
    print(f"  ✗ 获取报表失败: {resp.status_code}")
    return ''


# ==================== 报表解析 ====================
def get_report_date(html_content):
    """从报表 HTML 中提取日期（标题中的 YYYY年MM月DD日）"""
    match = re.search(r'(\d{4}年\d{2}月\d{2}日)机组运行日报表', html_content)
    return match.group(1) if match else ""


def get_row_by_name(rows, name):
    """通过行首名称定位数据行"""
    for row in rows:
        tds = row.find_all('td')
        if tds and tds[0].get_text(strip=True) == name:
            return [td.get_text(strip=True) for td in tds]
    return []


def get_value(values, idx):
    """安全获取指定索引的值"""
    return values[idx] if len(values) > idx else ""


def parse_report(html_content):
    """解析报表 HTML，提取关键指标及风/光发电量"""
    soup = BeautifulSoup(html_content, 'html.parser')
    tables = soup.find_all('table')

    if len(tables) < 5:
        return {"error": "报表结构异常"}

    rows = tables[4].find_all('tr')
    summary = get_row_by_name(rows, "国电电力")
    wind = get_row_by_name(rows, "风电")
    solar = get_row_by_name(rows, "光伏")

    total_gen = _safe_float(get_value(summary, 4))
    wind_gen = _safe_float(get_value(wind, 4))
    solar_gen = _safe_float(get_value(solar, 4))

    data = {
        "报表日期": get_report_date(html_content),
        "日发电量(MWh)": get_value(summary, 4),
        "风电日发电量(MWh)": get_value(wind, 4),
        "光伏日发电量(MWh)": get_value(solar, 4),
        "平均风速(m/s)": get_value(wind, 3),
        "辐照强度(W/m²)": get_value(solar, 3),
    }

    # 验证风电 + 光伏 = 总发电量
    if wind_gen is not None and solar_gen is not None and total_gen is not None:
        expected = round(wind_gen + solar_gen, 2)
        actual = round(total_gen, 2)
        data["发电量校验"] = "✓ 通过" if abs(expected - actual) < 0.05 else f"⚠ 不一致: 风电{wind_gen} + 光伏{solar_gen} = {expected}, 实际={actual}, 差值={round(expected - actual, 2)}"

    return data


def _safe_float(value):
    """安全转换为 float，失败返回 None"""
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ==================== 主流程 ====================
def fetch_single_report(date_str):
    """
    获取指定日期的报表（完整三步流程）
    date_str: "YYYY-MM-DD" 格式
    抛出：SessionIdExpiredError / ReportFetchError
    """
    print(f"\n→ 获取报表 (DATE={date_str})")

    # Step 1: 计算相关日期
    date_calc = calc_related_dates(date_str)
    if date_calc:
        print(f"  ✓ 相关日期: EXPORTDATE={date_calc.get('EXPORTDATE', '')}")

    # Step 2: 设置参数
    if not set_report_params(date_str, date_calc):
        return None

    # Step 3: 获取报表
    html = fetch_report_content()
    if not html:
        return None

    data = parse_report(html)
    if "error" in data:
        print(f"  ✗ 报表解析失败: {data['error']}")
        stats.record_parse_error()
        return None
    print(f"  ✓ 报表日期: {data.get('报表日期', '?')}")
    return data


def fetch_reports_for_date_range(start_date_str, days):
    """
    批量获取从 start_date 开始连续 days 天的报表
    start_date_str: "YYYY-MM-DD" 格式
    days: 天数
    遇 SessionIdExpiredError 立即终止并返回已获取数据
    """
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    today = datetime.now().date()
    all_reports = []

    # 计算实际可获取天数（不超过今天）
    max_end_date = start_date.date() + timedelta(days=days - 1)
    if max_end_date >= today:
        actual_days = (today - start_date.date()).days + 1
        if actual_days <= 0:
            print(f"\n✗ 起始日期 {start_date_str} 已超过今天（{today}），无可获取数据")
            return []
        print(f"\n⚠ 请求天数 {days} 超过当前日期，仅获取到今天（{today}），实际获取 {actual_days} 天")
        days = actual_days

    print(f"\n{'='*60}")
    print(f"批量获取 {days} 天报表（从 {start_date_str} 开始）")
    print(f"{'='*60}")

    for i in range(days):
        current_date = start_date + timedelta(days=i)
        date_str = current_date.strftime("%Y-%m-%d")
        # 双重保险：超过今天则停止
        if current_date.date() > today:
            print(f"\n⚠ 已到达今天（{today}），停止获取")
            break

        try:
            report = fetch_single_report(date_str)
        except SessionIdExpiredError as e:
            print(f"\n{'!'*60}")
            print(f"⚠ {e}")
            print(f"{'!'*60}")
            print(f"已终止批量获取，已成功获取 {len(all_reports)} 天数据。")
            break
        except ReportFetchError as e:
            print(f"  ✗ 跳过 {date_str}: {e}")
            continue

        if report:
            all_reports.append(report)
            wind_g = report.get('风电日发电量(MWh)', '?')
            solar_g = report.get('光伏日发电量(MWh)', '?')
            total_g = report.get('日发电量(MWh)', '?')
            check = report.get('发电量校验', '')
            print(f"  ★ 总发电={total_g} MWh, 风电={wind_g} MWh, 光伏={solar_g} MWh, "
                  f"风速={report.get('平均风速(m/s)', '?')} m/s, "
                  f"辐照={report.get('辐照强度(W/m²)', '?')} W/m²  {check}")

        # 避免请求过快
        if i < days - 1:
            time.sleep(0.5)

    return all_reports


def print_report(data):
    """打印单个报表"""
    print(f"\n{'='*60}")
    print(f"国电电力 机组运行日报 ({data.get('报表日期', '?')})")
    print(f"{'='*60}")
    for key, value in data.items():
        print(f"  {key}: {value}")

    print(f"\n{'='*60}")
    print(f"★ 日发电量:     {data.get('日发电量(MWh)', '?')} MWh")
    print(f"  风电:          {data.get('风电日发电量(MWh)', '?')} MWh")
    print(f"  光伏:          {data.get('光伏日发电量(MWh)', '?')} MWh")
    print(f"★ 平均风速:     {data.get('平均风速(m/s)', '?')} m/s")
    print(f"★ 辐照强度:     {data.get('辐照强度(W/m²)', '?')} W/m²")
    check = data.get('发电量校验', '')
    if check:
        print(f"★ 发电量校验:   {check}")
    print(f"{'='*60}")


def save_reports(reports, filename=None):
    """保存报表数据到 JSON 文件"""
    if not filename:
        filename = "day_rr.json"

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 已保存到 {filename} ({len(reports)} 条记录)")


# ==================== 命令行入口 ====================
def _handle_session_error(e):
    """统一处理 SessionId 过期异常"""
    print(f"\n{'!'*60}")
    print(f"⚠ {e}")
    print(f"{'!'*60}")
    print("操作步骤：")
    print("  1. 浏览器打开 http://10.170.243.41 并登录")
    print("  2. F12 → Network → 任意请求 → Headers → 找到 sessionID")
    print("  3. 复制 sessionID 值，更新 daily.py 第 110 行 SESSION_ID")
    print(f"{'!'*60}")
    sys.exit(1)


def _handle_fetch_error(e):
    """统一处理报表获取异常"""
    print(f"\n✗ 获取失败: {e}")
    sys.exit(2)


def print_stats():
    """打印请求统计报告"""
    s = stats.get_summary()
    print(f"\n{'#'*60}")
    print("请求统计报告")
    print(f"{'#'*60}")
    print(f"  请求总数:     {s['total']}")
    print(f"  成功:         {s['success']}")
    print(f"  超时:         {s['timeout']}")
    print(f"  连接失败:     {s['connection_error']}")
    print(f"  Session过期:  {s['session_expired']}")
    print(f"  其他错误:     {s['other_error']}")
    print(f"  解析错误:     {s['parse_error']}")
    print(f"  成功率:       {s['success_rate']:.1f}%")
    print(f"  总耗时:       {s['total_time']:.2f}s")
    print(f"  平均耗时:     {s['avg_time']:.2f}s")

    print(f"\n  --- 分阶段统计 ---")
    for stage, data in stats.stage_stats.items():
        total_s = data['success'] + data['fail'] + data['timeout']
        print(f"  [{stage:20s}] 成功={data['success']:2d}  失败={data['fail']:2d}  超时={data['timeout']:2d}  (共{total_s}次)")
    print(f"{'#'*60}")


def json_to_csv(json_file="day_rr.json", csv_file="day_rr.csv"):
    """将 day_rr.json 转换为 CSV 文件"""
    import csv as csv_module

    if not os.path.exists(json_file):
        print(f"✗ 文件不存在: {json_file}")
        return

    with open(json_file, 'r', encoding='utf-8') as f:
        reports = json.load(f)

    if not reports:
        print(f"✗ {json_file} 无数据")
        return

    # 统一字段顺序
    fieldnames = [
        "报表日期", "日发电量(MWh)", "风电日发电量(MWh)", "光伏日发电量(MWh)",
        "平均风速(m/s)", "辐照强度(W/m²)", "发电量校验"
    ]

    with open(csv_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv_module.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for r in reports:
            writer.writerow(r)

    print(f"✓ 已转换 {len(reports)} 条记录: {json_file} -> {csv_file}")
    print(f"  字段: {', '.join(fieldnames)}")


if __name__ == '__main__':
    args = sys.argv[1:]

    # 子命令：json2csv - 将 day_rr.json 转换为 CSV
    if args and args[0] in ('json2csv', 'tocsv'):
        json_to_csv()
        sys.exit(0)

    try:
        if len(args) == 0:
            # 默认获取今天
            date_str = datetime.now().strftime("%Y-%m-%d")
            report = fetch_single_report(date_str)
            if report:
                print_report(report)

        elif len(args) == 1:
            # 获取指定日期
            report = fetch_single_report(args[0])
            if report:
                print_report(report)

        elif len(args) == 2:
            # 批量获取：起始日期 + 天数
            reports = fetch_reports_for_date_range(args[0], int(args[1]))
            if reports:
                print(f"\n{'='*60}")
                print("汇总")
                print(f"{'='*60}")
                for r in reports:
                    print(f"  {r.get('报表日期', '?')}: 总发电={r.get('日发电量(MWh)', '?')} MWh, "
                          f"风电={r.get('风电日发电量(MWh)', '?')} MWh, "
                          f"光伏={r.get('光伏日发电量(MWh)', '?')} MWh, "
                          f"风速={r.get('平均风速(m/s)', '?')} m/s, "
                          f"辐照={r.get('辐照强度(W/m²)', '?')} W/m²  {r.get('发电量校验', '')}")
                # 校验异常汇总
                mismatches = [r for r in reports if '不一致' in str(r.get('发电量校验', ''))]
                if mismatches:
                    print(f"\n  ⚠ 发电量校验异常（{len(mismatches)} 天）:")
                    for r in mismatches:
                        print(f"    {r.get('报表日期', '?')}: {r.get('发电量校验', '')}")
                save_reports(reports)
            else:
                print("\n✗ 未能获取任何报表数据")

    except SessionIdExpiredError as e:
        _handle_session_error(e)
    except ReportFetchError as e:
        _handle_fetch_error(e)
    except ValueError as e:
        print(f"\n✗ 参数格式错误: {e}")
        print("用法: python daily.py [YYYY-MM-DD] [days]")
        sys.exit(3)
    finally:
        print_stats()
