import asyncio, json, os, random, string, threading, time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from dotenv import load_dotenv

from app.services.outlook_email_service import (
    OutlookEmailService,
    get_folder_highest_uids,
    wait_for_outlook_code,
)
from app.services.arting_client import claim_daily, get_balance

load_dotenv()
BASE = os.getenv('PIAX_API_URL', 'https://piax-api.piax.org')
_token_file_lock = asyncio.Lock()


def get_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def generate_password(length=12):
    return ''.join(random.choice(string.ascii_letters + string.digits + '_') for _ in range(length))


async def send_verify_code(email: str, client: httpx.AsyncClient):
    payload = {
        'channel': 'pia',
        'email': email,
        'device': {'deviceType': 'pc', 'osPlatform': 'web'}
    }
    r = await client.post(f'{BASE}/user-api/user/sendEmailVerifyCode', json=payload)
    r.raise_for_status()
    data = r.json()
    if data.get('code') != 1:
        raise RuntimeError(f"Piax sendEmailVerifyCode failed for {email}: {data}")
    return data


async def submit_email_login(email: str, code: str, client: httpx.AsyncClient) -> str:
    payload = {
        'email': email,
        'code': code,
        'channel': 'pia',
        'device': {'deviceType': 'pc', 'osPlatform': 'web'}
    }
    r = await client.post(f'{BASE}/user-api/user/emailLogin', json=payload)
    r.raise_for_status()
    data = r.json()
    if data.get('code') != 1 or not data.get('token'):
        raise RuntimeError(f"Piax emailLogin failed for {email}: {data}")
    return data['token']


async def register_account_from_record(email: str, password: str, client_id: str, refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Get baseline message IDs per folder ('"Junk"' and 'INBOX')
        baseline_folders = await asyncio.to_thread(get_folder_highest_uids, email, client_id, refresh_token)

        # 2. Request verification code from Piax
        await send_verify_code(email, client)

        # 3. Wait for code via Outlook IMAP OAuth
        code = await wait_for_outlook_code(email, client_id, refresh_token, baseline_folders=baseline_folders)

        # 4. Login to Piax and get JWT token
        token = await submit_email_login(email, code, client)

    # 5. Claim daily reward and get initial balance
    today_str = get_today_str()
    remaining = 0
    try:
        await claim_daily(token)
        bal_info = await get_balance(token)
        plan = bal_info.get('subscriptionPlan') or {}
        remaining = plan.get('subscriptionBalance', 0) + plan.get('totalityBalance', 0)
    except Exception as e:
        print(f"Warning: claim_daily or get_balance failed for new account {email}: {e}")

    return {
        "email": email,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "token": token,
        "state": "standby",
        "disabled": False,
        "remaining": remaining,
        "is_alive": True,
        "last_claim_date": today_str,
    }


async def relogin_account(account: dict) -> str:
    """When a token expires, reuse Outlook OAuth credentials to receive verify code and get a new JWT token."""
    email = account.get('email')
    client_id = account.get('client_id')
    refresh_token = account.get('refresh_token')
    if not email or not client_id or not refresh_token:
        raise ValueError(f"Account {email} lacks client_id or refresh_token for re-login")

    async with httpx.AsyncClient(timeout=30) as client:
        baseline_folders = await asyncio.to_thread(get_folder_highest_uids, email, client_id, refresh_token)
        await send_verify_code(email, client)
        code = await wait_for_outlook_code(email, client_id, refresh_token, baseline_folders=baseline_folders)
        new_token = await submit_email_login(email, code, client)

    account['token'] = new_token
    account['is_alive'] = True
    return new_token


async def register_batch_from_file(account_file: str = None, count: int = 5) -> list[dict]:
    path = account_file or os.getenv('OUTLOOK_ACCOUNT_FILE', 'outlook_live_20261018_222014.txt')
    token_path = Path(os.getenv('TOKEN_FILE', 'data/tokens.json'))

    async with _token_file_lock:
        existing = json.loads(token_path.read_text(encoding='utf-8')) if token_path.exists() else []
        existing_emails = {x.get('email') for x in existing if x.get('email')}

    candidates = []
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            parts = line.strip().split('----', 3)
            if len(parts) == 4 and parts[0] not in existing_emails:
                candidates.append(parts)
            if len(candidates) >= count:
                break

    if not candidates:
        print("No new candidate accounts to register.")
        return []

    registered = []
    for email, pwd, cid, rtok in candidates:
        try:
            print(f"Registering standby account: {email} ...", flush=True)
            acc = await register_account_from_record(email, pwd, cid, rtok)
            registered.append(acc)
            async with _token_file_lock:
                current_tokens = json.loads(token_path.read_text(encoding='utf-8')) if token_path.exists() else []
                acc['id'] = len(current_tokens) + 1
                current_tokens.append(acc)
                token_path.write_text(json.dumps(current_tokens, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"Successfully registered {email}, balance: {acc['remaining']}", flush=True)
        except Exception as e:
            print(f"Failed to register {email}: {e}", flush=True)
            if "too frequent" in str(e).lower() or "frequent" in str(e).lower():
                print("Upstream registration rate-limit detected. Stopping batch registration gracefully to cool down.", flush=True)
                break

    return registered


async def register_once(worker_id=1):
    service = OutlookEmailService()
    mailbox = await service.create_email()
    acc = await register_account_from_record(
        mailbox.address,
        mailbox.password,
        mailbox.client_id,
        mailbox.refresh_token,
    )
    token_path = Path(os.getenv('TOKEN_FILE', 'data/tokens.json'))
    async with _token_file_lock:
        existing = json.loads(token_path.read_text(encoding='utf-8')) if token_path.exists() else []
        acc['id'] = len(existing) + 1
        existing.append(acc)
        token_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
    return acc


def start_registration_tasks(task_account_count: int, infinite: bool = False, daemon: bool = True):
    def run():
        asyncio.run(register_batch_from_file(count=task_account_count))
    t = threading.Thread(target=run, daemon=daemon, name=f'PiaxReg-Task-{task_account_count}')
    t.start()
    return t
