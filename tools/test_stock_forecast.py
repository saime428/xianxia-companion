"""Forecast math and prospective records use synthetic prices and isolated SQLite."""
import math
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app/src'))
from tg_game.features.stock import biz_stock_forecast as forecast
from tg_game.storage import Storage

NOW = 1791000000.0


class ForecastTests(unittest.TestCase):
    def test_ridge_intercept_and_costs(self):
        model = forecast.fit([[0., 0.]]*60, [.1]*60)
        distribution = forecast.predict_distribution(model, [0., 0.])
        self.assertTrue(all(abs(r-.1)<1e-10 for r in distribution))
        buy = forecast.buy_assessment(distribution)
        self.assertAlmostEqual(buy['expected_return'], 1.09/1.005-1)
        self.assertEqual(buy['support'], 1)
        held = forecast.holding_assessment([0.], price=10, cost=8)
        self.assertEqual(held['expected_return'], 0, '已有仓位不能重复扣买入手续费')
        sell = forecast.holding_assessment([-.1], price=10, cost=8)
        self.assertAlmostEqual(sell['expected_return'], 8.9/9.8-1)

    def test_features_exclude_future_and_reject_stale_quote(self):
        points = [{'timestamp':NOW-i*86400, 'price':10+i/10} for i in range(30)]
        series = forecast.PriceSeries(points)
        before = series.features(NOW)
        self.assertEqual(len(before), 8)
        future = forecast.PriceSeries(points+[{'timestamp':NOW+1,'price':10000}])
        self.assertEqual(future.features(NOW), before)
        self.assertIsNone(series.features(NOW+7201))

    def test_tiny_variance_does_not_create_false_confidence(self):
        x = [[i*1e-14]+[0.]*7 for i in range(60)]
        y = [i/600 for i in range(60)]
        model = forecast.fit(x, y)
        predicted = forecast.predict_distribution(model, [2e-12]+[0.]*7)
        self.assertAlmostEqual(sum(predicted)/len(predicted), sum(y)/len(y))
        self.assertEqual(model['scale'][0], 1.)

    def test_training_excludes_unmatured_labels(self):
        points = [{'timestamp':NOW-i*3600, 'price':10+math.sin(i/90)} for i in range(180*24)]
        rows = forecast.training_rows({'TEST':forecast.PriceSeries(points)}, cutoff=NOW)
        self.assertGreater(len(rows), 60)
        self.assertTrue(all(r['exit_at'] < NOW for r in rows))
        self.assertTrue(all(r['exit_at'] >= r['entry_at']+72*3600 for r in rows))
        self.assertTrue(all(r['entry_at'] >= r['at']+3600 for r in rows))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(Path(self.temp.name)/'test.db')
        self.storage.init_schema()
        self.pid = self.storage.create_profile('forecast-tests').id

    def test_observation_preserves_original_and_uses_delayed_prices(self):
        model = forecast.fit([[0.]*8]*60, [.1]*60)
        model.update(cutoff=NOW-86400, trained_at=NOW, train_count=60, train_days=60, input_hash='test')
        forecast.save_model(self.storage, self.pid, model)
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[]},
                   'indices':[{'symbol':'TEST','name':'测试','price':10}]}
        points = {'TEST':[{'timestamp':NOW-i*86400,'price':10+i/10} for i in range(30)]}
        first = forecast.observe(self.storage, self.pid, summary, points, now=NOW, model=model)
        self.assertEqual(first['forecast_count'], 1)
        with self.storage.connect() as c:
            original = dict(c.execute('SELECT * FROM stock_price_forecasts').fetchone())
        forecast.observe(self.storage, self.pid, summary, points, now=NOW+1800, model=model)
        with self.storage.connect() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM stock_price_forecasts').fetchone()), original)
        # 不能用入场目标前的便宜价，也不能用退出目标前的高价。
        points['TEST'] += [{'timestamp':NOW+3599,'price':1}, {'timestamp':NOW+3600,'price':12}]
        forecast.observe(self.storage, self.pid, {'ok':False}, points, now=NOW+3600, model=model)
        end = NOW+73*3600
        points['TEST'] += [{'timestamp':end-1,'price':99}, {'timestamp':end,'price':15},
                          {'timestamp':end+1,'price':0.1}]
        forecast.observe(self.storage, self.pid, {'ok':False}, points, now=end, model=model)
        with self.storage.connect() as c:
            outcome = dict(c.execute('SELECT * FROM stock_price_forecasts').fetchone())
        self.assertEqual(outcome['status'], 'evaluated')
        self.assertEqual(outcome['entry_price'], 12)
        self.assertEqual(outcome['exit_price'], 15)
        self.assertAlmostEqual(outcome['actual_buy_return'], 14.7/12.06-1)
        points['TEST'].append({'timestamp':end,'price':3})
        forecast.observe(self.storage, self.pid, {'ok':False}, points, now=end+60, model=model)
        with self.storage.connect() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM stock_price_forecasts').fetchone()), outcome)

    def test_missing_entry_and_profile_scope(self):
        model = forecast.fit([[0.]*8]*60, [.1]*60)
        model.update(cutoff=NOW-86400, trained_at=NOW, train_count=60, train_days=60, input_hash='test')
        forecast.save_model(self.storage, self.pid, model)
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[]},
                   'indices':[{'symbol':'TEST','name':'测试','price':10}]}
        points = {'TEST':[{'timestamp':NOW-i*86400,'price':10+i/10} for i in range(30)]}
        forecast.observe(self.storage, self.pid, summary, points, now=NOW, model=model)
        result = forecast.observe(self.storage, self.pid, {'ok':False}, {}, now=NOW+26*3600, model=model)
        self.assertEqual(result['missing_count'], 1)
        other = self.storage.create_profile('other').id
        result = forecast.observe(self.storage, other, {'ok':False}, {}, now=NOW+26*3600)
        self.assertEqual(result['forecast_count'], 0)

    def test_observer_failure_does_not_break_existing_advice(self):
        from tg_game.features.stock import biz_stock_miniapp as stock
        result = dict(ok=True, fetched_at=NOW, portfolio={'positions':[]}, overview={'indices':[
            dict(symbol='TEST', name='测试', price=10, history=[dict(timestamp=NOW,price=10)])]})
        with patch.object(forecast, 'observe', side_effect=RuntimeError('synthetic failure')):
            summary = stock.store_market_snapshot(self.storage, self.pid, result, now=NOW)
        self.assertTrue(summary['ok'])
        self.assertIn('建议买入', '\n'.join(stock.build_alert_lines(summary, now=NOW)[0]))
        self.assertIn('暂不可用', '\n'.join(summary['forecast_review']['lines']))
        for ok in (True, False):
            with patch.object(forecast, 'pending_symbols', side_effect=RuntimeError('lookup failed')):
                summary = stock.store_market_snapshot(self.storage, self.pid, dict(result,ok=ok), now=NOW)
            self.assertEqual(summary['ok'], ok)
            self.assertIn('暂不可用', '\n'.join(summary['forecast_review']['lines']))

    def test_held_record_cost_and_model_survive_restart(self):
        from jinja2 import Environment, FileSystemLoader
        model = forecast.fit([[0.]*8]*60, [-.1]*60)
        model.update(cutoff=NOW-86400, trained_at=NOW, train_count=60, train_days=60, input_hash='held')
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[
            {'symbol':'TEST','quantity':10,'currentPrice':10,'avgCost':8}]},
            'indices':[{'symbol':'TEST','name':'<unsafe>','price':10}]}
        points = {'TEST':[{'timestamp':NOW-i*86400,'price':10+i/10} for i in range(30)]}
        result = forecast.observe(self.storage, self.pid, summary, points, now=NOW, model=model)
        self.assertEqual(result['recent'][0]['action'], 'exit')
        self.storage = Storage(Path(self.temp.name)/'test.db')
        self.storage.init_schema()
        changed = dict(model, coef=[10.]*9)
        saved = forecast.save_model(self.storage, self.pid, changed)
        self.assertEqual(saved['coef'], model['coef'], '同版本同训练截止不能覆盖模型')
        points['TEST'] += [{'timestamp':NOW+3600,'price':10},{'timestamp':NOW+73*3600,'price':9}]
        result = forecast.observe(self.storage, self.pid, {'ok':False}, points, now=NOW+73*3600, model=model)
        row = result['recent'][0]
        self.assertAlmostEqual(row['actual_hold_return'], 8.9/9.8-1)
        env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1]/'app/assets/templates')), autoescape=True)
        html = env.get_template('modules/stock.html').render(stock_state={'forecast_review':result},
            automation_settings={'stock':{'enabled':False,'interval_minutes':30}}, request={'query_params':{}},
            command_chat_ready=False, module_commands=[])
        self.assertIn('&lt;unsafe&gt;', html)
        self.assertIn('不参与买卖提醒', html)
        self.assertIn('继续持有相对延迟执行时卖出', html)
        self.assertIn('这1小时价差未建模', html)

    def test_missing_exit_is_not_replaced_by_days_later_quote(self):
        model = forecast.fit([[0.]*8]*60, [.1]*60)
        model.update(cutoff=NOW-86400, trained_at=NOW, train_count=60, train_days=60, input_hash='missing-exit')
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[]},
                   'indices':[{'symbol':'TEST','name':'测试','price':10}]}
        points = {'TEST':[{'timestamp':NOW-i*86400,'price':10+i/10} for i in range(30)]}
        forecast.observe(self.storage, self.pid, summary, points, now=NOW, model=model)
        points['TEST'] += [{'timestamp':NOW+3600,'price':10},{'timestamp':NOW+100*3600,'price':15}]
        result = forecast.observe(self.storage, self.pid, {'ok':False}, points, now=NOW+100*3600, model=model)
        self.assertEqual(result['missing_count'], 1)
        self.assertIsNone(result['recent'][0]['actual_buy_return'])

    def test_hold_comparison_starts_at_delayed_execution_price(self):
        model = forecast.fit([[0.]*8]*60, [0.]*60)
        model.update(cutoff=NOW-86400, trained_at=NOW, train_count=60, train_days=60, input_hash='execution')
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[
            {'symbol':'TEST','quantity':10,'currentPrice':10,'avgCost':8}]},
            'indices':[{'symbol':'TEST','name':'测试','price':10}]}
        points = {'TEST':[{'timestamp':NOW-i*86400,'price':10+i/10} for i in range(30)]}
        forecast.observe(self.storage, self.pid, summary, points, now=NOW, model=model)
        points['TEST'] += [{'timestamp':NOW+3600,'price':12},{'timestamp':NOW+73*3600,'price':12}]
        result = forecast.observe(self.storage, self.pid, {'ok':False}, points, now=NOW+73*3600, model=model)
        row = result['recent'][0]
        self.assertEqual(row['actual_hold_return'], 0)
        self.assertEqual((row['quote_price'],row['entry_price']), (10,12))

    def test_new_predictions_do_not_hide_completed_reviews(self):
        model = forecast.fit([[0.]*8]*60, [.1]*60)
        model.update(cutoff=NOW-86400, trained_at=NOW, train_count=60, train_days=60, input_hash='visible')
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[]},
                   'indices':[{'symbol':'TEST','name':'测试','price':10}]}
        points = {'TEST':[{'timestamp':NOW+i*3600,'price':10} for i in range(-21*24,74)]}
        forecast.observe(self.storage, self.pid, summary, points, now=NOW, model=model)
        end = NOW+73*3600
        result = forecast.observe(self.storage, self.pid, dict(summary,at=end), points, now=end, model=model)
        self.assertEqual(result['recent'][0]['forecast_at'], end)
        self.assertEqual(result['completed'][0]['forecast_at'], NOW)
        self.assertEqual(result['completed'][0]['status'], 'evaluated')

    def test_monthly_training_keeps_exact_inputs_and_cached_model(self):
        import json
        points = {'TEST':[{'timestamp':NOW-i*3600,'price':10+math.sin(i/90)} for i in range(180*24)]}
        summary = {'ok':True,'at':NOW,'portfolio_ok':True,'portfolio':{'positions':[]},
                   'indices':[{'symbol':'TEST','name':'测试','price':10}]}
        result = forecast.observe(self.storage, self.pid, summary, points, now=NOW)
        self.assertEqual(result['forecast_count'], 1)
        with self.storage.connect() as c:
            original = c.execute('SELECT model_json FROM stock_prediction_models').fetchone()[0]
        model = json.loads(original)
        self.assertEqual(model['train_count'], len(model['training_rows']))
        self.assertLess(model['latest_label_at'], model['cutoff'])
        points['TEST'][1000]['price'] = 100
        forecast.observe(self.storage, self.pid, summary, points, now=NOW+1800)
        with self.storage.connect() as c:
            self.assertEqual(c.execute('SELECT model_json FROM stock_prediction_models').fetchone()[0], original)


if __name__ == '__main__':
    def deny_network(event, args):
        if event in {'socket.connect','socket.getaddrinfo','socket.sendto'}:
            raise AssertionError('network forbidden')
    sys.addaudithook(deny_network)
    unittest.main()
