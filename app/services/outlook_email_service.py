import asyncio, imaplib, os, re, requests, quopri, time
from dataclasses import dataclass

@dataclass
class OutlookInbox:
    address: str
    index: int
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""

def get_outlook_access_token(client_id: str, refresh_token: str, retries: int = 4) -> str:
    url = os.getenv('OUTLOOK_TOKEN_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/token')
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': refresh_token},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if 'access_token' in data:
                return data['access_token']
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f'Failed to obtain access token from Microsoft OAuth: {last_exc}')

def get_folder_highest_uids(email: str, client_id: str, refresh_token: str, access_token: str = None) -> dict[str, int]:
    """Returns mapping of folder name to highest message ID before sending code, e.g. {'\"Junk\"': 5, 'INBOX': 19}."""
    result = {'"Junk"': 0, 'INBOX': 0}
    try:
        token = access_token or get_outlook_access_token(client_id, refresh_token)
    except Exception:
        return result

    hosts = [h.strip() for h in os.getenv('OUTLOOK_IMAP_HOSTS', 'outlook.office365.com;imap-mail.outlook.com').replace(',', ';').split(';') if h.strip()]
    port = int(os.getenv('OUTLOOK_IMAP_PORT', '993'))
    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=30)
            auth = f'user={email}\x01auth=Bearer {token}\x01\x01'.encode()
            typ, _ = c.authenticate('XOAUTH2', lambda _: auth)
            if typ != 'OK':
                continue
            for folder in ['"Junk"', 'INBOX']:
                try:
                    typ, _ = c.select(folder, readonly=True)
                    if typ == 'OK':
                        typ, ids = c.search(None, 'ALL')
                        if typ == 'OK' and ids[0].split():
                            result[folder] = int(ids[0].split()[-1])
                except Exception:
                    pass
            return result
        except Exception:
            pass
        finally:
            if c:
                try: c.logout()
                except Exception: pass
    return result

def check_folder_for_code(c: imaplib.IMAP4_SSL, folder: str, after_id: int = 0) -> str | None:
    try:
        typ, _ = c.select(folder, readonly=True)
        if typ != 'OK':
            return None
        typ, ids = c.search(None, 'ALL')
        if typ != 'OK' or not ids[0].split():
            return None
        all_ids = [int(mid) for mid in ids[0].split()]
        candidates = [mid for mid in all_ids if mid > after_id]
        # If no messages strictly > after_id, also inspect the latest message if after_id was 0
        if not candidates and after_id == 0:
            candidates = all_ids[-1:]

        for mid in candidates[::-1][:3]:
            typ, data = c.fetch(str(mid).encode(), '(RFC822)')
            if typ != 'OK':
                continue
            raw_bytes = b''.join(x[1] for x in data if isinstance(x, tuple))
            try:
                decoded = quopri.decodestring(raw_bytes).decode('utf-8', 'ignore')
            except Exception:
                decoded = raw_bytes.decode('utf-8', 'ignore')

            if not re.search(r'PIAX', decoded, re.I):
                continue
            m = re.search(r'verification\s+code\s+is[\s\S]{0,200}?([0-9]{6})', decoded, re.I)
            if not m:
                m = re.search(r'<strong[^>]*>([0-9]{6})</strong>', decoded, re.I)
            if not m:
                m = re.search(r'Your\s+verification\s+code\s+is\s*:\s*([0-9]{6})', decoded, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def read_outlook_verification_code_with_token(email: str, access_token: str, baseline_folders: dict[str, int] = None) -> str | None:
    baseline = baseline_folders or {'"Junk"': 0, 'INBOX': 0}
    hosts = [h.strip() for h in os.getenv('OUTLOOK_IMAP_HOSTS', 'outlook.office365.com;imap-mail.outlook.com').replace(',', ';').split(';') if h.strip()]
    port = int(os.getenv('OUTLOOK_IMAP_PORT', '993'))
    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=30)
            auth = f'user={email}\x01auth=Bearer {access_token}\x01\x01'.encode()
            typ, _ = c.authenticate('XOAUTH2', lambda _: auth)
            if typ != 'OK':
                continue
            # PIAX verification emails arrive primarily in Junk
            for folder in ['"Junk"', 'INBOX']:
                code = check_folder_for_code(c, folder, baseline.get(folder, 0))
                if code:
                    return code
            return None
        except Exception:
            pass
        finally:
            if c:
                try: c.logout()
                except Exception: pass
    return None

async def wait_for_outlook_code(email: str, client_id: str, refresh_token: str, baseline_folders: dict[str, int] = None, timeout: int = 120, interval: int = 5) -> str:
    # 1. Fetch access token ONCE for this polling session (valid for 1 hour)
    access_token = await asyncio.to_thread(get_outlook_access_token, client_id, refresh_token)

    for _ in range(max(1, timeout // interval)):
        await asyncio.sleep(interval)
        try:
            code = await asyncio.to_thread(read_outlook_verification_code_with_token, email, access_token, baseline_folders)
            if code:
                return code
        except Exception:
            pass
    raise TimeoutError(f'Outlook verification code timeout: {email}')

class OutlookEmailService:
    def __init__(self, account_file: str = None):
        path = account_file or os.getenv('OUTLOOK_ACCOUNT_FILE', 'outlook_live_20261018_222014.txt')
        self.accounts = []
        if os.path.exists(path):
            with open(path, encoding='utf-8-sig') as f:
                for line in f:
                    parts = line.strip().split('----', 3)
                    if len(parts) == 4:
                        self.accounts.append(parts)
        if not self.accounts:
            raise ValueError(f'Outlook account file is empty or invalid: {path}')
        self.index = 0

    async def create_email(self):
        i = self.index % len(self.accounts)
        self.index += 1
        acc = self.accounts[i]
        return OutlookInbox(address=acc[0], index=i, password=acc[1], client_id=acc[2], refresh_token=acc[3])

    async def latest_uid(self, inbox: OutlookInbox):
        return await asyncio.to_thread(get_folder_highest_uids, inbox.address, inbox.client_id, inbox.refresh_token)

    async def wait_for_code(self, inbox: OutlookInbox, baseline_folders: dict[str, int] = None):
        timeout = int(os.getenv('POLL_TIMEOUT', '120'))
        interval = int(os.getenv('POLL_INTERVAL', '5'))
        return await wait_for_outlook_code(inbox.address, inbox.client_id, inbox.refresh_token, baseline_folders, timeout, interval)
