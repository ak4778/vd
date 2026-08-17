#!/usr/bin/env python3
"""
密码哈希生成工具
用法:
  python gen_passhash.py                    # 交互式输入用户名和密码
  python gen_passhash.py 用户名 密码         # 命令行参数
  python gen_passhash.py --update           # 直接更新 data_config.json
"""
import hashlib, secrets, json, sys, getpass

ITERATIONS = 10000

def hash_password(password, salt_hex):
    """与 net.c 中 hash_password() 算法完全一致:
       h = SHA256(salt_hex || password), 迭代 ITERATIONS 次"""
    h = hashlib.sha256((salt_hex + password).encode()).digest()
    for _ in range(ITERATIONS - 1):
        h = hashlib.sha256(h).digest()
    return h.hex()

def gen_entry(name, password):
    salt = secrets.token_hex(16)
    passHash = hash_password(password, salt)
    return {"name": name, "passHash": passHash, "salt": salt}

def main():
    if len(sys.argv) >= 3:
        name = sys.argv[1]
        password = sys.argv[2]
    elif len(sys.argv) == 2 and sys.argv[1] == "--update":
        # 交互式更新 data_config.json
        name = input("用户名: ").strip()
        password = getpass.getpass("密码: ")
        entry = gen_entry(name, password)

        with open("data_config.json", "r", encoding="utf-8") as f:
            config = json.load(f)

        # 查找是否已存在该用户
        found = False
        for i, u in enumerate(config.get("users", [])):
            if u.get("name") == name:
                config["users"][i] = entry
                found = True
                break
        if not found:
            config.setdefault("users", []).append(entry)

        with open("data_config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

        print(f"\n已更新 data_config.json: 用户 [{name}]")
        print(f"  salt:     {entry['salt']}")
        print(f"  passHash: {entry['passHash']}")
        return
    else:
        print("=== 密码哈希生成工具 ===")
        name = input("用户名: ").strip()
        password = getpass.getpass("密码: ")

    entry = gen_entry(name, password)
    print(f"\n用户名:   {entry['name']}")
    print(f"salt:     {entry['salt']}")
    print(f"passHash: {entry['passHash']}")
    print(f'\nJSON 格式 (复制到 data_config.json):')
    print(f'  {{"name": "{entry["name"]}", "passHash": "{entry["passHash"]}", "salt": "{entry["salt"]}"}}')

if __name__ == "__main__":
    main()
