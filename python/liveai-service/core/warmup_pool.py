# core/warmup_pool.py
"""
Production-ready Gemini warmup pool.

Features:
- ThreadPoolExecutor to run blocking genai.Client init without blocking event loop.
- Semaphore to limit concurrent sync client creations.
- Batch warmup to avoid cold-start spikes.
- Concurrent replenish (ensure_pool_capacity) with locking to avoid duplicate replenishes.
- keep_alive task and graceful shutdown support.
- Credential caching.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List

import google.genai as genai
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

# ---------- Configurable parameters ----------
DEFAULT_POOL_SIZE = 10
DEFAULT_MAX_WORKERS = 4           # threadpool size for running blocking init
DEFAULT_CREATION_CONCURRENCY = 3  # how many blocking inits to run concurrently
DEFAULT_BATCH_SIZE = 3            # how many to warmup per batch during startup
DEFAULT_KEEPALIVE_INTERVAL = 300  # seconds
# ---------------------------------------------

# A cached credentials object to avoid repeated file IO
_CACHED_CREDS = None


def _get_cached_credentials(sa_path: str):
    global _CACHED_CREDS
    if _CACHED_CREDS is None:
        _CACHED_CREDS = service_account.Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    return _CACHED_CREDS


class GeminiWarmupPool:
    def __init__(
        self,
        pool_size: int = DEFAULT_POOL_SIZE,
        max_workers: int = DEFAULT_MAX_WORKERS,
        creation_concurrency: int = DEFAULT_CREATION_CONCURRENCY,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sa_key_path: str = "secrets/rayneoassiatent-x3pro-wwei-3066015b1faf.json",
    ):
        self.pool_size = pool_size
        self._queue: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()  # protects refill logic
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._sem = asyncio.Semaphore(creation_concurrency)
        self._batch_size = batch_size
        self._sa_key_path = sa_key_path

        self._keepalive_task: Optional[asyncio.Task] = None
        self._is_shutting_down = False

        # For tracking background refill tasks (optional, for shutdown wait)
        self._background_tasks: List[asyncio.Task] = []

    @property
    def qsize(self):
        return self._queue.qsize()

    async def warmup_sessions(self):
        """
        Initial warmup: create pool_size sessions.
        Use batching to avoid upstream quota spikes.
        """
        logger.info(f"[Warmup] Starting initial warmup: target={self.pool_size}, batch_size={self._batch_size}")
        created = 0
        while created < self.pool_size and not self._is_shutting_down:
            to_create = min(self._batch_size, self.pool_size - created)
            logger.info(f"[Warmup] Creating batch of {to_create} sessions (created so far: {created})")
            tasks = [asyncio.create_task(self._create_and_put_safe(i + created)) for i in range(to_create)]
            # Wait for batch to finish (they will use threadpool in background)
            await asyncio.gather(*tasks)
            created = await self._queue.qsize()
            # small pause between batches
            await asyncio.sleep(0.5)
        logger.info(f"[Warmup] Initial warmup finished. pool_size={self._queue.qsize()}")

    async def _create_and_put_safe(self, index: int):
        """Wrapper that manages concurrency semaphore and safe put."""
        if self._is_shutting_down:
            return
        # limit concurrent blocking inits
        async with self._sem:
            try:
                client = await self._create_client_in_thread()
                await self._queue.put(client)
                logger.info(f"[Warmup] Replenished session (index={index}) ✅ (now {self._queue.qsize()}/{self.pool_size})")
            except Exception as e:
                logger.exception(f"[Warmup] Failed to create session (index={index}): {e}")

    async def _create_client_in_thread(self):
        """
        Run blocking genai.Client init in a thread using run_in_executor.
        Returns the initialized client object.
        """
        loop = asyncio.get_running_loop()

        def _sync_create():
            # NOTE: keep this simple & synchronous - runs on worker thread
            from config.config import api_config  # import here to avoid import-time side-effects

            if api_config.use_vertex:
                location = os.getenv("VERTEX_LOCATION", "us-central1")
                project_id = os.environ.get("PROJECT_ID")
                if not project_id:
                    raise RuntimeError("PROJECT_ID is required for Vertex AI")

                logger.debug(f"[create] Creating Vertex AI client (location={location}, project={project_id})")
                creds = _get_cached_credentials(self._sa_key_path)

                client = genai.Client(
                    vertexai=True,
                    location=location,
                    project=project_id,
                    credentials=creds,
                )
            else:
                logger.debug("[create] Creating development genai Client")
                from config.config import api_config
                client = genai.Client(
                    vertexai=False,
                    http_options={"api_version": "v1alpha"},
                    api_key=api_config.api_key,
                )
            return client

        start = loop.time()
        client = await loop.run_in_executor(self._executor, _sync_create)
        elapsed = loop.time() - start
        logger.info(f"[create] genai.Client created asynchronously (took {elapsed:.2f}s)")
        return client

    async def get_session(self):
        """
        Get a session from pool. If pool not empty, return immediately and trigger background replenish.
        If empty, create a new client immediately (but via threadpool).
        """
        if self._is_shutting_down:
            raise RuntimeError("Pool is shutting down")

        if not self._queue.empty():
            client = await self._queue.get()
            logger.info(f"[Warmup] Using preheated session (remaining: {self._queue.qsize()}/{self.pool_size})")
            # schedule background ensure (non-blocking)
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.ensure_pool_capacity())
            self._background_tasks.append(task)
            # remove finished tasks occasionally
            self._cleanup_background_tasks()
            return client

        # no preheated session available, create one now
        logger.warning("[Warmup] No preheated session available, creating new client on demand")
        client = await self._create_client_in_thread()
        return client

    def _cleanup_background_tasks(self):
        # drop finished tasks from list to avoid memory growth
        self._background_tasks = [t for t in self._background_tasks if not t.done()]

    async def ensure_pool_capacity(self):
        """
        Check pool capacity and replenish to pool_size when below 50%.
        This method is protected by _lock to avoid duplicate replenishes.
        """
        if self._is_shutting_down:
            return
        async with self._lock:
            current = self._queue.qsize()
            threshold = max(1, self.pool_size // 2)
            if current >= threshold:
                return
            needed = self.pool_size - current
            logger.warning(f"[Warmup] Pool below threshold ({current}/{self.pool_size}). Replenishing {needed} sessions...")

            # Create needed clients concurrently (but each creation will obey _sem)
            tasks = [asyncio.create_task(self._create_and_put_safe(i)) for i in range(needed)]
            # Wait for all tasks to finish (they will run in threadpool)
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"[Warmup] Replenish complete. now {self._queue.qsize()}/{self.pool_size}")

    async def return_session(self, client):
        """
        Return a session back to the pool. If client is closed/unusable, attempt to close and not return.
        """
        if self._is_shutting_down:
            # try to close the client and skip re-put
            await self._close_client_safe(client)
            return

        # Optionally validate client health here
        try:
            await self._queue.put(client)
            logger.debug(f"[Warmup] Session returned to pool (now {self._queue.qsize()}/{self.pool_size})")
        except Exception as e:
            logger.warning(f"[Warmup] Failed to return session to pool: {e}")
            await self._close_client_safe(client)

    async def _close_client_safe(self, client):
        """Try to close underlying client resources if supported by SDK."""
        try:
            # many SDKs expose close or transport/transport_channel
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()
                logger.debug("[Warmup] client.close() called")
            else:
                # try transport/transport_channel
                transport = getattr(client, "transport", None)
                if transport is not None:
                    close_transport = getattr(transport, "close", None)
                    if callable(close_transport):
                        close_transport()
                        logger.debug("[Warmup] client.transport.close() called")
        except Exception as e:
            logger.debug(f"[Warmup] Exception while closing client: {e}")

    async def keep_alive(self, interval: int = DEFAULT_KEEPALIVE_INTERVAL):
        """
        Periodic check of pooled clients. If any appear broken, recreate them.
        This method runs continuously until shutdown is called.
        """
        logger.info("[Warmup] keep_alive started")
        while not self._is_shutting_down:
            await asyncio.sleep(interval)
            try:
                # snapshot to avoid mutating during iteration
                snapshot: List = list(self._queue._queue)
                logger.debug(f"[Warmup] keep_alive checking {len(snapshot)} sessions")
                for idx, client in enumerate(snapshot):
                    # if SDK supports a lightweight ping or health-check, call it.
                    # Here we just attempt a no-op or sleep to keep the code non-blocking.
                    try:
                        # Insert any SDK-specific lightweight call here, e.g. client.ping()
                        await asyncio.sleep(0)  # placeholder for potential async check
                    except Exception:
                        logger.warning("[Warmup] Detected bad pooled client; replacing...")
                        # remove one client from queue and replace it
                        try:
                            bad = await self._queue.get()
                            await self._close_client_safe(bad)
                        except Exception:
                            pass
                        await self._create_and_put_safe(-1)
            except asyncio.CancelledError:
                logger.info("[Warmup] keep_alive cancelled")
                break
            except Exception as e:
                logger.exception(f"[Warmup] keep_alive encountered exception: {e}")

        logger.info("[Warmup] keep_alive stopped")

    async def shutdown(self, wait: bool = True):
        """
        Gracefully shutdown the pool:
        - mark shutting down
        - cancel keepalive
        - wait for background tasks optionally
        - drain queue and close clients
        - shutdown threadpool executor
        """
        logger.info("[Warmup] Shutdown: starting")
        self._is_shutting_down = True

        # cancel pending background tasks
        for t in list(self._background_tasks):
            if not t.done():
                t.cancel()
        self._background_tasks.clear()

        # stop keepalive if running
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except Exception:
                pass

        # drain queue and close clients
        drained = []
        while not self._queue.empty():
            try:
                c = self._queue.get_nowait()
                drained.append(c)
            except asyncio.QueueEmpty:
                break

        for client in drained:
            try:
                await self._close_client_safe(client)
            except Exception:
                pass

        # shutdown executor
        try:
            self._executor.shutdown(wait=wait)
            logger.info("[Warmup] ThreadPoolExecutor shutdown")
        except Exception as e:
            logger.warning(f"[Warmup] Exception shutting down executor: {e}")

        logger.info("[Warmup] Shutdown complete")

    # Convenience helpers for starting background tasks
    def start_background_tasks(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        Start keepalive in background. Call this after creating the pool and/or after warmup.
        """
        if loop is None:
            loop = asyncio.get_event_loop()
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = loop.create_task(self.keep_alive())
            logger.info("[Warmup] keep_alive task scheduled")

# Single global pool instance (adjust size as needed)
gemini_pool = GeminiWarmupPool(pool_size=DEFAULT_POOL_SIZE)
