import unittest
from pathlib import Path


ACCOUNT_HTML = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_console" / "static" / "ibkr_account.html"


class AccountTicketStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = ACCOUNT_HTML.read_text(encoding="utf-8")

    def test_direct_ticket_sends_sync_confirmation_and_step_metadata(self):
        self.assertIn("wait_for_confirmation: true", self.html)
        self.assertIn("manual_price_step: state.priceStep", self.html)
        self.assertIn("protective_auto_shift: state.protectiveAutoShift", self.html)
        self.assertIn("三腿 Bracket 已确认", self.html)

    def test_ticket_entry_price_step_link_controls_are_present(self):
        self.assertIn('id="ticketPriceStep"', self.html)
        self.assertIn('id="ticketProtectiveAutoShift"', self.html)
        self.assertIn("onTicketEntryPriceInput()", self.html)
        self.assertIn("computeShiftedTicketProtectionPrices", self.html)
        self.assertIn("tp + normalizedDelta", self.html)
        self.assertIn("sl + normalizedDelta", self.html)


if __name__ == "__main__":
    unittest.main()
