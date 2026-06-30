import pandas as pd


def calculate_macd(data, fast_period=12, slow_period=26, signal_period=9):
    # 计算MACD指标
    data.loc[:, 'EMA_fast'] = data['close'].ewm(span=fast_period, adjust=False).mean()
    data.loc[:, 'EMA_slow'] = data['close'].ewm(span=slow_period, adjust=False).mean()
    data.loc[:, 'MACD'] = data['EMA_fast'] - data['EMA_slow']
    data.loc[:, 'MACD_signal'] = data['MACD'].ewm(span=signal_period, adjust=False).mean()
    data.loc[:, 'MACD_hist'] = data['MACD'] - data['MACD_signal']
    return data


def filter_macd_values(data, direction):
    if direction == 'up':
        # 返回0轴上方的MACD值
        return data[data['MACD'] > 0]['MACD'].values
    elif direction == 'down':
        # 返回0轴下方的MACD值
        return data[data['MACD'] < 0]['MACD'].values
    else:
        raise ValueError("方向必须是 'up' 或 'down'")

def find_crosses(data):
    # 找到金叉（MACD上穿）和死叉（MACD下穿）
    data['prev_MACD'] = data['MACD'].shift(1)
    data['prev_MACD_signal'] = data['MACD_signal'].shift(1)

    # 金叉：从MACD < MACD_signal 变为 MACD > MACD_signal
    data['golden_cross'] = (data['prev_MACD'] < data['prev_MACD_signal']) & (data['MACD'] > data['MACD_signal'])

    # 死叉：从MACD > MACD_signal 变为 MACD < MACD_signal
    data['death_cross'] = (data['prev_MACD'] > data['prev_MACD_signal']) & (data['MACD'] < data['MACD_signal'])

    return data

def find_max_cross(csvdata, start_time, end_time, is_up):
    # 读取CSV文件，并解析时间为datetime格式
    # data = pd.read_csv(csv_path, parse_dates=['timestamp'])

    # 根据时间区间筛选数据
    data = csvdata[(csvdata['timestamp'] >= start_time) & (csvdata['timestamp'] <= end_time)].copy()

    # 计算MACD
    data = calculate_macd(data)

    # 查找金叉和死叉
    data = find_crosses(data)

    if is_up:
        # 如果是上升趋势，找死叉（MACD 下穿信号线）
        death_crosses = data[data['death_cross']]
        if not death_crosses.empty:
            max_death_cross = death_crosses['MACD'].abs().max()  # 找最大绝对值的死叉
            return max_death_cross, 'death_cross'
        else:
            return None, 'no_death_cross'
    else:
        # 如果是下降趋势，找金叉（MACD 上穿信号线）
        golden_crosses = data[data['golden_cross']]
        if not golden_crosses.empty:
            max_golden_cross = golden_crosses['MACD'].abs().max()  # 找最大绝对值的金叉
            return max_golden_cross, 'golden_cross'
        else:
            return None, 'no_golden_cross'
def read_and_calculate_macd(csvdata, start_time, end_time, direction):
    # 读取CSV文件，并解析时间为datetime格式



    # 根据时间区间筛选数据
    data = csvdata[(csvdata['timestamp'] >= start_time) & (csvdata['timestamp'] <= end_time)].copy()

    # 计算MACD
    data = calculate_macd(data)

    # 根据方向过滤MACD值
    macd_values = filter_macd_values(data, direction)



    return macd_values,data.iloc[-1]['MACD'],data.iloc[-1]['MACD_signal']


if __name__ == '__main__':

    # 示例使用
    csv_path = 'your_data.csv'  # 替换为实际的CSV文件路径
    start_time = '2024-10-01 00:00:00'
    end_time = '2024-10-01 23:59:59'
    direction = 'up'  # 选择方向 'up' 或 'down'

    # 调用函数
    macd_values = read_and_calculate_macd(csv_path, start_time, end_time, direction)
    print(f"Filtered MACD values: {macd_values}")
