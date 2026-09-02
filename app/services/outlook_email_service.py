import asyncio, imaplib, os, re, requests
from dataclasses import dataclass

@dataclass
class OutlookInbox:
    address: str
    index: int

class OutlookEmailService:
    def __init__(self):
        path=os.getenv('OUTLOOK_ACCOUNT_FILE','')
        self.accounts=[]
        with open(path,encoding='utf-8-sig') as f:
            for line in f:
                parts=line.strip().split('----',3)
                if len(parts)==4: self.accounts.append(parts)
        if not self.accounts: raise ValueError('OUTLOOK_ACCOUNT_FILE is empty or invalid')
        self.index=0
    async def create_email(self):
        i=self.index % len(self.accounts); self.index+=1
        return OutlookInbox(self.accounts[i][0],i)
    def _read_code(self, account):
        email,_,client_id,refresh=account
        token=requests.post(os.getenv('OUTLOOK_TOKEN_URL','https://login.microsoftonline.com/common/oauth2/v2.0/token'),data={'client_id':client_id,'grant_type':'refresh_token','refresh_token':refresh},timeout=20).json()['access_token']
        hosts=[h.strip() for h in os.getenv('OUTLOOK_IMAP_HOSTS','outlook.office365.com;imap-mail.outlook.com').replace(',',';').split(';') if h.strip()]
        last=None
        for host in hosts:
            c=None
            try:
                c=imaplib.IMAP4_SSL(host,int(os.getenv('OUTLOOK_IMAP_PORT','993')),timeout=30)
                auth=f'user={email}\x01auth=Bearer {token}\x01\x01'.encode()
                typ,_=c.authenticate('XOAUTH2',lambda _:auth)
                if typ != 'OK': raise RuntimeError(f'XOAUTH2 status={typ}')
                typ,_=c.select('INBOX',readonly=True)
                if typ != 'OK': raise RuntimeError(f'INBOX select status={typ}')
                typ,ids=c.search(None,'ALL')
                if typ != 'OK': raise RuntimeError(f'search status={typ}')
                for mid in ids[0].split()[-25:][::-1]:
                    typ,data=c.fetch(mid,'(RFC822)')
                    if typ != 'OK': continue
                    raw=b''.join(x[1] for x in data if isinstance(x,tuple)).decode('utf-8','ignore'); m=re.search(r'\b(\d{6})\b',raw)
                    if m: return m.group(1)
                return None
            except Exception as exc:
                last=exc
            finally:
                if c:
                    try: c.logout()
                    except Exception: pass
        raise RuntimeError(f'Outlook IMAP connection failed: {last}')
    async def wait_for_code(self,inbox):
        timeout=int(os.getenv('POLL_TIMEOUT','100')); interval=int(os.getenv('POLL_INTERVAL','5'))
        for _ in range(max(1,timeout//interval)):
            code=await asyncio.to_thread(self._read_code,self.accounts[inbox.index])
            if code:return code
            await asyncio.sleep(interval)
        raise TimeoutError(f'Outlook verification code timeout: {inbox.address}')
