from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sol_runner_bot.bot import SolRunnerAlertBot
from sol_runner_bot.models import RuntimeOptions, Snapshot
from sol_runner_bot.rules_loader import RulesetLoader
from sol_runner_bot.state_store import SqliteStateStore


ROOT = Path(__file__).resolve().parents[1]
RULESET = ROOT / "rules" / "sol_meme_runner_ruleset_v1_1.json"
SAMPLE = ROOT / "sample_data" / "sample_snapshots.jsonl"
REJECT = ROOT / "sample_data" / "reject_snapshot.jsonl"


class BotTests(unittest.TestCase):
    def make_bot(self, dispatch_rejects: bool = False):
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()
        store = SqliteStateStore(temp_db.name)
        bot = SolRunnerAlertBot(
            ruleset=RulesetLoader.load(RULESET),
            store=store,
            runtime=RuntimeOptions(dispatch_rejects=dispatch_rejects, dispatch_candidates=True, write_all_events_jsonl=None),
        )
        return bot, store, Path(temp_db.name)

    def test_candidate_alert_rearm_alert(self):
        bot, store, db_path = self.make_bot()
        try:
            snapshots = [Snapshot.from_dict(json.loads(line)) for line in SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]
            emitted = []
            for snapshot in snapshots:
                emitted.extend(bot.process_snapshot(snapshot))
            self.assertEqual([event.event for event in emitted], [
                "runner_candidate",
                "runner_alert",
                "runner_candidate",
                "runner_alert",
            ])
        finally:
            store.close()
            db_path.unlink(missing_ok=True)

    def test_reject_on_hard_veto(self):
        bot, store, db_path = self.make_bot(dispatch_rejects=True)
        try:
            snapshot = Snapshot.from_dict(json.loads(REJECT.read_text(encoding="utf-8").strip()))
            emitted = bot.process_snapshot(snapshot)
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0].event, "reject")
            self.assertIn("MINT_AUTHORITY_NOT_REVOKED", emitted[0].reason_codes)
        finally:
            store.close()
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
