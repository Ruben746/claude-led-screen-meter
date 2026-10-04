import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
from codex_meter import CodexMeter, UsageSwitch, normalize_limits
import claude_meter as m

class UsageTests(unittest.TestCase):
    def test_null_is_unavailable_and_multiple_buckets_are_preserved(self):
        parsed=normalize_limits({'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':0,'windowDurationMins':300,'resetsAt':123},'secondary':None},'other':{'primary':{'usedPercent':57,'windowDurationMins':60}}}})
        self.assertEqual(len(parsed),2)
        self.assertEqual(parsed[0]['windows'][0]['used'],0)
        self.assertEqual(len(parsed[0]['windows']),1)
        self.assertEqual(normalize_limits({'rateLimits':None}),[])

    def test_changes_in_either_window_queue_without_starvation(self):
        s=UsageSwitch()
        s.observe('claude',(10,20));s.observe('chatgpt',(30,40))
        self.assertEqual(s.choose(0,['claude','chatgpt']),'claude')
        s.observe('chatgpt',(30,41))
        self.assertEqual(s.choose(1,['claude','chatgpt']),'chatgpt')
        s.observe('claude',(11,20))
        s.observe('chatgpt',(31,41))
        self.assertEqual(s.choose(2,['claude','chatgpt']),'chatgpt')
        self.assertEqual(s.choose(9,['claude','chatgpt']),'claude')
        self.assertEqual(s.choose(17,['claude','chatgpt']),'chatgpt')
        self.assertEqual(s.choose(18,['claude']),'claude')

    def test_reset_is_a_change_but_identical_polls_are_not(self):
        s=UsageSwitch();s.observe('claude',(90,80));s.observe('claude',(90,80))
        self.assertEqual(s.pending,[])
        s.observe('claude',(0,80));self.assertEqual(s.pending,['claude'])

class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_gates_connection_and_credentials_never_in_state(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, patch.object(m,'ADMIN_TOKEN','admin'):
            c=CodexMeter(directory)
            c.connect=AsyncMock(return_value={'verificationUrl':'https://auth.openai.com/codex/device','userCode':'TEST-CODE','loginId':'private'})
            c.disconnect=AsyncMock()
            async with TestClient(TestServer(m.make_app(SimpleNamespace(address='',connected=False),m.State(),codex=c))) as client:
                for action in ('connect','disconnect'):
                    self.assertEqual((await client.post('/api/chatgpt/'+action,json={})).status,403)
                response=await client.post('/api/chatgpt/connect',json={'token':'admin'})
                self.assertEqual(response.status,200)
                self.assertNotIn('loginId',await response.text())
                body=await (await client.get('/api/state')).text()
                self.assertNotIn('TEST-CODE',body)
                self.assertNotIn('private',body)

    async def test_refresh_reads_only_account_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            c=CodexMeter(directory);c.start=AsyncMock()
            c.rpc=AsyncMock(side_effect=[{'account':{'type':'chatgpt'}},{'rateLimits':{'limitId':'codex','primary':{'usedPercent':42,'windowDurationMins':300}}}])
            await c.refresh()
            self.assertTrue(c.connected);self.assertIsNotNone(c.last_ok)
            self.assertEqual([call.args[0] for call in c.rpc.call_args_list],['account/read','account/rateLimits/read'])
            self.assertEqual(c.buckets[0]['windows'][0]['used'],42)
