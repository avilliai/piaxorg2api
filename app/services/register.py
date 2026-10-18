import asyncio, json, os, random, string, threading, time
from pathlib import Path
import httpx
from dotenv import load_dotenv
from app.services.email_service import EmailService
from app.services.outlook_email_service import OutlookEmailService

load_dotenv()
BASE = os.getenv('PIAX_API_URL', 'https://piax-api.piax.org')
_token_file_lock = asyncio.Lock()

def generate_password(length=12):
    return ''.join(random.choice(string.ascii_letters + string.digits + '_') for _ in range(length))

def extract_verification_code(text):
    import re
    text = text or ''
    # Piax's mail template labels the code explicitly.  Prefer that match so
    # dates, message ids and footer numbers cannot win over the actual code.
    matches = re.findall(r'(?:verification\s*code|验证码)\s*(?:(?:is|为|是)\s*)?[:：]?\s*([0-9]{6})', text, re.I)
    if matches: return matches[-1]
    codes = re.findall(r'\b([0-9]{6})\b', text)
    return codes[-1] if codes else None

async def wait_for_code(service, jwt, timeout=100, interval=5, baseline=""):
    loop = asyncio.get_running_loop()
    for _ in range(max(1, timeout // interval)):
        content = await loop.run_in_executor(None, service.fetch_first_email, jwt)
        # Ignore messages present before this verification request.
        if content and content != baseline:
            code = extract_verification_code(content)
            if code: return code
        await asyncio.sleep(interval)
    raise TimeoutError('verification code timeout')

async def register_once(worker_id=1):
    service = OutlookEmailService() if os.getenv('EMAIL_MODE').lower() == 'outlook' else EmailService()
    mailbox = await service.create_email() if hasattr(service, 'wait_for_code') else None
    if mailbox:
        email = mailbox.address; jwt = mailbox
    else:
        jwt, email = service.create_email()
    outlook_baseline = 0
    if mailbox:
        outlook_baseline = await service.latest_uid(jwt)
    baseline = ""
    if not mailbox:
        baseline = service.fetch_first_email(jwt) or ""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f'{BASE}/user-api/user/sendEmailVerifyCode', json={
            'channel':'pia','email':email,'device':{'deviceType':'pc','osPlatform':'web'}})
        r.raise_for_status()
        code = await service.wait_for_code(jwt, outlook_baseline) if mailbox else await wait_for_code(service, jwt, int(os.getenv('POLL_TIMEOUT','100')), int(os.getenv('POLL_INTERVAL','5')), baseline)
        r = await client.post(f'{BASE}/user-api/user/emailLogin', json={
            'email':email,'code':code,'channel':'pia','device':{'deviceType':'pc','osPlatform':'web'}})
        r.raise_for_status(); data = r.json()
    token = data.get('token')
    if not token: raise RuntimeError(f'Piax login returned no token: {data}')
    entry = {'email': email, 'password': '', 'token': token}
    path = Path(os.getenv('TOKEN_FILE','data/tokens.json')); path.parent.mkdir(parents=True, exist_ok=True)
    async with _token_file_lock:
        existing = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        existing.append(entry)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
    return entry

def start_registration_tasks(task_account_count:int, infinite:bool=False, daemon:bool=True):
    def run():
        async def batch():
            concurrency=max(1,int(os.getenv('THREADS','2'))); completed=0
            while completed < task_account_count:
                size=min(concurrency,task_account_count-completed)
                results=await asyncio.gather(*(register_once(completed+i+1) for i in range(size)),return_exceptions=True)
                ok=sum(not isinstance(x,Exception) for x in results); completed += ok
                print(f'完成: 成功 {completed} / 目标 {task_account_count} / 本轮失败 {size-ok}')
                for x in results:
                    if isinstance(x,Exception): print(f'注册失败: {x}')
                if completed < task_account_count:
                    await asyncio.sleep(float(os.getenv('REGISTER_RETRY_DELAY', '5')) if ok == 0 else 0)
        asyncio.run(batch())
    t = threading.Thread(target=run, daemon=daemon, name=f'PiaxReg-Task-{task_account_count}'); t.start(); return t
