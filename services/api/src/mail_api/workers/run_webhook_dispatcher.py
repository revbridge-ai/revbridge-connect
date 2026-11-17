"""Entry point of the webhook dispatcher (the `webhook-dispatcher` compose service).

    python -m mail_api.workers.run_webhook_dispatcher
"""

import asyncio

from mail_api.webhook_dispatch.dispatcher import main

if __name__ == "__main__":
    asyncio.run(main())
