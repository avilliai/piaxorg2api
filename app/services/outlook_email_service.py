import asyncio, imaplib, os, re, requests, quopri
from dataclasses import dataclass

@dataclass
class OutlookInbox:
    address: str
    index: int

class OutlookEmailService:
    def __init__(self):
        path = os.getenv('OUTLOOK_ACCOUNT_FILE', '')
        self.accounts = []
        with open(path, encoding='utf-8-sig') as f:
            for line in f:
                parts = line.strip().split('----', 3)
                if len(parts) == 4:
                    self.accounts.append(parts)
        if not self.accounts:
            raise ValueError('OUTLOOK_ACCOUNT_FILE is empty or invalid')
        self.index = 0

    async def create_email(self):
        i = self.index % len(self.accounts)
        self.index += 1
        return OutlookInbox(self.accounts[i][0], i)

    async def latest_uid(self, inbox):
        """Return the current highest IMAP sequence ID across Junk and INBOX before sending a code request."""
        email, _, client_id, refresh = self.accounts[inbox.index]
        resp = requests.post(
            os.getenv('OUTLOOK_TOKEN_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/token'),
            data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': refresh},
            timeout=20,
        )
        token = resp.json()['access_token']
        host = os.getenv('OUTLOOK_IMAP_HOSTS', 'outlook.office365.com').split(';')[0]
        c = imaplib.IMAP4_SSL(host, int(os.getenv('OUTLOOK_IMAP_PORT', '993')), timeout=30)
        try:
            auth = f'user={email}\x01auth=Bearer {token}\x01\x01'.encode()
            c.authenticate('XOAUTH2', lambda _: auth)
            max_id = 0
            for folder in ['"Junk"', 'INBOX']:
                try:
                    typ, _ = c.select(folder, readonly=True)
                    if typ == 'OK':
                        typ, ids = c.search(None, 'ALL')
                        if typ == 'OK' and ids[0].split():
                            max_id = max(max_id, int(ids[0].split()[-1]))
                except Exception:
                    pass
            return max_id
        finally:
            try:
                c.logout()
            except Exception:
                pass

    def _read_code(self, account, after_uid=0):
        email, _, client_id, refresh = account
        resp = requests.post(
            os.getenv('OUTLOOK_TOKEN_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/token'),
            data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': refresh},
            timeout=20,
        )
        token = resp.json()['access_token']
        hosts = [h.strip() for h in os.getenv('OUTLOOK_IMAP_HOSTS', 'outlook.office365.com;imap-mail.outlook.com').replace(',', ';').split(';') if h.strip()]
        last = None
        for host in hosts:
            c = None
            try:
                c = imaplib.IMAP4_SSL(host, int(os.getenv('OUTLOOK_IMAP_PORT', '993')), timeout=30)
                auth = f'user={email}\x01auth=Bearer {token}\x01\x01'.encode()
                typ, _ = c.authenticate('XOAUTH2', lambda _: auth)
                if typ != 'OK':
                    raise RuntimeError(f'XOAUTH2 status={typ}')
                # PIAX verification emails frequently arrive in Junk folder on Outlook.
                for folder in ['"Junk"', 'INBOX']:
                    typ, _ = c.select(folder, readonly=True)
                    if typ != 'OK':
                        continue
                    typ, ids = c.search(None, 'ALL')
                    if typ != 'OK' or not ids[0].split():
                        continue
                    # Only accept newly arrived messages strictly after baseline
                    candidates = [mid for mid in ids[0].split() if int(mid) > after_uid]
                    if not candidates:
                        continue
                    for mid in candidates[::-1][:2]:
                        typ, data = c.fetch(mid, '(RFC822)')
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
                return None
            except Exception as exc:
                last = exc
            finally:
                if c:
                    try:
                        c.logout()
                    except Exception:
                        pass
        raise RuntimeError(f'Outlook IMAP connection failed: {last}')

    async def wait_for_code(self, inbox, after_uid=0):
        timeout = int(os.getenv('POLL_TIMEOUT', '100'))
        interval = int(os.getenv('POLL_INTERVAL', '5'))
        for _ in range(max(1, timeout // interval)):
            await asyncio.sleep(interval)
            code = await asyncio.to_thread(self._read_code, self.accounts[inbox.index], after_uid)
            if code:
                return code
        raise TimeoutError(f'Outlook verification code timeout: {inbox.address}')
