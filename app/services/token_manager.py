import asyncio, json, logging, os, time
from datetime import datetime, timezone
from pathlib import Path
from fastapi import HTTPException
from app.services.arting_client import claim_daily, get_balance, get_daily_status

logger = logging.getLogger(__name__)


def get_today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TokenManager:
    def __init__(self, file_path='data/tokens.json'):
        self.file_path = Path(file_path)
        self.tokens = []
        self.current_index = 0
        self.lock = asyncio.Lock()
        self.active_pool_size = int(os.getenv('ACTIVE_POOL_SIZE', '3'))
        self.last_check_date = get_today_utc_str()
        self.min_standby_threshold = int(os.getenv('MIN_STANDBY_THRESHOLD', '5'))
        self.auto_replenish_batch = int(os.getenv('AUTO_REPLENISH_BATCH', '2'))
        self._replenishing = False
        self._last_replenish_attempt = 0
        self._replenish_cooldown = int(os.getenv('REPLENISH_COOLDOWN', '120'))

    async def load(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.loads(self.file_path.read_text(encoding='utf-8')) if self.file_path.exists() else []
        self.tokens = []
        for i, x in enumerate(raw, 1):
            state = x.get('state')
            if not state:
                state = 'active' if i <= self.active_pool_size else 'standby'
            if x.get('disabled', False):
                state = 'disabled'

            self.tokens.append({
                'id': i,
                'email': x.get('email', ''),
                'password': x.get('password', ''),
                'client_id': x.get('client_id', ''),
                'refresh_token': x.get('refresh_token', ''),
                'token': x.get('token', ''),
                'state': state,
                'disabled': bool(x.get('disabled', False)),
                'remaining': x.get('remaining', 0),
                'is_alive': bool(x.get('is_alive', True)),
                'last_claim_date': x.get('last_claim_date', ''),
                'error_count': 0,
            })
        logger.info(
            'TokenManager loaded %d tokens (active_pool_size=%d)',
            len(self.tokens), self.active_pool_size
        )
        await self.ensure_active_pool()

    async def _save(self):
        self.file_path.write_text(
            json.dumps(self.tokens, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

    async def _claim_and_update(self, obj: dict):
        today = get_today_utc_str()
        token = obj.get('token', '')
        if not token:
            obj['is_alive'] = False
            return obj

        try:
            # 1. Claim daily if not claimed today
            if obj.get('last_claim_date') != today:
                claim_res = await claim_daily(token)
                if claim_res.get('code') == 1:
                    obj['last_claim_date'] = today
                    logger.info("Account %s daily claim success (1000 points)", obj.get('email'))

            # 2. Query balance
            status = await get_balance(token)
            if status.get('code') == 1:
                plan = status.get('subscriptionPlan') or {}
                obj['remaining'] = plan.get('subscriptionBalance', 0) + plan.get('totalityBalance', 0)
                obj['is_alive'] = True
                obj['error_count'] = 0
            else:
                logger.warning("Account %s balance check failed: %s", obj.get('email'), status)
                # If upstream indicates auth error, attempt recovery
                if status.get('code') == 0 or 'user not exist' in str(status):
                    await self.recover_auth_failure(token)
        except Exception as exc:
            logger.warning('Token id=%s (%s) check failed: %s', obj.get('id'), obj.get('email'), exc)
            if '401' in str(exc) or 'user not exist' in str(exc):
                await self.recover_auth_failure(token)
        return obj

    async def ensure_active_pool(self):
        """Maintains the active pool: promotes standby accounts when active accounts are exhausted."""
        today = get_today_utc_str()

        # Check if UTC day rolled over
        if today != self.last_check_date:
            self.last_check_date = today
            logger.info("New day detected (%s). Resetting exhausted accounts to standby.", today)
            for x in self.tokens:
                if x['state'] == 'exhausted' and not x['disabled']:
                    x['state'] = 'standby'

        # Current usable active accounts
        active_usable = [
            x for x in self.tokens
            if x['state'] == 'active' and not x['disabled'] and x['is_alive'] and x['remaining'] > 0
        ]

        needed = self.active_pool_size - len(active_usable)
        if needed > 0:
            standby_candidates = [
                x for x in self.tokens
                if x['state'] == 'standby' and not x['disabled'] and x['is_alive']
            ]
            for candidate in standby_candidates[:needed]:
                logger.info("Promoting standby account %s to active pool", candidate.get('email'))
                candidate['state'] = 'active'
                await self._claim_and_update(candidate)

        await self._save()
        self.trigger_auto_replenish_if_needed()

    def trigger_auto_replenish_if_needed(self):
        """Triggers background auto-registration if standby accounts drop below threshold."""
        if self._replenishing:
            return
        now = time.time()
        if now - self._last_replenish_attempt < self._replenish_cooldown:
            return

        standby_count = sum(
            1 for x in self.tokens
            if x['state'] == 'standby' and not x['disabled'] and x['is_alive']
        )
        if standby_count < self.min_standby_threshold:
            logger.info(
                "Standby pool count (%d) below threshold (%d). Scheduling background auto-replenish.",
                standby_count, self.min_standby_threshold
            )
            asyncio.create_task(self._auto_replenish_task())

    async def _auto_replenish_task(self):
        """Quietly registers candidate accounts from file and adds them to standby pool."""
        if self._replenishing:
            return
        self._replenishing = True
        self._last_replenish_attempt = time.time()
        try:
            from app.services.register import register_batch_from_file
            logger.info("Auto-replenish started (target: %d accounts)...", self.auto_replenish_batch)
            new_accs = await register_batch_from_file(count=self.auto_replenish_batch)
            if new_accs:
                existing_emails = {x.get('email') for x in self.tokens}
                for acc in new_accs:
                    if acc.get('email') not in existing_emails:
                        self.tokens.append({
                            'id': len(self.tokens) + 1,
                            'email': acc.get('email', ''),
                            'password': acc.get('password', ''),
                            'client_id': acc.get('client_id', ''),
                            'refresh_token': acc.get('refresh_token', ''),
                            'token': acc.get('token', ''),
                            'state': 'standby',
                            'disabled': False,
                            'remaining': acc.get('remaining', 0),
                            'is_alive': True,
                            'last_claim_date': acc.get('last_claim_date', ''),
                            'error_count': 0,
                        })
                        existing_emails.add(acc.get('email'))
                await self._save()
                logger.info("Auto-replenish completed: added %d new standby accounts", len(new_accs))
        except Exception as e:
            logger.warning("Auto-replenish task error: %s", e)
            if "frequent" in str(e).lower() or "too frequent" in str(e).lower():
                # On upstream rate-limit, back off for 5 minutes
                self._last_replenish_attempt = time.time() + 300
        finally:
            self._replenishing = False

    async def get_next(self) -> str:
        async with self.lock:
            # 1. Ensure active pool has healthy accounts
            await self.ensure_active_pool()

            # 2. Get active accounts with remaining > 0
            available = [
                x for x in self.tokens
                if x['state'] == 'active' and not x['disabled'] and x['is_alive'] and x['remaining'] > 0
            ]

            # 3. Fallback: if no active account with remaining > 0, check all alive active accounts
            if not available:
                available = [
                    x for x in self.tokens
                    if x['state'] == 'active' and not x['disabled'] and x['is_alive']
                ]

            if not available:
                raise HTTPException(503, 'No available active Piax token with remaining quota')

            obj = available[self.current_index % len(available)]
            self.current_index += 1
            return obj['token']

    async def report_request_success(self, token: str):
        # Slightly decrement estimated points or check balance
        for x in self.tokens:
            if x['token'] == token:
                if x['remaining'] > 0:
                    x['remaining'] -= 10
                if x['remaining'] <= 0:
                    x['state'] = 'exhausted'
                    logger.info("Account %s exhausted quota, rotating active pool", x.get('email'))
                    asyncio.create_task(self.ensure_active_pool())
                break

    async def report_request_failure(self, token: str, reason: str = ''):
        logger.warning('Piax request failed for token %s... : %s', token[:15], reason)
        obj = next((x for x in self.tokens if x['token'] == token), None)
        if not obj:
            return

        obj['error_count'] = obj.get('error_count', 0) + 1

        # Check for quota exhaustion
        if 'quota' in reason.lower() or 'balance' in reason.lower() or 'insufficient' in reason.lower():
            obj['remaining'] = 0
            obj['state'] = 'exhausted'
            logger.info("Account %s marked as exhausted. Triggering active pool rotation.", obj.get('email'))
            await self.ensure_active_pool()

        # Check for auth failure / token expiration
        elif '401' in reason or 'user not exist' in reason.lower() or 'token' in reason.lower():
            await self.recover_auth_failure(token)

    async def recover_auth_failure(self, token: str) -> bool:
        """When a token expires or fails auth, automatically re-login via Outlook IMAP OAuth."""
        obj = next((x for x in self.tokens if x['token'] == token), None)
        if not obj:
            return False

        email = obj.get('email')
        client_id = obj.get('client_id')
        refresh_token = obj.get('refresh_token')

        if not email or not client_id or not refresh_token:
            logger.error("Cannot re-login account %s: missing Outlook credentials", email)
            obj['is_alive'] = False
            await self._save()
            return False

        logger.info("Attempting automatic re-login for expired account: %s", email)
        try:
            from app.services.register import relogin_account
            new_token = await relogin_account(obj)
            obj['token'] = new_token
            obj['is_alive'] = True
            obj['error_count'] = 0
            await self._claim_and_update(obj)
            await self._save()
            logger.info("Successfully re-logged in account: %s", email)
            return True
        except Exception as e:
            logger.error("Failed to re-login account %s: %s", email, e)
            obj['is_alive'] = False
            await self._save()
            return False

    async def refresh_all(self):
        """Refreshes active accounts (claims daily and updates balance)."""
        logger.info("Starting refresh_all for active tokens...")
        for x in self.tokens:
            if x['state'] == 'active' and not x['disabled']:
                await self._claim_and_update(x)
        await self.ensure_active_pool()

    async def refresh_many(self, ids: list[int]):
        for x in self.tokens:
            if x['id'] in set(ids):
                await self._claim_and_update(x)
        await self._save()

    async def refresh_one(self, i: int):
        x = next((x for x in self.tokens if x['id'] == i), None)
        if x:
            await self._claim_and_update(x)
            await self._save()
        return x

    async def add(self, items: list[dict]):
        emails = {x['email'] for x in self.tokens}
        added = 0
        for x in items:
            email = x.get('email', '')
            if email in emails:
                continue
            self.tokens.append({
                'id': len(self.tokens) + 1,
                'email': email,
                'password': x.get('password', ''),
                'client_id': x.get('client_id', ''),
                'refresh_token': x.get('refresh_token', ''),
                'token': x.get('token', ''),
                'state': 'standby',
                'disabled': False,
                'remaining': x.get('remaining', 0),
                'is_alive': True,
                'last_claim_date': '',
                'error_count': 0,
            })
            emails.add(email)
            added += 1
        await self.ensure_active_pool()
        return {'added': added, 'skipped': len(items) - added}

    def remove(self, i: int):
        old = len(self.tokens)
        self.tokens = [x for x in self.tokens if x['id'] != i]
        asyncio.create_task(self._save())
        return len(self.tokens) < old

    def remove_many(self, ids: list[int]):
        old = len(self.tokens)
        self.tokens = [x for x in self.tokens if x['id'] not in set(ids)]
        asyncio.create_task(self._save())
        return old - len(self.tokens)

    def disable(self, ids: list[int]):
        return self._toggle(ids, True)

    def enable(self, ids: list[int]):
        return self._toggle(ids, False)

    def _toggle(self, ids: list[int], value: bool):
        n = 0
        for x in self.tokens:
            if x['id'] in set(ids) and x['disabled'] != value:
                x['disabled'] = value
                if value:
                    x['state'] = 'disabled'
                else:
                    x['state'] = 'standby'
                n += 1
        if n:
            asyncio.create_task(self.ensure_active_pool())
        return n

    def summary(self):
        enabled = [x for x in self.tokens if not x['disabled']]
        active = [x for x in enabled if x['state'] == 'active']
        standby = [x for x in enabled if x['state'] == 'standby']
        exhausted = [x for x in enabled if x['state'] == 'exhausted']
        return {
            'total': len(self.tokens),
            'alive': sum(1 for x in enabled if x['is_alive']),
            'active': len(active),
            'active_target': self.active_pool_size,
            'standby': len(standby),
            'retired': 0,
            'disabled': len(self.tokens) - len(enabled),
            'exhausted': len(exhausted),
            'total_remaining': sum(x['remaining'] for x in active),
            'refilling': self._replenishing,
        }

    def detail(self):
        return [
            {
                'index': x['id'],
                'email': x['email'],
                'token_hint': '...' + x['token'][-8:] if x['token'] else '',
                'is_alive': x['is_alive'],
                'active': x['state'] == 'active' and not x['disabled'],
                'pool_state': x['state'],
                'remaining': x['remaining'],
                'last_claim_date': x.get('last_claim_date', ''),
                'error_count': x.get('error_count', 0),
                'disabled': x['disabled'],
            }
            for x in self.tokens
        ]

    async def keep_alive_loop(self, interval_minutes: int = 60):
        while True:
            await asyncio.sleep(interval_minutes * 60)
            try:
                await self.refresh_all()
            except Exception as e:
                logger.error("keep_alive_loop error: %s", e)

    async def handle_proxy_pool_refresh(self):
        pass

    async def close(self):
        pass
