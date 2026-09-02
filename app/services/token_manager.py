import asyncio, json, logging
from pathlib import Path
from fastapi import HTTPException
from app.services.arting_client import claim_daily, get_balance

logger = logging.getLogger(__name__)

class TokenManager:
    def __init__(self, file_path='data/tokens.json'):
        self.file_path=Path(file_path); self.tokens=[]; self.current_index=0; self.lock=asyncio.Lock()
    async def load(self):
        self.file_path.parent.mkdir(parents=True,exist_ok=True)
        raw=json.loads(self.file_path.read_text(encoding='utf-8')) if self.file_path.exists() else []
        self.tokens=[]
        for i,x in enumerate(raw,1):
            self.tokens.append({'id':i,'email':x.get('email',''),'password':'','token':x.get('token',''),
                'disabled':bool(x.get('disabled',False)),'remaining':x.get('remaining',0),'is_alive':True})
        logger.info('已加载 %d 个 Piax Token（无代理池、无活跃池）',len(self.tokens))
    async def _save(self):
        self.file_path.write_text(json.dumps(self.tokens,ensure_ascii=False,indent=2),encoding='utf-8')
    async def get_next(self):
        async with self.lock:
            available=[x for x in self.tokens if not x['disabled'] and x['token']]
            if not available: raise HTTPException(503,'No available Piax token')
            obj=available[self.current_index%len(available)]; self.current_index+=1; return obj['token']
    async def report_request_success(self,token): pass
    async def report_request_failure(self,token,reason=''): logger.warning('Piax request failed: %s',reason)
    async def recover_auth_failure(self,token): return False
    async def _refresh(self,obj):
        try:
            await claim_daily(obj['token'])
            status=await get_balance(obj['token']); plan=status.get('subscriptionPlan') or {}
            obj['remaining']=plan.get('subscriptionBalance',0)+plan.get('totalityBalance',0); obj['is_alive']=status.get('code')==1
        except Exception as exc: obj['is_alive']=False; logger.warning('Piax token id=%s 保活失败: %s',obj['id'],exc)
        return obj
    async def refresh_all(self):
        semaphore = asyncio.Semaphore(30)
        async def limited(obj):
            async with semaphore:
                return await self._refresh(obj)
        await asyncio.gather(*(limited(x) for x in self.tokens if not x['disabled']))
        await self._save()
    async def refresh_many(self,ids):
        await asyncio.gather(*(self._refresh(x) for x in self.tokens if x['id'] in set(ids))); await self._save()
    async def refresh_one(self,i):
        x=next((x for x in self.tokens if x['id']==i),None)
        if x: await self._refresh(x); await self._save()
        return x
    async def add(self,items):
        emails={x['email'] for x in self.tokens}; added=0
        for x in items:
            if x.get('email') in emails: continue
            self.tokens.append({'id':len(self.tokens)+1,'email':x.get('email',''),'password':'','token':x.get('token',''),'disabled':False,'remaining':0,'is_alive':True}); added+=1
        await self._save(); return {'added':added,'skipped':len(items)-added}
    def remove(self,i):
        old=len(self.tokens); self.tokens=[x for x in self.tokens if x['id']!=i]; asyncio.create_task(self._save()); return len(self.tokens)<old
    def remove_many(self,ids):
        old=len(self.tokens); self.tokens=[x for x in self.tokens if x['id'] not in set(ids)]; asyncio.create_task(self._save()); return old-len(self.tokens)
    def disable(self,ids): return self._toggle(ids,True)
    def enable(self,ids): return self._toggle(ids,False)
    def _toggle(self,ids,value):
        n=0
        for x in self.tokens:
            if x['id'] in set(ids) and x['disabled']!=value: x['disabled']=value; n+=1
        if n: asyncio.create_task(self._save())
        return n
    def summary(self):
        enabled=[x for x in self.tokens if not x['disabled']]
        return {'total':len(self.tokens),'alive':sum(x['is_alive'] for x in enabled),'active':len(enabled),'active_target':len(enabled),'standby':0,'retired':0,'disabled':len(self.tokens)-len(enabled),'exhausted':0,'total_remaining':sum(x['remaining'] for x in enabled),'refilling':False}
    def detail(self):
        return [{'index':x['id'],'email':x['email'],'token_hint':'...'+x['token'][-8:],'is_alive':x['is_alive'],'active':not x['disabled'],'pool_state':'enabled' if not x['disabled'] else 'disabled','remaining':x['remaining'],'error_count':0,'proxy':'','disabled':x['disabled']} for x in self.tokens]
    async def keep_alive_loop(self,interval_minutes=30):
        while True: await asyncio.sleep(interval_minutes*60); await self.refresh_all()
    async def handle_proxy_pool_refresh(self): pass
    async def close(self): pass
