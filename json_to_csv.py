#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sys
import os
import csv

def json_to_csv(json_file, csv_file):
    """
    将 leaf_nodes.json 转换为 leaf_nodes.csv
    
    参数:
        json_file: JSON文件路径
        csv_file: CSV文件输出路径
    """
    # 字段顺序定义
    field_order = [
        'id', 'name', 'channelCode', 'pId', 'isParent', 'hasMoreNode',
        'cameraType', 'capability', 'channelSeq', 'channelType', 'checkStat',
        'deviceCategory', 'deviceCode', 'deviceModel', 'deviceType', 'domainId',
        'iconType', 'isCheck', 'isOnline', 'isVirtual', 'nodeType', 'ownerCode',
        'path', 'registServerCode', 'sort', 'stat', 'type', 'unitType',
        'operation', 'customOperation'
    ]
    
    try:
        # 读取JSON文件
        with open(json_file, 'r', encoding='utf-8') as f:
            print(f"正在读取 JSON 文件: {json_file}")
            data = json.load(f)
        
        nodes = data.get('nodes', [])
        total = data.get('total', len(nodes))
        
        print(f"共读取 {len(nodes)} 条数据 (total: {total})")
        
        # 写入CSV文件
        with open(csv_file, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            # 写入表头
            writer.writerow(field_order)
            
            # 写入数据行
            for i, node in enumerate(nodes):
                row = []
                for field in field_order:
                    value = node.get(field, '')
                    # 处理布尔值
                    if isinstance(value, bool):
                        value = 'True' if value else 'False'
                    # 处理数字
                    elif isinstance(value, (int, float)):
                        value = str(value)
                    row.append(str(value))
                writer.writerow(row)
                
                # 进度提示
                if (i + 1) % 5000 == 0:
                    print(f"已处理 {i + 1} / {len(nodes)} 条数据...")
        
        print(f"\nCSV 文件生成成功: {csv_file}")
        print(f"总行数: {len(nodes) + 1} (1行表头 + {len(nodes)}行数据)")
        
        return True
        
    except FileNotFoundError:
        print(f"错误: JSON文件不存在 - {json_file}")
        return False
    except json.JSONDecodeError as e:
        print(f"错误: JSON解析失败 - {e}")
        return False
    except Exception as e:
        print(f"错误: {e}")
        return False

def main():
    # 默认文件路径
    json_file = 'leaf_nodes.json'
    csv_file = 'leaf_nodes.csv'
    
    # 检查参数
    if len(sys.argv) >= 2:
        json_file = sys.argv[1]
    if len(sys.argv) >= 3:
        csv_file = sys.argv[2]
    
    # 检查JSON文件是否存在
    if not os.path.exists(json_file):
        print(f"错误: JSON文件不存在 - {json_file}")
        sys.exit(1)
    
    # 执行转换
    success = json_to_csv(json_file, csv_file)
    
    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
