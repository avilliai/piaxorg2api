class _Config:
    enabled=False; fallback_proxy=None; protocol=''; max_concurrency_per_proxy=100; refresh_seconds=3600
    connect_timeout=15; tls_verify=True
class ProxyPoolError(Exception): pass
class ProxyPool:
    config=_Config(); refresh_generation=0
    async def get_proxy(self,*a,**k): return None
    def tls_verify(self,*a,**k): return True
    async def acquire_request_slot(self,*a,**k): return None
    def release_request_slot(self,*a,**k): pass
    async def report_success(self,*a,**k): pass
    async def report_failure(self,*a,**k): pass
    async def retire(self,*a,**k): pass
    async def bind_alias(self,*a,**k): pass
    async def refresh(self,*a,**k): pass
    async def handle_proxy_pool_refresh(self,*a,**k): pass
    async def status(self): return {'available': 0, 'cached': 0}
def get_proxy_pool(): return ProxyPool()
