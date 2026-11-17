"""Database helpers shared by the email services.

mysql_sync — api, tracking, milter and infra. The milter path is synchronous (libmilter);
the API uses the async wrappers of mysql_sync, which run on a dedicated ThreadPoolExecutor.
"""

from mail_shared.db import mysql_sync

__all__ = ["mysql_sync"]
