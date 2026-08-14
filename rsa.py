#下面有两种RSA加密的代码，若python版本3.x的选择第一种，若python版本为2.x的选择第二种。
第一种：
#import base64

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

def get_encrypt_password(pubkey, password):
    if isinstance(password, str):
        password = password.encode()
    pub_key = """-----BEGIN PUBLIC KEY-----
        """ + pubkey.strip('"') + """
        -----END PUBLIC KEY-----"""
    key_obj = RSA.importKey(pub_key)
    cipher = PKCS1_v1_5.new(key_obj)
    web_safe_pwd = base64.b64encode(cipher.encrypt(password))
    return web_safe_pwd.decode()



#第二种：
#import json
#import requests
#import base64
#import urllib3
#import M2Crypto
#
#urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
#
#def get_public_key(host):
#    url = f'https://{host}/evo-apigw/evo-oauth/1.0.0/oauth/public-key'
#    resp = requests.get(url=url, verify=False)
#    data = json.loads(resp.content)['data']
#    public_key = data['publicKey']
#    return public_key
#
#
#def make_pem_from_string(public_key):
#    header = '-----BEGIN PUBLIC KEY-----'
#    content = '\n'.join([public_key[i:i+64] for i in range(0, len(public_key), 64)])
#    footer = '-----END PUBLIC KEY-----'
#    x509 = '\n'.join([header, content, footer])
#    return x509
#
#
#def public_encrypt(public_key, password):
#    x509 = make_pem_from_string(public_key)
#    bio = M2Crypto.BIO.MemoryBuffer(x509.encode('utf-8'))
#    rsa_pub = M2Crypto.RSA.load_pub_key_bio(bio)
#    ctxt_pub = rsa_pub.public_encrypt(password.encode(), M2Crypto.RSA.pkcs1_padding)
#    ctxt64_pub = base64.b64encode(ctxt_pub).decode()
#    return ctxt64_pub
#
#
#def get_token_by_password(host, username, password, client_id, client_secret, public_key):
#    url = f'https://{host}/evo-apigw/evo-oauth/1.0.0/oauth/extend/token/'
#    data = {
#        "grant_type": "password",
#        "username": f"{username}",
#        "password": f"{password}",
#        "client_id": f"{client_id}",
#        "client_secret": f"{client_secret}",
#        "public_key": f"{public_key}"
#    }
#    resp = requests.post(url=url, json=data, verify=False)
#    data = json.loads(resp.content)['data']
#    access_token = data['access_token']
#    refresh_token = data['refresh_token']
#    return access_token, refresh_token
