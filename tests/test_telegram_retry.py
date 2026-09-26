import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError
from app.telegram_retry import retry_telegram


class TelegramRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_transient_network_failure_is_retried(self):
        operation = AsyncMock(side_effect=[TelegramNetworkError(method=AsyncMock(), message="offline"), "done"])
        notice = AsyncMock()
        result = await retry_telegram(operation, notice=notice)
        self.assertEqual(result, "done")
        self.assertEqual(operation.await_count, 2)
        notice.assert_awaited_once_with(1, 1.0)
