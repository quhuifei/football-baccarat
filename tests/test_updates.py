import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
import update_scores as scores

class OutputTests(unittest.TestCase):
    def test_failed_fetch_preserves_old_but_complete_empty_refreshes(self):
        with tempfile.TemporaryDirectory() as d:
            j, s = str(Path(d)/'data.json'), str(Path(d)/'data.js')
            old = {'fixtures': [{'date':'2026-08-17'}]*20}
            scores.safe_write_output(j,s,'DATA',old,'fixtures','test')
            self.assertFalse(scores.safe_write_output(j,s,'DATA',{'fixtures':[]},'fixtures','test'))
            self.assertEqual(json.loads(Path(j).read_text()),old)
            self.assertTrue(scores.safe_write_output(j,s,'DATA',{'fixtures':[]},'fixtures','test',complete=True))
            self.assertEqual(json.loads(Path(j).read_text()),{'fixtures':[]})
            self.assertEqual(Path(s).read_text(),'window.DATA = {"fixtures": []};\n')
            self.assertEqual(len(list(Path(d).iterdir())),2)

    def test_api_error_never_marks_window_complete(self):
        for payload, expected in (({'response': [], 'errors': []}, scores.FAST_DAYS + scores.FIXTURE_DAYS),
                                  ({'response': [], 'errors': {'quota': 'limited'}}, 0),
                                  ({'errors': []}, 0)):
            health = set()
            with patch.object(scores.time, 'sleep'), patch.object(scores.urllib.request, 'urlopen',
                    side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())):
                matches, fixtures = scores.fetch_apifb_fast({}, {}, set(), health)
            self.assertEqual(len(health), expected)
            self.assertEqual(matches, [])
            self.assertEqual(fixtures, [])

    def test_daily_api_budget_stops_before_five_thousand(self):
        def status(current):
            return io.BytesIO(json.dumps({'response': {'requests': {'current': current}}}).encode())
        with patch.object(scores, 'APIFB_KEY', 'test-key'), \
                patch.object(scores.urllib.request, 'urlopen', side_effect=lambda *a, **k: status(4943)):
            self.assertTrue(scores.api_budget_available())
        with patch.object(scores, 'APIFB_KEY', 'test-key'), \
                patch.object(scores.urllib.request, 'urlopen', side_effect=lambda *a, **k: status(4944)):
            self.assertFalse(scores.api_budget_available())

    def test_unhealthy_source_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as d:
            json_path, js_path = str(Path(d) / 'data.json'), str(Path(d) / 'data.js')
            old = {'matches': [{'id': n} for n in range(20)]}
            scores.safe_write_output(json_path, js_path, 'DATA', old, 'matches', 'test')
            self.assertFalse(scores.safe_write_output(
                json_path, js_path, 'DATA', {'matches': [{'id': 1}]}, 'matches', 'test', healthy=False,
            ))
            self.assertEqual(json.loads(Path(json_path).read_text()), old)

    def test_failed_league_keeps_history_but_fast_result_wins(self):
        old = [
            {'_league': 'E0', 'date': '2026-09-01', 'team1': 'A', 'team2': 'B', 's1': 1},
            {'_league': 'SP1', 'date': '2026-09-01', 'team1': 'C', 'team2': 'D', 's1': 2},
        ]
        fresh = [
            {'_league': 'E0', 'date': '2026-09-01', 'team1': 'A', 'team2': 'B', 's1': 3},
            {'_league': 'E0', 'date': '2026-09-02', 'team1': 'E', 'team2': 'F', 's1': 0},
        ]
        merged, retained = scores.restore_previous_missing_leagues(fresh, old, {'E0'})
        self.assertEqual(retained, 1)
        self.assertEqual(len(merged), 2)
        self.assertEqual(next(m for m in merged if m['team1'] == 'A')['s1'], 3)

    def test_initial_odds_snapshot_retention_covers_a_season(self):
        self.assertGreaterEqual(scores.OU_STORE_DAYS, 365)

    def test_quarter_lines(self):
        self.assertEqual(scores.settle_ou(3,2.5),'大')
        self.assertEqual(scores.settle_ou(3,3),'走')
        self.assertEqual(scores.settle_ou(3,2.75),'半大')
        self.assertEqual(scores.settle_ou(3,3.25),'半小')

if __name__ == '__main__': unittest.main()
