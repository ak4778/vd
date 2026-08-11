import requests
import base64
import json
import csv
import time
import logging
from typing import Optional, Dict, List, Any, Tuple

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


class EVOClient:
    DEFAULT_BASE_URL = "https://10.65.78.161"
    DEFAULT_BASE_URL_WITH_PORT = "https://10.65.78.161:443"
    DEFAULT_API_VERSION = "1.0.0"
    DEFAULT_JSESSIONID = "625D9FE43A6A5CFE0F4C8A9A9B32806C"
    
    def __init__(
        self,
        username: str,
        password: str,
        client_id: str = "web_client",
        client_secret: str = "web_client",
        base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        jsessionid: Optional[str] = None,
        busi_type: int = 2,
        check_stat: int = 1,
        tree_type: str = ";;;"
    ):
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.base_url_with_port = base_url or self.DEFAULT_BASE_URL_WITH_PORT
        self.api_version = api_version or self.DEFAULT_API_VERSION
        self.jsessionid = jsessionid or self.DEFAULT_JSESSIONID
        self.busi_type = busi_type
        self.check_stat = check_stat
        self.tree_type = tree_type
        
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_in: Optional[int] = None
        self.token_obtained_at: Optional[float] = None
        
        self.session = requests.Session()
        self.session.verify = False
        self.session.timeout = 30
        
        self.final_leaf_nodes: List[Dict[str, Any]] = []
        self.error_logs: List[str] = []
        
        self.logger = logging.getLogger(__name__)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token:
            self.logout()
        return False
    
    def _encrypt_password(self, pubkey: str) -> str:
        if isinstance(self.password, str):
            password = self.password.encode()
        else:
            password = self.password
        
        pub_key = f"""-----BEGIN PUBLIC KEY-----
{pubkey.strip('"')}
-----END PUBLIC KEY-----"""
        
        key_obj = RSA.importKey(pub_key)
        cipher = PKCS1_v1_5.new(key_obj)
        web_safe_pwd = base64.b64encode(cipher.encrypt(password))
        return web_safe_pwd.decode()
    
    def _parse_json_response(self, response: requests.Response) -> Optional[Dict[str, Any]]:
        try:
            return response.json()
        except json.JSONDecodeError:
            error_msg = f"响应不是有效的JSON格式: {response.text[:100]}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return None
    
    def _check_response_success(self, data: Dict[str, Any], context: str = "") -> bool:
        if not data.get('success'):
            err_msg = data.get('errMsg', '')
            err_code = data.get('code', '')
            error_msg = f"{context}失败: 错误码={err_code}, 错误信息={err_msg}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return False
        return True
    
    def _get_data_value(self, data: Dict[str, Any], context: str = "") -> Optional[List[Dict[str, Any]]]:
        tree_result = data.get('data')
        if not isinstance(tree_result, dict):
            error_msg = f"{context} data类型不支持: {type(tree_result)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return None
        
        value_list = tree_result.get('value')
        if not isinstance(value_list, list):
            error_msg = f"{context} data.value不是列表类型: {type(value_list)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return None
        
        return value_list
    
    def _is_token_expired(self) -> bool:
        if self.token is None or self.token_obtained_at is None or self.expires_in is None:
            return True
        elapsed = time.time() - self.token_obtained_at
        return elapsed >= self.expires_in - 60
    
    def _ensure_valid_token(self) -> bool:
        if self._is_token_expired():
            if self.refresh_token:
                self.logger.info("Token即将过期，尝试刷新...")
                if self.refresh_token_func():
                    return True
                self.logger.info("刷新Token失败，尝试获取新Token...")
            if self.get_token():
                return True
            return False
        return True
    
    def get_public_key(self) -> Optional[str]:
        url = f"{self.base_url_with_port}/evo-apigw/evo-oauth/{self.api_version}/oauth/public-key"
        headers = {'Cookie': f'JSESSIONID={self.jsessionid}'}
        
        try:
            response = self.session.get(url, headers=headers)
            data = self._parse_json_response(response)
            if data is None:
                return None
            
            if not self._check_response_success(data, "获取公钥"):
                return None
            
            return data.get('data', {}).get('publicKey')
        except Exception as e:
            error_msg = f"获取公钥异常: {str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return None
    
    def get_token(self) -> bool:
        public_key = self.get_public_key()
        if not public_key:
            return False
        
        encrypted_password = self._encrypt_password(public_key)
        url = f"{self.base_url}/evo-apigw/evo-oauth/{self.api_version}/oauth/extend/token"
        payload = json.dumps({
            "grant_type": "password",
            "username": self.username,
            "password": encrypted_password,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "public_key": public_key
        })
        headers = {'Content-Type': 'application/json'}
        
        try:
            response = self.session.post(url, headers=headers, data=payload)
            token_data = self._parse_json_response(response)
            if token_data is None:
                return False
            
            if token_data.get('success'):
                data_obj = token_data.get('data', {})
                access_token = data_obj.get('access_token')
                if not access_token:
                    self.logger.error("获取Token失败: access_token为空")
                    return False
                
                self.refresh_token = data_obj.get('refresh_token')
                token_type = data_obj.get('token_type', 'Bearer')
                self.expires_in = data_obj.get('expires_in')
                self.token = f"{token_type} {access_token}"
                self.token_obtained_at = time.time()
                
                self.logger.info(f"获取Token成功: {self.token[:50]}...")
                return True
            else:
                err_msg = token_data.get('errMsg', '未知错误')
                error_msg = f"获取Token失败: {err_msg}"
                self.error_logs.append(error_msg)
                self.logger.error(error_msg)
                return False
        except Exception as e:
            error_msg = f"获取Token异常: {str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return False
    
    def refresh_token_func(self) -> bool:
        if not self.refresh_token:
            self.logger.error("刷新Token失败: refresh_token为空")
            return False
        
        url = f"{self.base_url}/evo-apigw/evo-oauth/{self.api_version}/oauth/extend/refresh/token"
        payload = json.dumps({
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token
        })
        headers = {'Content-Type': 'application/json'}
        
        try:
            response = self.session.post(url, headers=headers, data=payload)
            refresh_data = self._parse_json_response(response)
            if refresh_data is None:
                return False
            
            if refresh_data.get('success'):
                data_obj = refresh_data.get('data', {})
                access_token = data_obj.get('access_token')
                if not access_token:
                    self.logger.error("刷新Token失败: access_token为空")
                    return False
                
                token_type = data_obj.get('token_type', 'Bearer')
                self.refresh_token = data_obj.get('refresh_token', self.refresh_token)
                self.expires_in = data_obj.get('expires_in')
                self.token = f"{token_type} {access_token}"
                self.token_obtained_at = time.time()
                
                self.logger.info(f"刷新Token成功: {self.token[:50]}...")
                return True
            else:
                err_msg = refresh_data.get('errMsg', '未知错误')
                error_msg = f"刷新Token失败: {err_msg}"
                self.error_logs.append(error_msg)
                self.logger.error(error_msg)
                return False
        except Exception as e:
            error_msg = f"刷新Token异常: {str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return False
    
    def logout(self) -> bool:
        if not self.token:
            self.logger.info("无需登出: token为空")
            return True
        
        url = f"{self.base_url}/evo-apigw/evo-oauth/{self.api_version}/oauth/logout"
        headers = {
            'Authorization': self.token,
            'Content-Type': 'application/json',
        }
        
        try:
            response = self.session.get(url, headers=headers)
            logout_data = self._parse_json_response(response)
            if logout_data is None:
                self.token = None
                self.refresh_token = None
                self.expires_in = None
                self.token_obtained_at = None
                return False
            
            if logout_data.get('success'):
                self.token = None
                self.refresh_token = None
                self.expires_in = None
                self.token_obtained_at = None
                self.logger.info("logout成功")
                return True
            else:
                err_msg = logout_data.get('errMsg', '未知错误')
                err_code = logout_data.get('code', '')
                error_msg = f"登出失败: 错误码={err_code}, 错误信息={err_msg}"
                self.error_logs.append(error_msg)
                self.logger.error(error_msg)
                return False
        except Exception as e:
            error_msg = f"登出异常: {str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            self.token = None
            self.refresh_token = None
            self.expires_in = None
            self.token_obtained_at = None
            return False
    
    def get_tree_data(self, node_id: str) -> Optional[requests.Response]:
        if not self._ensure_valid_token():
            self.logger.error("获取树数据失败: 无法获取有效Token")
            return None
        
        url = f"{self.base_url}/evo-apigw/evo-brm/{self.api_version}/tree"
        payload = json.dumps({
            "id": node_id,
            "busiType": self.busi_type,
            "checkStat": self.check_stat,
            "type": self.tree_type
        })
        headers = {
            'Authorization': self.token,
            'Content-Type': 'application/json',
        }
        
        try:
            return self.session.post(url, headers=headers, data=payload)
        except Exception as e:
            error_msg = f"请求树数据失败: id={node_id}, 错误={str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return None
    
    def traverse_tree(self, node_id: str, level: int = 0, retry_count: int = 0, parent_names: List[str] = None) -> None:
        if parent_names is None:
            parent_names = []
        
        self.logger.info("  " * level + f"--- 调用 get_tree_data(id={node_id}) ---")
        response = self.get_tree_data(node_id)
        
        if response is None:
            return
        
        tree_data = self._parse_json_response(response)
        if tree_data is None:
            return
        
        if not tree_data.get('success'):
            err_msg = tree_data.get('errMsg', '')
            err_code = tree_data.get('code', '')
            error_msg = f"获取树数据失败: id={node_id}, 错误码={err_code}, 错误信息={err_msg}"
            self.error_logs.append(error_msg)
            self.logger.error("  " * level + f"错误: {error_msg}")
            
            if retry_count < 3 and (err_code == "27001007" or "token" in err_msg.lower() or "过期" in err_msg):
                self.logger.info("  " * level + "Token可能过期，尝试刷新Token...")
                if self.refresh_token_func():
                    self.logger.info("  " * level + "Token刷新成功，重新尝试请求...")
                    self.traverse_tree(node_id, level, retry_count + 1, parent_names)
                else:
                    self.logger.info("  " * level + "Token刷新失败，尝试获取新Token...")
                    if self.get_token():
                        self.logger.info("  " * level + "获取新Token成功，重新尝试请求...")
                        self.traverse_tree(node_id, level, retry_count + 1, parent_names)
            
            return
        
        value_list = self._get_data_value(tree_data, f"id={node_id}")
        if value_list is None:
            return
        
        for item in value_list:
            item_id = item.get('id')
            item_name = item.get('name')
            is_parent = item.get('isParent')
            has_more_node = item.get('hasMoreNode')
            
            if item_id and item_name:
                if is_parent or has_more_node:
                    self.logger.info("  " * level + f"节点ID: {item_id}, 名称: {item_name} (非叶子节点，继续遍历)")
                    if item_id.startswith("L03"):
                        new_parent_names = parent_names + [item_name]
                        self.traverse_tree(item_id, level + 1, 0, new_parent_names)
                else:
                    if 'channelCode' in item:
                        leaf_node = dict(item)
                        for i, name in enumerate(parent_names, 1):
                            leaf_node[f'P{i}'] = name
                        self.logger.info("  " * level + f"最终叶子节点(含父路径): {json.dumps(leaf_node, ensure_ascii=False)}")
                        self.final_leaf_nodes.append(leaf_node)
    
    def collect_channel_devices(self) -> int:
        self.final_leaf_nodes = []
        
        if not self.get_token():
            self.logger.error("无法获取Token，程序退出")
            return 0
        
        response = self.get_tree_data("L03")
        if response is None:
            return 0
        
        tree_data = self._parse_json_response(response)
        if tree_data is None:
            return 0
        
        if not self._check_response_success(tree_data, "获取L03树数据"):
            return 0
        
        value_list = self._get_data_value(tree_data, "L03")
        if value_list is None:
            return 0
        
        target_names = ["新能源场站", "火电厂站", "水电厂站"]
        top_level_nodes = [
            item for item in value_list
            if item.get('id') and item.get('name') and len(item.get('id')) == 6 and item.get('name') in target_names
        ]
        
        self.logger.info(f"筛选后的顶层节点: {[(item['id'], item['name']) for item in top_level_nodes]}")
        
        all_leaf_nodes = []
        
        for top_node in top_level_nodes:
            top_id = top_node['id']
            top_name = top_node['name']
            p1_names = [top_name]
            
            self.logger.info(f"\n--- 处理顶层节点: {top_id} ({top_name}) ---")
            
            if not top_id.startswith("L03"):
                continue
            
            sub_response = self.get_tree_data(top_id)
            if sub_response is None:
                continue
            
            sub_tree_data = self._parse_json_response(sub_response)
            if sub_tree_data is None:
                continue
            
            self.logger.info(f"子节点响应: {json.dumps(sub_tree_data, ensure_ascii=False)[:500]}...")
            
            if not sub_tree_data.get('success'):
                self._check_response_success(sub_tree_data, f"获取子节点树数据(id={top_id})")
                continue
            
            sub_value_list = self._get_data_value(sub_tree_data, f"id={top_id}")
            if sub_value_list is None:
                continue
            
            sub_nodes = [
                item for item in sub_value_list
                if item.get('id') and item.get('name') and len(item.get('id')) == 9 and "工程期" not in item.get('name', '')
            ]
            self.logger.info(f"第二层节点: {[(item['id'], item['name']) for item in sub_nodes]}")
            
            for sub_node in sub_nodes:
                sub_id = sub_node['id']
                sub_name = sub_node['name']
                p2_names = p1_names + [sub_name]
                
                self.logger.info(f"\n--- 处理第二层节点: {sub_id} ({sub_name}) ---")
                
                leaf_response = self.get_tree_data(sub_id)
                if leaf_response is None:
                    continue
                
                leaf_tree_data = self._parse_json_response(leaf_response)
                if leaf_tree_data is None:
                    continue
                
                self.logger.info(f"叶子节点响应: {json.dumps(leaf_tree_data, ensure_ascii=False)[:500]}...")
                
                if not leaf_tree_data.get('success'):
                    self._check_response_success(leaf_tree_data, f"获取叶子节点树数据(id={sub_id})")
                    continue
                
                leaf_value_list = self._get_data_value(leaf_tree_data, f"id={sub_id}")
                if leaf_value_list is None:
                    continue
                
                for item in leaf_value_list:
                    item_id = item.get('id')
                    item_name = item.get('name')
                    
                    if not item_id or not item_name:
                        continue
                    
                    if "工作记录仪" in item_name:
                        self.logger.info(f"跳过工作记录仪: {item_id}, {item_name}")
                        continue
                    
                    if len(item_id) == 12:
                        p3_names = p2_names + [item_name]
                        all_leaf_nodes.append({
                            "id": item_id, 
                            "name": item_name,
                            "parent_names": p3_names
                        })
                        self.logger.info(f"P3层节点: {item_id}, 名称: {item_name}, 父路径(P1,P2,P3): {' > '.join(p3_names)}")
        
        self.logger.info(f"\n共收集到 {len(all_leaf_nodes)} 个中间叶子节点")
        
        for node in all_leaf_nodes:
            self.traverse_tree(node['id'], parent_names=node['parent_names'])
        
        return len(all_leaf_nodes)
    
    def export_to_json(self, filename: str = "leaf_nodes.json") -> bool:
        output_data = {
            "total": len(self.final_leaf_nodes),
            "nodes": self.final_leaf_nodes
        }
        
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)
            self.logger.info(f"JSON文件已保存: {filename}")
            return True
        except IOError as e:
            error_msg = f"保存JSON文件失败: {str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return False
    
    def find_fields_with_commas(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.final_leaf_nodes:
            self.logger.warning("无数据可检查")
            return {}
        
        fields_with_commas: Dict[str, List[Dict[str, Any]]] = {}
        
        for node in self.final_leaf_nodes:
            node_id = node.get('id', '未知')
            for key, value in node.items():
                if value is None:
                    continue
                str_val = str(value)
                if ',' in str_val:
                    if key not in fields_with_commas:
                        fields_with_commas[key] = []
                    if len(fields_with_commas[key]) < 10:
                        fields_with_commas[key].append({
                            'id': node_id,
                            'value': str_val
                        })
        
        self.logger.info("\n" + "="*60)
        self.logger.info("检测包含CSV分隔符(,)的字段")
        self.logger.info("="*60)
        
        if not fields_with_commas:
            self.logger.info("未发现包含逗号的字段值")
        else:
            total_affected = 0
            for field_name, examples in fields_with_commas.items():
                count = sum(1 for n in self.final_leaf_nodes
                          if n.get(field_name) is not None and ',' in str(n.get(field_name)))
                total_affected += count
                self.logger.info(f"\n字段: '{field_name}' - 共 {count} 条记录包含逗号")
                self.logger.info(f"  示例（前{len(examples)}条）:")
                for ex in examples:
                    self.logger.info(f"    ID: {ex['id']}, 值: {repr(ex['value'])}")
            
            self.logger.info(f"\n总计: {len(fields_with_commas)} 个字段存在逗号问题，共影响 {total_affected} 条记录")
        
        return fields_with_commas

    def export_to_csv(self, filename: str = "leaf_nodes.csv") -> bool:
        if not self.final_leaf_nodes:
            self.logger.warning("无数据可保存")
            return False
        
        self.find_fields_with_commas()
        
        # Collect all P fields and decide which to keep
        all_p_fields = set()
        for node in self.final_leaf_nodes:
            for k in node.keys():
                if k.startswith('P') and k[1:].isdigit():
                    all_p_fields.add(k)
        
        keep_p = {'P1', 'P4'}
        drop_p = all_p_fields - keep_p
        
        all_keys = set()
        for node in self.final_leaf_nodes:
            all_keys.update(node.keys())
        
        priority_fields = ['id', 'name', 'channelCode', 'pId', 'isParent', 'hasMoreNode',
                         'P1', 'P4']
        fieldnames = [k for k in priority_fields if k in all_keys]
        fieldnames += sorted([k for k in all_keys if k not in priority_fields and k not in drop_p])
        
        self.logger.info(f"CSV导出: 保留P字段={keep_p}, 丢弃P字段={drop_p if drop_p else '无'}")
        
        def flatten_value(val):
            if isinstance(val, (dict, list)):
                return json.dumps(val, ensure_ascii=False)
            return val
        
        try:
            with open(filename, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for node in self.final_leaf_nodes:
                    flattened_node = {k: flatten_value(v) for k, v in node.items() if k not in drop_p}
                    writer.writerow(flattened_node)
            self.logger.info(f"CSV文件已保存: {filename}")
            return True
        except IOError as e:
            error_msg = f"保存CSV文件失败: {str(e)}"
            self.error_logs.append(error_msg)
            self.logger.error(error_msg)
            return False
    
    def print_summary(self, start_time: float, intermediate_count: int = 0) -> None:
        self.logger.info("\n" + "="*60)
        self.logger.info("运行统计")
        self.logger.info("="*60)
        
        end_time = time.time()
        total_time = end_time - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        milliseconds = int((total_time % 1) * 1000)
        
        self.logger.info(f"总运行时间: {minutes}分{seconds}秒{milliseconds}毫秒 ({total_time:.2f}秒)")
        self.logger.info(f"收集到的中间叶子节点数: {intermediate_count}")
        self.logger.info(f"收集到的最终叶子节点数: {len(self.final_leaf_nodes)}")
        
        if self.error_logs:
            self.logger.info("\n" + "="*60)
            self.logger.info("错误日志汇总")
            self.logger.info("="*60)
            self.logger.info(f"共发生 {len(self.error_logs)} 个错误:")
            for i, err in enumerate(self.error_logs, 1):
                self.logger.info(f"{i}. {err}")
        else:
            self.logger.info("\n无错误发生")
    
    def find_duplicate_channelCodes(self) -> Dict[str, List[Dict[str, Any]]]:
        from collections import defaultdict
        
        code_to_nodes = defaultdict(list)
        for node in self.final_leaf_nodes:
            if 'channelCode' in node:
                code_to_nodes[node['channelCode']].append(node)
        
        duplicates = {k: v for k, v in code_to_nodes.items() if len(v) > 1}
        return duplicates
    
    def save_duplicate_channelCodes(self, filename: str = "dup_cc") -> bool:
        duplicates = self.find_duplicate_channelCodes()
        
        if not duplicates:
            self.logger.info("\n无重复的channelCode")
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write("无重复的channelCode\n")
                self.logger.info(f"重复统计文件已保存: {filename}")
                return True
            except IOError as e:
                self.logger.error(f"保存重复统计文件失败: {str(e)}")
                return False
        
        self.logger.info(f"\n" + "="*60)
        self.logger.info("重复channelCode统计")
        self.logger.info("="*60)
        self.logger.info(f"共找到 {len(duplicates)} 个重复的channelCode")
        
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"重复channelCode统计\n")
                f.write(f"="*60 + "\n")
                f.write(f"总记录数: {len(self.final_leaf_nodes)}\n")
                f.write(f"有channelCode的记录数: {len([n for n in self.final_leaf_nodes if 'channelCode' in n])}\n")
                f.write(f"重复的channelCode数量: {len(duplicates)}\n")
                f.write(f"重复记录总数: {sum(len(v) for v in duplicates.values())}\n")
                f.write(f"\n" + "="*60 + "\n")
                
                for code, node_list in sorted(duplicates.items()):
                    f.write(f"\nchannelCode: {code}\n")
                    f.write(f"重复次数: {len(node_list)}\n")
                    f.write("-"*40 + "\n")
                    for i, node in enumerate(node_list, 1):
                        f.write(f"  记录{i}:\n")
                        f.write(f"    id: {node.get('id', '')}\n")
                        f.write(f"    name: {node.get('name', '')}\n")
                        f.write(f"    pId: {node.get('pId', '')}\n")
                        f.write(f"    cameraType: {node.get('cameraType', '')}\n")
                    f.write("\n")
                
                f.write("="*60 + "\n")
            
            self.logger.info(f"重复统计文件已保存: {filename}")
            return True
        except IOError as e:
            self.logger.error(f"保存重复统计文件失败: {str(e)}")
            return False


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )


if __name__ == "__main__":
    setup_logging()
    
    start_time = time.time()

    #host = "平台IP";
    #"Test"
    #"190dd326-eba0-44a1-8ad4-35152ddb5a89"

    
    with EVOClient(
        username="e0232091",
        password="Atos.202102",
        client_id="web_client",
        client_secret="web_client"
    ) as client:
        client.logger.info("="*60)
        client.logger.info("开始采集新能源场站设备数据")
        client.logger.info("="*60)
        
        intermediate_count = client.collect_channel_devices()
        if intermediate_count > 0:
            client.logger.info(f"\n共收集到 {len(client.final_leaf_nodes)} 个最终叶子节点")
            
            client.export_to_json()
            client.export_to_csv()
        
        client.print_summary(start_time, intermediate_count)
        client.save_duplicate_channelCodes()
