"""建议原样留档和到期核验的集成回归；临时数据库、禁止网络。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src'))
from tg_game.features.stock import biz_stock_miniapp as stock
from tg_game.storage import Storage

NOW = 1791000000.0


class AdviceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Storage(Path(self.temp.name) / 'test.db')
        self.s.init_schema()
        self.pid = self.s.create_profile('offline-stock-review').id

    def snapshot(self, at=NOW, price=10, history=None, **extra):
        result = dict(ok=True, fetched_at=at, overview={'indices':[
            {'symbol':'IDX_TEST', 'name':'测试股', 'price':price,
             'history':history or [{'timestamp':at, 'price':price}]}
        ]}, portfolio={'positions':[]})
        result.update(extra)
        return stock.store_market_snapshot(self.s, self.pid, result, now=at)

    def rows(self, table='stock_advice_decisions'):
        with self.s.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')]

    def test_fresh_decision_is_saved_and_duplicate_quote_does_not_rewrite_it(self):
        first = self.snapshot()
        self.assertIn('advice_review', first, '建议必须留下可核对记录')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quote_price'], 10)
        self.assertTrue(rows[0]['reason'])
        self.snapshot(price=10.1)
        self.assertEqual(self.rows(), rows, '同一报价时刻不能覆盖原始建议')

    def test_repeated_state_is_sampled_every_six_hours_but_changes_are_immediate(self):
        self.snapshot()
        self.snapshot(NOW+1800, 10.1)
        self.assertEqual(len(self.rows()), 1)
        self.snapshot(NOW+3600, 20)
        self.assertEqual(len(self.rows()), 2)
        self.snapshot(NOW+7*3600, 20)
        self.assertEqual(len(self.rows()), 3)

    def test_wait_is_recorded_and_stale_or_missing_portfolio_is_not_a_decision(self):
        self.snapshot(price=30)
        self.assertEqual(self.rows()[0]['action'], 'wait')
        count = len(self.rows())
        self.snapshot(NOW+86400, 10, history=[{'timestamp':NOW,'price':10}])
        self.snapshot(NOW+2*86400, 10, portfolio={})
        self.assertEqual(len(self.rows()), count)

    def test_outcomes_wait_for_maturity_use_post_target_price_and_remain_immutable(self):
        self.snapshot()
        self.snapshot(NOW+23*3600, 11)
        self.assertTrue(all(r['status']=='pending' for r in self.rows('stock_advice_outcomes')))
        at=NOW+24*3600+600
        result=self.snapshot(at, 12, history=[
            {'timestamp':NOW+23*3600,'price':11},
            {'timestamp':NOW+24*3600-1,'price':99},
            {'timestamp':at,'price':12},
            {'timestamp':at+600,'price':50},  # 未知未来点不得用于核验
        ])
        outcome=self.rows('stock_advice_outcomes')[0]
        self.assertEqual(outcome['status'],'evaluated')
        self.assertAlmostEqual(outcome['return_pct'],20)
        self.assertEqual(outcome['evaluated_price_at'],at)
        self.assertIn('不是实盘收益','\n'.join(result['advice_review']['lines']))
        self.snapshot(at+600, 5)
        self.assertEqual(self.rows('stock_advice_outcomes')[0],outcome)

    def test_missing_target_price_is_reported_instead_of_using_days_later_price(self):
        self.snapshot()
        self.snapshot(NOW+49*3600, 15)
        first=self.rows('stock_advice_outcomes')[0]
        self.assertEqual(first['status'],'missing')
        self.assertIsNone(first['return_pct'])

    def test_profiles_are_isolated_and_restart_preserves_records(self):
        self.snapshot()
        other=self.s.create_profile('other-profile').id
        self.pid=other
        result=self.snapshot(price=20)
        self.assertEqual(result['advice_review']['decision_count'],1)
        before=self.rows()
        self.s=Storage(Path(self.temp.name)/'test.db')
        self.s.init_schema()
        self.assertEqual(self.rows(),before)

    def test_missing_profit_does_not_reissue_stale_exit_or_claim_hold(self):
        position = dict(symbol='IDX_TEST', name='测试股', quantity=10, currentPrice=10,
                        profitPct=-25, holdingStartTime=NOW-3600)
        self.snapshot(portfolio={'positions':[position]})
        self.assertEqual(self.rows()[0]['action'], 'exit')
        result = self.snapshot(NOW+1800, portfolio={'positions':[{**position, 'profitPct':None}]})
        lines, signature = stock.build_alert_lines(result, now=NOW+1800, include_idle=True)
        self.assertIn('盈亏数据缺失', '\n'.join(lines))
        self.assertNotIn('建议清仓', '\n'.join(lines))
        self.assertNotIn('继续持有', '\n'.join(lines))
        self.assertEqual(len(self.rows()), 1)
        # 强平风险有独立的本轮数据时仍应提醒。
        result = self.snapshot(NOW+3600, portfolio={'positions':[{**position, 'profitPct':None,
            'risk':{'liquidationPrice':9.9}}]})
        self.assertIn('融资风险', '\n'.join(stock.build_alert_lines(result, now=NOW+3600)[0]))

    def test_sampled_drawdown_and_gap_are_not_filled_with_imaginary_prices(self):
        self.snapshot()
        at = NOW+24*3600
        self.snapshot(at, 12, history=[{'timestamp':NOW+3600,'price':15},
                                     {'timestamp':NOW+7200,'price':9},
                                     {'timestamp':at,'price':12}])
        outcome = self.rows('stock_advice_outcomes')[0]
        self.assertAlmostEqual(outcome['max_drawdown_pct'], 40)
        self.assertEqual(outcome['sample_count'], 3)
        self.assertEqual(outcome['max_gap_seconds'], 22*3600)

    def test_all_horizons_survive_restart_and_failed_fetch_does_not_create_advice(self):
        self.snapshot()
        self.s = Storage(Path(self.temp.name)/'test.db')
        self.s.init_schema()
        self.snapshot(NOW+24*3600, 12)
        self.snapshot(NOW+72*3600, 11)
        result = self.snapshot(NOW+168*3600, 13)
        first = self.rows('stock_advice_outcomes')[:3]
        self.assertEqual([r['status'] for r in first], ['evaluated']*3)
        self.assertEqual([round(r['return_pct']) for r in first], [20,10,30])
        count = len(self.rows())
        self.snapshot(NOW+169*3600, ok=False, error='offline')
        self.assertEqual(len(self.rows()), count)
        self.assertIn('不是实盘收益', '\n'.join(stock.build_digest_lines(result, now=NOW+168*3600)))

    def test_page_exposes_review_with_escaped_reasons_and_empty_state(self):
        from jinja2 import Environment, FileSystemLoader
        from tg_game.features.stock.biz_stock_page_state import build_stock_view
        templates = Path(__file__).resolve().parents[1]/'app/assets/templates'
        env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
        template = env.get_template('modules/stock.html')
        kwargs = dict(automation_settings={'stock':{'enabled':False,'interval_minutes':30}},
                      module_commands=[], command_chat_ready=False, request={'query_params':{}})
        state = build_stock_view(self.s, self.pid, None, format_timestamp=lambda t: str(t))
        self.assertIn('等待下一轮有效行情', template.render(stock_state=state, **kwargs))
        self.snapshot(overview={'indices':[{'symbol':'IDX_TEST','name':'<script>bad</script>',
                      'price':10,'history':[{'timestamp':NOW,'price':10}]}]})
        self.snapshot(NOW+86400, 12)
        state = build_stock_view(self.s, self.pid, None, format_timestamp=lambda t: str(t))
        html = template.render(stock_state=state, **kwargs)
        self.assertIn('+20.0%', html)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', html)
        self.assertNotIn('<script>bad</script>', html)
        self.assertIn('不是实盘收益', html)

    def test_market_fallback_does_not_switch_account_review(self):
        from tg_game.features.stock.biz_stock_page_state import build_stock_view
        self.snapshot()
        other = self.s.create_profile('empty-account').id
        self.s.list_stock_source_messages = lambda **kwargs: [dict(profile_id=self.pid,
            stock_code='IDX_TEST', current_price=10, created_at=NOW)]
        self.s.list_stock_market_info = lambda pid: []
        state = build_stock_view(self.s, other, None, format_timestamp=lambda t: str(t))
        self.assertFalse(state['advice_review'], '公共行情回退不能切换账号的建议记录')

    def test_failed_fetch_still_marks_overdue_missing_price(self):
        self.snapshot()
        self.snapshot(NOW+49*3600, ok=False, error='offline')
        self.assertEqual(self.rows('stock_advice_outcomes')[0]['status'], 'missing')
        self.assertEqual(len(self.rows()), 1)

    def test_portfolio_card_does_not_turn_unknown_profit_into_zero(self):
        from tg_game.features.stock.biz_stock_page_state import portfolio_card_from_snapshot
        card = portfolio_card_from_snapshot({'portfolio':{'positions':[
            dict(symbol='IDX_TEST', quantity=10, currentPrice=10, profitPct=None)
        ]}}, format_timestamp=str)
        self.assertIn('盈亏未提供', card['text'])
        self.assertNotIn('+0.0%', card['text'])

    def test_partial_success_reviews_available_history_for_omitted_stock(self):
        self.snapshot()
        # 公共行情可由另一账号采集；当前账号本轮未返回该标的仍可核对。
        other = self.s.create_profile('history-only').id
        self.s.upsert_stock_market_history_rows([(other, 0, int(NOW+86400), 'IDX_TEST',
            dict(stock_name='测试股', current_price=12, observed_at=NOW+86400, raw_text='miniapp:1D'))])
        self.snapshot(NOW+49*3600, overview={'indices':[]})
        outcome = self.rows('stock_advice_outcomes')[0]
        self.assertEqual(outcome['status'], 'evaluated')
        self.assertAlmostEqual(outcome['return_pct'], 20)


if __name__=='__main__':
    def deny_network(event,args):
        if event in {'socket.connect','socket.getaddrinfo','socket.sendto'}:
            raise AssertionError('network forbidden')
    sys.addaudithook(deny_network)
    unittest.main()
