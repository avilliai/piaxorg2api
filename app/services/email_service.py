"""
邮箱服务类
"""
import os
import requests
import random
import string
from dotenv import load_dotenv


class EmailService:
    """邮箱服务类"""

    def __init__(self):
        """初始化邮箱服务"""
        load_dotenv()

        self.worker_domain = os.getenv("WORKER_DOMAIN")
        self.email_domain = os.getenv("EMAIL_DOMAIN")
        self.admin_password = os.getenv("ADMIN_PASSWORD")

        if not all([self.worker_domain, self.email_domain, self.admin_password]):
            raise ValueError("Missing required environment variables: WORKER_DOMAIN, EMAIL_DOMAIN, ADMIN_PASSWORD")

    def _generate_random_name(self):
        """生成随机邮箱名称"""
        letters1 = ''.join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers = ''.join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = ''.join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def create_email(self):
        """
        创建临时邮箱
        """
        url = f"https://{self.worker_domain}/admin/new_address"
        try:
            random_name = self._generate_random_name()
            # print(f"[debug-email] 请求创建邮箱: {url}")
            res = requests.post(
                url,
                json={
                    "enablePrefix": True,
                    "name": random_name,
                    "domain": self.email_domain,
                },
                headers={
                    'x-admin-auth': self.admin_password,
                    "Content-Type": "application/json"
                },
                timeout=10 # 添加超时
            )
            if res.status_code == 200:
                data = res.json()
                return data.get('jwt'), data.get('address')
            else:
                print(f"[-] 创建邮箱接口返回错误: {res.status_code} - {res.text}")
                return None, None
        except Exception as e:
            print(f"[-] 创建邮箱网络异常 ({url}): {e}")
            return None, None

    def fetch_first_email(self, jwt):
        """
        获取邮件内容
        """
        try:
            limit = 10
            offset = 0
            res = requests.get(
                f"https://{self.worker_domain}/api/mails",
                params={
                    "limit": limit,
                    "offset": offset
                },
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json"
                }
            )

            if res.status_code == 200:
                data = res.json()
                if data["results"]:
                    raw_email_content = data["results"][0]["raw"]
                    return raw_email_content
                return None
            else:
                return None
        except Exception as e:
            print(f"获取邮件失败: {e}")
            return None
